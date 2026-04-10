# Copyright (c) 2026 Bob Amstadt
# SPDX-License-Identifier: MIT
"""
LLM Advisor for AIWanderer.

In human-play mode the LLM acts as a real-time advisor: it observes
every command the human types and the MUD's response, then offers brief
free-text tactical commentary.  It also retains a legacy request_action()
interface (gated behind AUTONOMOUS_DISABLED in ai_agent.py) for possible
future restoration of automated play.

Uses only Python stdlib (http.client, json, threading) — no external deps.

Configuration (stored in profile["ai_config"]):
    {
        "llm_backend":  "ollama",               # "ollama" | "claude"
        "llm_model":    "llama3.1:8b",          # any model installed in Ollama
        "llm_endpoint": "http://localhost:11434",# Ollama base URL
        "claude_api_key": "",                   # only needed for "claude" backend
        "claude_model": "claude-haiku-4-5-20251001"
    }
"""

from collections import Counter
import http.client
import json
import re
import threading
import urllib.parse


ADVISOR_SYSTEM_PROMPT = """You are a knowledgeable advisor watching a human play a \
text-based MUD (Multi-User Dungeon) fantasy RPG. The player types commands and you \
see what the MUD responds.

Your role:
- Provide brief, helpful tactical commentary after each command and response.
- Describe what just happened in plain language if it is not obvious.
- Point out anything the player should watch for (danger, opportunities, status effects).
- Suggest what the player might consider doing next — but do not issue commands yourself.
- Be concise: 2–4 sentences is ideal. Avoid restating what the MUD already said clearly.
- If nothing noteworthy happened (e.g. routine movement with no threats), keep it very brief.
- Flag low health, hunger, thirst, or nearby danger prominently.
- If the player seems stuck or is repeating commands without progress, say so and suggest alternatives."""

# Legacy prompt kept for potential restoration of autonomous play mode.
ACTION_SYSTEM_PROMPT = """You are an AI player in a MUD (Multi-User Dungeon), a text-based \
fantasy RPG. Your goal is to explore the world, gain experience, and survive.

Rules:
- Respond with ONLY a single game command — nothing else, no explanation.
- Valid movement commands: north, south, east, west, up, down (or n/s/e/w/u/d).
- Other useful commands: look, examine <object>, open <door>, get <item>, \
say <text>, kill <mob>, flee, score, inventory, buy <item>, eat <item>, drink <source>.
- Read the recent MUD text carefully — it tells you what just happened and what is here.
- If you see a shop, merchant, or vendor: type "list" to see what is for sale.
- If you see an NPC speaking to you, respond with: say <reply>
- If a door or container is mentioned, try: open <door/container>
- If you see an item on the ground that may be useful, try: get <item>
- If you are low on health, prioritise fleeing or resting.
- If you are IN COMBAT: movement commands are blocked — use flee, or any non-movement command (eat, drink, tell, say, cast, etc.).
- Prefer unexplored exits over revisiting known rooms.
- Do not repeat a command that was just blocked ("You can't go that way").
- Otto is a helper PC who provides healing and buffs via tell — he does NOT sell food or water."""


class LLMAdvisor:
    """
    Calls an LLM to advise a human MUD player in real time.

    All network I/O runs in a background thread so the tkinter main loop
    is never blocked.  Results are delivered via a callback scheduled on
    the main thread with master.after(0, ...).
    """

    # How often (in advice calls) to inject a full game-state refresh message.
    STATE_REFRESH_INTERVAL = 10

    def __init__(self, client):
        self.client = client          # MUDClient instance
        self._call_count = 0
        self._messages = []           # persistent context: init block + direct chat only
        self._advice_call_count = 0   # counts regular advice calls for refresh scheduling
        self._is_first_message = True # send full state on first turn only
        self._last_inventory = None   # track inventory for change detection
        self._last_equipment = None   # track equipment for change detection

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self):
        """Return True if an LLM is configured for this profile."""
        cfg = self._config()
        return cfg is not None and bool(cfg.get('llm_backend'))

    def reset_history(self):
        """Clear in-session conversation history. Called at each new connection."""
        self._messages = []
        self._advice_call_count = 0
        self._is_first_message = True
        self._last_inventory = None
        self._last_equipment = None

    def request_advice(self, events, room_data, on_result):
        """
        Asynchronously request advisor commentary from the LLM.

        events    — list of dicts with keys 'command' (str) and 'mud_lines' (list[str]).
                    Typically one entry, but may contain several when events were
                    batched while a previous LLM call was in flight.
        room_data — dict with name, description, exits from the room DB (or None)
        on_result — callable(advice: str | None) called on the main thread
        """
        context = {
            'events': events,
            'room_name': room_data.get('name', '') if room_data else '',
            'room_description': room_data.get('description', '') if room_data else '',
            'room_exits': room_data.get('exits', '') if room_data else '',
        }
        self._call_count += 1
        thread = threading.Thread(
            target=self._advice_worker,
            args=(context, on_result),
            daemon=True,
            name=f"llm-advisor-{self._call_count}"
        )
        thread.start()

    def request_action(self, context, on_result):
        """
        Asynchronously request an action from the LLM (legacy autonomous-play API).

        context   — dict produced by build_context()
        on_result — callable(command: str | None) called on the main thread
        """
        self._call_count += 1

        thread = threading.Thread(
            target=self._worker,
            args=(context, on_result),
            daemon=True,
            name=f"llm-action-{self._call_count}"
        )
        thread.start()

    def request_direct(self, prompt, on_result):
        """
        Asynchronously send a freeform user prompt into the advisor conversation.

        The prompt is injected as a user turn so the LLM replies in its advisor
        persona with full conversation history available.
        on_result — callable(reply: str | None) called on the main thread
        """
        self._call_count += 1
        thread = threading.Thread(
            target=self._direct_worker,
            args=(prompt, on_result),
            daemon=True,
            name=f"llm-direct-{self._call_count}"
        )
        thread.start()

    def generate_session_summary(self, on_result):
        """
        Asynchronously generate a session summary for cross-session persistence.

        Fires a one-shot LLM call summarising the advisor's own output from this
        session.  on_result(summary: str | None) is called on the main thread.
        """
        assistant_turns = [m['content'] for m in self._messages
                           if m['role'] == 'assistant']
        if not assistant_turns:
            self.client.master.after(0, lambda: on_result(None))
            return
        self._call_count += 1
        thread = threading.Thread(
            target=self._summary_worker,
            args=(assistant_turns, on_result),
            daemon=True,
            name=f"llm-summary-{self._call_count}"
        )
        thread.start()

    def build_context(self):
        """
        Build a context dict from the current client/agent state.
        Returns None if essential state is missing.
        """
        if not self.client.current_profile or not self.client.current_room_hash:
            return None

        profile = self.client.profiles.get(self.client.current_profile, {})
        rooms = profile.get('rooms', {})
        room_links = profile.get('room_links', {})
        ai_state = profile.get('ai_state', {})

        current_hash = self.client.current_room_hash
        room = rooms.get(current_hash, {})

        # Split mapped exits into confirmed-passable vs previously-impassable
        current_links = room_links.get(current_hash, {})
        passable_exits    = [d for d, dest in current_links.items() if dest is not None]
        impassable_exits  = [d for d, dest in current_links.items() if dest is None]

        # Recent actions (last 6 for display, last 16 for loop detection)
        full_log = ai_state.get('action_log', [])
        recent = full_log[-6:]
        loop_log = full_log[-16:]

        # Loop detection — surface patterns the LLM should know about
        loop_warnings = []

        # 1. Room cycle: same room entered repeatedly
        room_visits = [e['room'] for e in loop_log if e.get('action') == 'entered']
        if len(room_visits) >= 4:
            for room_hash, freq in Counter(room_visits).most_common(3):
                if freq >= 3:
                    rname = rooms.get(room_hash, {}).get('name', room_hash[:8])
                    loop_warnings.append(
                        f"You have entered '{rname}' {freq} times in the last "
                        f"{len(room_visits)} moves — possible room cycle."
                    )

        # 2. Direction failure loop: same direction blocked repeatedly
        blocked = [e.get('action') for e in loop_log if e.get('outcome') == 'blocked']
        if len(blocked) >= 3:
            for direction, freq in Counter(blocked).most_common(2):
                if freq >= 3:
                    loop_warnings.append(
                        f"Direction '{direction}' has been tried and blocked "
                        f"{freq} times recently — something is preventing passage."
                    )

        # Otto helper-player state
        agent = self.client.ai_agent
        otto_caps = ai_state.get('otto_capabilities', [])
        otto_present = agent._otto_present() if agent else False
        otto_room = ai_state.get('otto_room')

        # NPC proximity summary — tier NPCs by BFS distance from current room
        npc_danger = ai_state.get('npc_danger', {})
        npc_summary = self._build_npc_summary(
            current_hash, room_links, rooms, npc_danger)

        # Recent MUD text from agent's ring buffer
        recent_mud = list(agent._recent_mud_lines) if agent else []

        in_combat = agent._combat_active if agent else False
        combat_npc = (agent._combat_npc or agent._last_combat_npc) if agent else None

        return {
            'room_name':     room.get('name', 'Unknown'),
            'description':   room.get('description', ''),
            'exits_text':       room.get('exits', ''),
            'passable_exits':   passable_exits,
            'impassable_exits': impassable_exits,
            'rooms_mapped':  len(rooms),
            'rooms_visited': len(ai_state.get('visited', [])),
            'hp':   ai_state.get('current_hp'),
            'mp':   ai_state.get('current_mp'),
            'mv':   ai_state.get('current_mv'),
            'max_hp': ai_state.get('max_hp'),
            'max_mp': ai_state.get('max_mp'),
            'max_mv': ai_state.get('max_mv'),
            'char_level':     ai_state.get('char_level'),
            'char_class':     ai_state.get('char_class'),
            'char_xp':        ai_state.get('char_xp'),
            'char_xp_next':   ai_state.get('char_xp_next'),
            'gold':           ai_state.get('gold'),
            'hunger':         ai_state.get('hunger_level'),
            'thirst':         ai_state.get('thirst_level'),
            'recent_actions': [
                f"{e.get('action')} → {e.get('outcome')}"
                for e in recent
            ],
            'danger_rooms':  list(ai_state.get('danger_rooms', {}).keys()),
            'recent_mud_text': recent_mud[-60:],  # last 60 lines the MUD sent
            'loop_warnings': loop_warnings,
            'npc_summary': npc_summary,
            'current_goal': ai_state.get('current_goal', 'explore'),
            'inventory': ai_state.get('inventory', []),
            'equipment': ai_state.get('equipment', {}),
            'otto_capabilities': otto_caps,
            'otto_present': otto_present,
            'otto_room': otto_room,
            'in_combat': in_combat,
            'combat_npc': combat_npc,
        }

    def _build_npc_summary(self, current_hash, room_links, rooms, npc_danger):
        """
        Build a proximity-tiered NPC summary for the LLM.
        Returns a dict with keys: here, nearby, distant_beatable, dangerous.
        """
        # BFS distances to every room that has a known NPC (max depth 10)
        dist = {current_hash: 0}
        queue = [current_hash]
        while queue:
            node = queue.pop(0)
            if dist[node] >= 10:
                continue
            for dest in room_links.get(node, {}).values():
                if dest and dest not in dist:
                    dist[dest] = dist[node] + 1
                    queue.append(dest)

        here, nearby, distant_beatable, dangerous = [], [], [], []

        for name, rec in npc_danger.items():
            deaths   = rec.get('deaths', 0)
            wins     = rec.get('wins', 0)
            nk       = rec.get('near_kills', 0)
            fast     = rec.get('fastest_death_secs')
            last_room = rec.get('last_room')
            d = dist.get(last_room) if last_room else None  # None = unreachable / unknown

            # Build a compact descriptor
            if deaths > 0:
                desc = f"{name} (DANGEROUS: {deaths} deaths"
                if fast:
                    desc += f", killed us in {fast}s"
                desc += ")"
                dangerous.append((d if d is not None else 999, desc))
            elif wins > 0 or nk > 0:
                desc = f"{name} (wins={wins}, near-kills={nk})"
                if d is None:
                    distant_beatable.append((999, desc))
                elif d == 0:
                    here.append(desc)
                elif d <= 5:
                    nearby.append((d, desc))
                else:
                    distant_beatable.append((d, desc))

        here.sort()
        nearby.sort()
        distant_beatable.sort()
        dangerous.sort()

        return {
            'here':             here,
            'nearby':           [(d, desc) for d, desc in nearby],
            'distant_beatable': distant_beatable,
            'dangerous':        [desc for _, desc in dangerous],
        }

    # ------------------------------------------------------------------
    # Internal — runs in background thread
    # ------------------------------------------------------------------

    def _advice_worker(self, context, on_result):
        """Background worker for request_advice().

        Each call sends: system_prompt + persistent_context + [state_refresh] + current_event.
        Regular advice Q&A is NOT accumulated in self._messages — the LLM's large context
        window makes per-call history unnecessary, and omitting it keeps requests small.
        A full game-state refresh is injected every STATE_REFRESH_INTERVAL calls so the
        LLM stays current without redundant repetition every turn.
        """
        logger = self.client.session_logger
        self._advice_call_count += 1
        self._is_first_message = False

        system_prompt = self._build_system_prompt()
        user_msg = self._build_advisor_message(context)

        # Build per-call message list: persistent context + optional refresh + event
        msgs = list(self._messages)  # initial world knowledge + any direct chat

        if self._advice_call_count % self.STATE_REFRESH_INTERVAL == 1:
            # First call and every Nth call: inject a current game-state block
            state = self._build_game_state_block()
            if state:
                msgs.append({"role": "user",
                             "content": f"[State refresh]\n{state}"})
                msgs.append({"role": "assistant",
                             "content": "Understood, I have your current state."})

        msgs.append({"role": "user", "content": user_msg})

        logger.log_llm_prompt(f"[system]\n{system_prompt}\n[user]\n{user_msg}")

        master = self.client.master
        master.after(0, self.client.begin_advisor_stream)

        def on_token(text):
            master.after(0, lambda t=text: self.client.append_advisor_token(t))

        advice = None
        try:
            advice = self._call_backend(system_prompt, msgs,
                                        max_tokens=1024, on_token=on_token)
            logger.log_llm_response(advice)
            master.after(0, self.client.end_advisor_stream)
        except Exception as e:
            master.after(0, self.client.cancel_advisor_stream)
            msg = str(e)
            master.after(0, lambda m=msg: self.client.append_advisor_text(
                f"[Advisor error: {m}]"))
        master.after(0, lambda: on_result(advice))

    def _direct_worker(self, prompt, on_result):
        """Background worker for request_direct()."""
        logger = self.client.session_logger
        system_prompt = self._build_system_prompt()
        self._messages.append({"role": "user", "content": prompt})
        self._is_first_message = False
        logger.log_llm_prompt(f"[direct]\n{prompt}")

        master = self.client.master
        master.after(0, self.client.begin_advisor_stream)

        def on_token(text):
            master.after(0, lambda t=text: self.client.append_advisor_token(t))

        reply = None
        try:
            reply = self._call_backend(system_prompt, list(self._messages),
                                       max_tokens=1024, on_token=on_token)
            logger.log_llm_response(reply)
            if reply:
                self._messages.append({"role": "assistant", "content": reply})
            master.after(0, self.client.end_advisor_stream)
        except Exception as e:
            if self._messages and self._messages[-1]['role'] == 'user':
                self._messages.pop()
                self._is_first_message = True
            master.after(0, self.client.cancel_advisor_stream)
            msg = str(e)
            master.after(0, lambda m=msg: self.client.append_advisor_text(f"[Advisor error: {m}]"))
        master.after(0, lambda: on_result(reply))

    def _summary_worker(self, assistant_turns, on_result):
        """Background worker for generate_session_summary()."""
        recent = assistant_turns[-30:]
        advice_block = "\n---\n".join(recent)
        system = (
            "You are a session summarizer for a MUD (text RPG) advisor. "
            "Write a detailed briefing (500 words max) for yourself to read at "
            "the START of the next session. Cover: where the player is or was, "
            "every danger or NPC noted (including names and threat level), "
            "active goals and their progress, character state (level, class, stats "
            "if known), any tactical patterns you observed, and anything else that "
            "would help you pick up where you left off without losing context. "
            "Write in third person about the player. Be specific and factual — "
            "this replaces your memory of the session."
        )
        user = (
            "Here is your advisory output from the session that just ended:\n\n"
            + advice_block
            + "\n\nWrite the session summary now."
        )
        summary = None
        try:
            summary = self._call_backend(
                system, [{"role": "user", "content": user}], max_tokens=1000)
            if summary:
                summary = summary.strip()[:8000]
        except Exception:
            pass
        self.client.master.after(0, lambda: on_result(summary))

    def _build_system_prompt(self):
        """Build the system prompt, appending any saved cross-session summary."""
        prompt = ADVISOR_SYSTEM_PROMPT
        if not self.client.current_profile:
            return prompt
        profile = self.client.profiles.get(self.client.current_profile, {})
        ctx = profile.get('advisor_context', {})
        summary = ctx.get('session_summary', '')
        if not summary:
            return prompt
        ts = ctx.get('session_summary_ts', '')[:10]
        n = ctx.get('total_sessions', '')
        label = f"session #{n}, {ts}" if ts else f"session #{n}"
        prompt += (
            f"\n\nContext from your last session ({label}):\n{summary}"
        )
        return prompt

    def _trim_history(self):
        """Drop the oldest turns so the history stays within _history_limit pairs."""
        cap = self._history_limit * 2
        if len(self._messages) > cap:
            self._messages = self._messages[-cap:]

    def _build_game_state_block(self):
        """
        Build a concise structured game-state block from the current profile and
        agent state.  Mirrors the context that the autonomous agent already uses.
        """
        if not self.client.current_profile:
            return ""
        profile = self.client.profiles.get(self.client.current_profile, {})
        ai_state = profile.get('ai_state', {})
        agent = self.client.ai_agent
        parts = []

        # --- Always include: volatile state that changes turn-to-turn ---

        hp  = ai_state.get('current_hp')
        mp  = ai_state.get('current_mp')
        mv  = ai_state.get('current_mv')
        max_hp = ai_state.get('max_hp')
        max_mp = ai_state.get('max_mp')
        max_mv = ai_state.get('max_mv')
        if hp is not None:
            hp_str = f"{hp}/{max_hp}" if max_hp else str(hp)
            mp_str = f"{mp}/{max_mp}" if max_mp else str(mp)
            mv_str = f"{mv}/{max_mv}" if max_mv else str(mv)
            parts.append(f"Stats: {hp_str}HP  {mp_str}MP  {mv_str}MV")

        needs = []
        if ai_state.get('hunger_level'): needs.append(f"hunger:{ai_state['hunger_level']}")
        if ai_state.get('thirst_level'): needs.append(f"thirst:{ai_state['thirst_level']}")
        if needs:
            parts.append("Needs: " + ", ".join(needs))

        in_combat = agent._combat_active if agent else False
        if in_combat:
            npc = (agent._combat_npc or agent._last_combat_npc) if agent else None
            parts.append(f"IN COMBAT with: {npc or 'unknown'}")

        # --- Include when changed since last message ---

        inventory = ai_state.get('inventory', [])
        if inventory != self._last_inventory:
            if inventory:
                parts.append("Carrying: " + ", ".join(inventory))
            elif self._last_inventory:
                parts.append("Carrying: (nothing)")
            self._last_inventory = list(inventory)

        equipment = ai_state.get('equipment', {})
        if equipment != self._last_equipment:
            if equipment:
                parts.append("Equipped: " + "; ".join(f"{s}: {i}" for s, i in equipment.items()))
            self._last_equipment = dict(equipment)

        # --- First message only: static / slowly-changing state ---

        if self._is_first_message:
            char_parts = []
            if ai_state.get('char_level'):  char_parts.append(f"Level {ai_state['char_level']}")
            if ai_state.get('char_class'):  char_parts.append(ai_state['char_class'])
            xp = ai_state.get('char_xp')
            if xp is not None:
                xp_str = f"{xp}/{ai_state['char_xp_next']}" if ai_state.get('char_xp_next') else str(xp)
                char_parts.append(f"XP {xp_str}")
            if ai_state.get('gold') is not None:
                char_parts.append(f"Gold {ai_state['gold']}")
            if char_parts:
                parts.append("Character: " + "  ".join(char_parts))

        return "\n".join(parts)

    def send_initial_context(self):
        """
        Proactively send world knowledge to the LLM right after login, before the
        player types any commands.  Runs in a background thread so the main loop
        is not blocked.
        """
        if not self.is_available():
            return
        self._call_count += 1
        thread = threading.Thread(
            target=self._initial_context_worker,
            daemon=True,
            name=f"llm-init-{self._call_count}"
        )
        thread.start()

    def _initial_context_worker(self):
        """Background worker for send_initial_context()."""
        if not self.client.current_profile:
            return
        profile = self.client.profiles.get(self.client.current_profile, {})
        ai_state = profile.get('ai_state', {})
        world = self._build_world_knowledge_block(profile, ai_state)
        if not world:
            return
        system_prompt = self._build_system_prompt()
        user_msg = (
            "Before we begin, here is everything known about this world from "
            "previous sessions. Use this to inform your advice throughout our session.\n\n"
            + world
        )
        self._messages.append({"role": "user", "content": user_msg})
        self._is_first_message = False
        logger = self.client.session_logger
        logger.log_llm_prompt(f"[init]\n{user_msg}")
        try:
            reply = self._call_backend(system_prompt, list(self._messages), max_tokens=256)
            if reply:
                logger.log_llm_response(reply)
                self._messages.append({"role": "assistant", "content": reply})
        except Exception:
            # Roll back so the next real message retries as first
            if self._messages and self._messages[-1]['role'] == 'user':
                self._messages.pop()
                self._is_first_message = True

    def _build_world_knowledge_block(self, profile, ai_state):
        """
        Build a comprehensive world-knowledge block.
        Combines combat history (npc_danger) with the broader mob sighting
        database (mob_db) and the full room roster.
        """
        rooms = profile.get('rooms', {})
        parts = []

        # --- Mob database: merge npc_danger (combat stats) + mob_db (sightings) ---
        npc_danger = ai_state.get('npc_danger', {})
        mob_db     = profile.get('mob_db', {})

        all_mob_names = sorted(set(list(npc_danger.keys()) + list(mob_db.keys())))
        if all_mob_names:
            mob_lines = []
            for name in all_mob_names:
                danger = npc_danger.get(name, {})
                db     = mob_db.get(name, {})

                deaths     = danger.get('deaths', 0)
                wins       = danger.get('wins', 0)
                near_kills = danger.get('near_kills', 0)
                fast       = danger.get('fastest_death_secs')
                sightings  = db.get('total_sightings', 0)
                is_wanderer = db.get('is_wanderer', False)
                display    = db.get('display_name', name)

                # Last known room (prefer combat record, fall back to mob_db)
                last_room = danger.get('last_room') or db.get('last_room')
                room_name = rooms.get(last_room, {}).get('name', '') if last_room else ''

                xp_total = danger.get('xp_total', 0)
                xp_kills = danger.get('xp_kills', 0)

                stats = []
                if deaths:
                    stats.append(f"KILLED US {deaths}x")
                    if fast:
                        stats.append(f"fastest kill: {fast}s")
                if wins:
                    avg_xp = (xp_total // xp_kills) if xp_kills else None
                    win_str = f"we won {wins}x"
                    if avg_xp:
                        win_str += f" (~{avg_xp} xp/kill)"
                    stats.append(win_str)
                if near_kills:
                    stats.append(f"near-kills: {near_kills}")
                if sightings and not deaths and not wins:
                    stats.append(f"seen {sightings}x")
                if is_wanderer:
                    stats.append("wanderer")
                if room_name:
                    stats.append(f"last seen: {room_name}")

                label = display if display != name else name
                mob_lines.append(
                    f"  {label}: " + ", ".join(stats) if stats else f"  {label}: observed"
                )

            parts.append("Known mobs:\n" + "\n".join(mob_lines))

        # --- Room roster ---
        room_links = profile.get('room_links', {})
        current_hash = self.client.current_room_hash
        if rooms:
            room_lines = []
            for h, room in rooms.items():
                name = room.get('name', '?')
                links = room_links.get(h, {})
                passable = [d for d, dest in links.items() if dest is not None]
                exit_str = ", ".join(passable) if passable else (room.get('exits', '') or "none")
                marker = " <-- YOU ARE HERE" if h == current_hash else ""
                room_lines.append(f"  {name} [exits: {exit_str}]{marker}")

            parts.append(f"Known rooms ({len(rooms)} mapped):\n" + "\n".join(room_lines))

        return "\n\n".join(parts)

    def _build_advisor_message(self, context):
        """Format one or more command+response events into the advisor user-turn message."""
        events = context.get('events', [])
        parts = []

        if len(events) == 1:
            ev = events[0]
            parts.append(f'Player typed: "{ev["command"]}"')
            parts.append("")
            parts.append("MUD response:")
            mud = '\n'.join(ev.get('mud_lines', [])).strip()
            parts.append(mud if mud else "(no response)")
        else:
            parts.append(
                f"The following {len(events)} commands were sent since the last advice:"
            )
            for i, ev in enumerate(events, 1):
                parts.append(f"\n[{i}] Player typed: \"{ev['command']}\"")
                mud = '\n'.join(ev.get('mud_lines', [])).strip()
                parts.append("MUD response:")
                parts.append(mud if mud else "(no response)")

        room_name = context.get('room_name', '')
        room_exits = context.get('room_exits', '')
        if room_name:
            parts.append(f"\nCurrent room: {room_name}")
        if room_exits:
            parts.append(f"Exits: {room_exits}")

        game_state = self._build_game_state_block()
        if game_state:
            parts.append("\n" + game_state)

        return "\n".join(parts)

    def _worker(self, context, on_result):
        """Background worker for request_action() (legacy autonomous-play API)."""
        logger = self.client.session_logger
        user_msg = self._build_user_message(context)
        logger.log_llm_prompt(f"[system]\n{ACTION_SYSTEM_PROMPT}\n[user]\n{user_msg}")
        try:
            raw_response = self._call_backend(
                ACTION_SYSTEM_PROMPT,
                [{"role": "user", "content": user_msg}],
                max_tokens=2048
            )
            logger.log_llm_response(raw_response)
            command = self._sanitize(raw_response)
        except Exception as e:
            msg = str(e)
            self.client.master.after(
                0, lambda m=msg: self.client.append_text(
                    f"[AI/LLM] Error: {m}\n", "error"))
            command = None

        # Deliver result on the main thread
        self.client.master.after(0, lambda: on_result(command))

    def _call_backend(self, system_prompt, messages, max_tokens=2048, on_token=None):
        """
        Dispatch to the configured backend.

        messages  — list of {role, content} dicts (system prompt passed separately)
        on_token  — optional callable(str) invoked for each streamed chunk;
                    when provided the backends stream rather than buffer
        """
        cfg = self._config()
        if cfg is None:
            raise RuntimeError("No LLM configured. Add ai_config to profile.")
        backend = cfg.get('llm_backend', 'ollama').lower()
        if backend == 'ollama':
            return self._call_ollama(cfg, system_prompt, messages, max_tokens, on_token)
        elif backend == 'claude':
            return self._call_claude(cfg, system_prompt, messages, max_tokens, on_token)
        else:
            raise RuntimeError(f"Unknown llm_backend: {backend!r}")

    def _build_user_message(self, context):
        """Format context into the user-turn message."""
        parts = [
            f"Room: {context['room_name']}",
            f"Description: {context['description']}",
            f"Exits reported by MUD: {context['exits_text']}",
        ]
        if context['passable_exits']:
            parts.append(f"Exits confirmed passable: {', '.join(context['passable_exits'])}")
        if context['impassable_exits']:
            parts.append(
                f"Exits previously impassable: {', '.join(context['impassable_exits'])}"
                " (may require an item, key, or special action to pass)"
            )

        # Character stats
        hp, mp, mv = context.get('hp'), context.get('mp'), context.get('mv')
        max_hp, max_mp, max_mv = context.get('max_hp'), context.get('max_mp'), context.get('max_mv')
        if hp is not None:
            hp_str = f"{hp}/{max_hp}" if max_hp else str(hp)
            mp_str = f"{mp}/{max_mp}" if max_mp else str(mp)
            mv_str = f"{mv}/{max_mv}" if max_mv else str(mv)
            parts.append(f"Stats: {hp_str}HP  {mp_str}MP  {mv_str}MV")

        # Character sheet
        char_parts = []
        if context.get('char_level'):   char_parts.append(f"Level {context['char_level']}")
        if context.get('char_class'):   char_parts.append(context['char_class'])
        if context.get('char_xp') is not None:
            xp_str = str(context['char_xp'])
            if context.get('char_xp_next'):
                xp_str += f"/{context['char_xp_next']}"
            char_parts.append(f"XP {xp_str}")
        if context.get('gold') is not None: char_parts.append(f"Gold {context['gold']}")
        if char_parts:
            parts.append("Character: " + "  ".join(char_parts))

        if context.get('in_combat'):
            npc = context.get('combat_npc') or 'unknown'
            parts.append(f"IN COMBAT with: {npc} — movement blocked; use flee or non-movement commands only.")

        goal = context.get('current_goal', 'explore')
        if goal != 'explore':
            parts.append(f"Current goal: {goal}")

        equipment = context.get('equipment', {})
        if equipment:
            eq_parts = [f"{slot}: {item}" for slot, item in equipment.items()]
            parts.append("Equipped: " + "; ".join(eq_parts))

        inventory = context.get('inventory', [])
        if inventory:
            parts.append("Carrying: " + ", ".join(inventory))

        # Survival state
        needs = []
        if context.get('hunger'): needs.append(f"hunger:{context['hunger']}")
        if context.get('thirst'): needs.append(f"thirst:{context['thirst']}")
        if needs:
            parts.append("Needs: " + ", ".join(needs))

        # Otto — only show when relevant to current goal
        goal = context.get('current_goal', 'explore')
        food_goals = {'get_food', 'earn_gold', 'explore', 'seek_mobs', 'hunt_mob'}
        otto_caps = context.get('otto_capabilities', [])
        if goal not in food_goals and otto_caps:
            cap_str = ', '.join(f'tell otto {c}' for c in otto_caps)
            presence = "is here" if context.get('otto_present') else "is available remotely"
            parts.append(f"Otto {presence}. Commands: {cap_str}.")

        # NPC knowledge — proximity-tiered, most relevant first
        ns = context.get('npc_summary', {})
        if ns.get('here'):
            parts.append("Beatable NPCs HERE: " + "; ".join(ns['here']))
        if ns.get('nearby'):
            nearby_strs = [f"{desc} [{d} steps]" for d, desc in ns['nearby']]
            parts.append("Beatable NPCs nearby: " + "; ".join(nearby_strs))
        if ns.get('distant_beatable'):
            distant_strs = [f"{desc} [{d} steps]" for d, desc in ns['distant_beatable']]
            parts.append("Beatable NPCs further away: " + "; ".join(distant_strs))
        if ns.get('dangerous'):
            parts.append("AVOID: " + "; ".join(ns['dangerous']))

        if context['recent_actions']:
            parts.append("Recent actions: " + " | ".join(context['recent_actions']))

        parts.append(
            f"Map progress: {context['rooms_visited']} visited / "
            f"{context['rooms_mapped']} mapped"
        )

        # Recent MUD text — the raw stream the player sees
        if context.get('recent_mud_text'):
            parts.append("\nRecent MUD output (last lines received):")
            parts.append("\n".join(context['recent_mud_text']))

        # Loop warnings — explicit signal that something is stuck
        if context.get('loop_warnings'):
            parts.append("\nWARNING — LOOP DETECTED:")
            for w in context['loop_warnings']:
                parts.append(f"  - {w}")
            parts.append("Please choose a different action to break out of this loop.")

        # Goal-specific guidance
        goal = context.get('current_goal', 'explore')
        if goal == 'get_food':
            parts.append(
                "\nTASK: Find a food shop by exploring. "
                "Move to an unexplored room — do not interact with Otto, he cannot provide food."
            )
        elif goal == 'get_water':
            parts.append(
                "\nTASK: Find a fountain or water source by exploring. "
                "Move to an unexplored room."
            )
        elif goal == 'earn_gold':
            parts.append("\nTASK: Find and kill a beatable monster to earn gold.")

        # parts.append("\nWhat single command should I enter next?")
        parts.append("\nRecommend a plan of action and finish with a single command as the last line.")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Ollama backend  (OpenAI-compatible chat completions)
    # ------------------------------------------------------------------

    def _ollama_conn(self, cfg):
        """Return (conn, host_str, use_ssl) for the configured Ollama endpoint."""
        endpoint = cfg.get('llm_endpoint', 'http://localhost:11434')
        parsed = urllib.parse.urlparse(endpoint)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        use_ssl = parsed.scheme == 'https'
        if use_ssl:
            return http.client.HTTPSConnection(host, port, timeout=180), endpoint, True
        return http.client.HTTPConnection(host, port, timeout=180), endpoint, False

    def check_ollama_model(self, cfg):
        """
        Verify the configured model is available in Ollama.
        Returns (ok: bool, message: str).
        """
        model = cfg.get('llm_model', 'llama3.1:8b')
        conn, endpoint, _ = self._ollama_conn(cfg)
        try:
            conn.request("GET", "/api/tags")
            resp = conn.getresponse()
            raw = resp.read().decode('utf-8')
            conn.close()
        except Exception as e:
            return False, (
                f"Cannot reach Ollama at {endpoint}.\n"
                f"Make sure Ollama is running ('ollama serve').\nError: {e}"
            )
        if resp.status != 200:
            return False, f"Ollama returned HTTP {resp.status}"
        data = json.loads(raw)
        available = [m.get('name', '') for m in data.get('models', [])]
        # Ollama model names may include a tag like "gemma2:27b" or just "gemma2"
        if any(m == model or m.startswith(model.split(':')[0]) for m in available):
            return True, f"Model {model!r} is ready."
        return False, (
            f"Model {model!r} is not downloaded yet.\n"
            f"Run:  ollama pull {model}\n"
            f"Available models: {', '.join(available) or 'none'}"
        )

    def _call_ollama(self, cfg, system_prompt, messages, max_tokens=2048, on_token=None):
        endpoint = cfg.get('llm_endpoint', 'http://localhost:11434')
        model = cfg.get('llm_model', 'llama3.1:8b')

        parsed = urllib.parse.urlparse(endpoint)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        use_ssl = parsed.scheme == 'https'

        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}] + messages,
            "temperature": 0.5,
            "max_tokens": max_tokens,
            "stream": on_token is not None,
            "options": {"num_ctx": 131072},
        }

        body = json.dumps(payload).encode('utf-8')
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }

        if use_ssl:
            conn = http.client.HTTPSConnection(host, port, timeout=180)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=180)

        try:
            conn.request("POST", "/v1/chat/completions", body=body, headers=headers)
            resp = conn.getresponse()

            if resp.status != 200:
                raw = resp.read().decode('utf-8')
                raise RuntimeError(f"Ollama returned HTTP {resp.status}: {raw[:200]}")

            if on_token is None:
                # Non-streaming: read full response at once
                data = json.loads(resp.read().decode('utf-8'))
                return data['choices'][0]['message']['content']

            # Streaming: read SSE lines
            accumulated = []
            while True:
                line = resp.readline()
                if not line:
                    break
                line = line.decode('utf-8', errors='replace').strip()
                if not line or not line.startswith('data: '):
                    continue
                data_str = line[6:]
                if data_str == '[DONE]':
                    break
                try:
                    chunk = json.loads(data_str)
                    content = chunk['choices'][0]['delta'].get('content') or ''
                    if content:
                        accumulated.append(content)
                        on_token(content)
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
            return ''.join(accumulated)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Claude backend
    # ------------------------------------------------------------------

    def _call_claude(self, cfg, system_prompt, messages, max_tokens=1024, on_token=None):
        api_key = cfg.get('claude_api_key', '')
        if not api_key:
            raise RuntimeError("claude_api_key is not set in profile ai_config.")

        model = cfg.get('claude_model', 'claude-haiku-4-5-20251001')

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": messages,
            "stream": on_token is not None,
        }

        body = json.dumps(payload).encode('utf-8')
        headers = {
            "Content-Type":      "application/json",
            "Content-Length":    str(len(body)),
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
        }

        conn = http.client.HTTPSConnection("api.anthropic.com", timeout=180)
        try:
            conn.request("POST", "/v1/messages", body=body, headers=headers)
            resp = conn.getresponse()

            if resp.status != 200:
                raw = resp.read().decode('utf-8')
                raise RuntimeError(f"Claude API returned HTTP {resp.status}: {raw[:200]}")

            if on_token is None:
                # Non-streaming: read full response at once
                data = json.loads(resp.read().decode('utf-8'))
                return data['content'][0]['text']

            # Streaming: read SSE events
            accumulated = []
            event_type = None
            while True:
                line = resp.readline()
                if not line:
                    break
                line = line.decode('utf-8', errors='replace').rstrip('\r\n')
                if line.startswith('event: '):
                    event_type = line[7:].strip()
                elif line.startswith('data: '):
                    if event_type == 'content_block_delta':
                        try:
                            data = json.loads(line[6:])
                            text = data.get('delta', {}).get('text', '')
                            if text:
                                accumulated.append(text)
                                on_token(text)
                        except (json.JSONDecodeError, KeyError):
                            pass
                    elif event_type == 'message_stop':
                        break
                elif not line:
                    event_type = None
            return ''.join(accumulated)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sanitize(self, text):
        """
        Strip the LLM response down to a single clean command.
        LLMs sometimes wrap their answer in quotes, explanation, or markdown.
        The prompt asks them to "finish with a single command as the last line",
        often preceded by "Command:" label.
        """
        if not text:
            return None
        
        lines = text.strip().splitlines()
        if not lines:
            return None
        
        # Look for "Command:" label (case-insensitive), with optional markdown formatting
        line = None
        for i, l in enumerate(lines):
            l_stripped = l.strip()
            # Check if this line contains "Command:" (possibly with markdown bold ** around it)
            if re.match(r'^\*{0,2}command\s*:\*{0,2}\s*', l_stripped, re.IGNORECASE):
                # Check if command is on the same line after the colon
                remainder = re.sub(r'^\*{0,2}command\s*:\*{0,2}\s*', '', l_stripped, flags=re.IGNORECASE).strip()
                if remainder:
                    line = remainder
                    break
                # Otherwise, take the next non-empty line
                for j in range(i + 1, len(lines)):
                    next_line = lines[j].strip()
                    if next_line:
                        line = next_line
                        break
                break
        
        # If no "Command:" label found, use the last non-empty line
        if line is None:
            for l in reversed(lines):
                l_stripped = l.strip()
                if l_stripped:
                    line = l_stripped
                    break
        
        if not line:
            return None
        
        # Remove markdown formatting (bold/italic asterisks)
        line = re.sub(r'\*+', '', line)
        # Remove surrounding quotes
        line = line.strip('"\'`')
        # Remove common prefixes the model might add
        line = re.sub(
            r'^(?:command|action|i (?:would |will |should )?(?:type|enter|say)|>)\s*[:\-]?\s*',
            '', line, flags=re.IGNORECASE
        ).strip()
        # Reject anything too long to be a valid MUD command (>60 chars)
        if not line or len(line) > 60:
            return None
        return line

    def _config(self):
        if not self.client.current_profile:
            return None
        profile = self.client.profiles.get(self.client.current_profile, {})
        return profile.get('ai_config')
