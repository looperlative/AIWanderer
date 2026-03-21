# Copyright (c) 2026 Bob Amstadt
# SPDX-License-Identifier: MIT
"""
LLM Advisor for AIWanderer.

Calls a local Ollama instance (or the Claude API) to decide what to do
in situations the rule-based explorer can't handle — unexplored areas
that require non-movement commands, NPC interaction, puzzle text, etc.

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
import time
import urllib.parse


SYSTEM_PROMPT = """You are an AI player in a MUD (Multi-User Dungeon), a text-based \
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
    Calls an LLM to suggest a MUD command given the current game state.

    All network I/O runs in a background thread so the tkinter main loop
    is never blocked.  Results are delivered via a callback scheduled on
    the main thread with master.after(0, ...).
    """

    MIN_CALL_INTERVAL = 8.0   # seconds between LLM calls (rate limit)

    def __init__(self, client):
        self.client = client          # MUDClient instance
        self._last_call_time = 0.0
        self._call_in_flight = False
        self._call_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self):
        """Return True if an LLM is configured for this profile."""
        cfg = self._config()
        return cfg is not None and bool(cfg.get('llm_backend'))

    def is_ready(self):
        """Return True if enough time has passed since the last call."""
        return (not self._call_in_flight and
                time.monotonic() - self._last_call_time >= self.MIN_CALL_INTERVAL)

    def request_action(self, context, on_result):
        """
        Asynchronously request an action from the LLM.

        context   — dict produced by build_context()
        on_result — callable(command: str | None) called on the main thread
        """
        if not self.is_ready():
            on_result(None)
            return

        self._call_in_flight = True
        self._last_call_time = time.monotonic()
        self._call_count += 1

        thread = threading.Thread(
            target=self._worker,
            args=(context, on_result),
            daemon=True,
            name=f"llm-advisor-{self._call_count}"
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
            'recent_mud_text': recent_mud[-20:],  # last 20 lines the MUD sent
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

    def _worker(self, context, on_result):
        logger = self.client.session_logger
        prompt = self._build_user_message(context)
        logger.log_llm_prompt(f"[system]\n{SYSTEM_PROMPT}\n[user]\n{prompt}")
        try:
            raw_response = self._call_llm(context)
            logger.log_llm_response(raw_response)
            command = self._sanitize(raw_response)
        except Exception as e:
            msg = str(e)
            self.client.master.after(
                0, lambda m=msg: self.client.append_text(
                    f"[AI/LLM] Error: {m}\n", "error"))
            command = None
        finally:
            self._call_in_flight = False

        # Deliver result on the main thread
        self.client.master.after(0, lambda: on_result(command))

    def _call_llm(self, context):
        """Dispatch to the appropriate backend."""
        cfg = self._config()
        if cfg is None:
            raise RuntimeError("No LLM configured. Add ai_config to profile.")

        backend = cfg.get('llm_backend', 'ollama').lower()
        if backend == 'ollama':
            return self._call_ollama(cfg, context)
        elif backend == 'claude':
            return self._call_claude(cfg, context)
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
            nearby_strs = [f"{desc} [{d} steps]" for d, desc in ns['nearby'][:4]]
            parts.append("Beatable NPCs nearby: " + "; ".join(nearby_strs))
        if ns.get('distant_beatable'):
            count = len(ns['distant_beatable'])
            nearest_d, nearest_desc = ns['distant_beatable'][0]
            parts.append(
                f"Beatable NPCs further away: {count} known"
                + (f"; nearest: {nearest_desc} [{nearest_d} steps]"
                   if nearest_d < 999 else "")
            )
        if ns.get('dangerous'):
            parts.append("AVOID: " + "; ".join(ns['dangerous'][:5]))

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
            return http.client.HTTPSConnection(host, port, timeout=30), endpoint, True
        return http.client.HTTPConnection(host, port, timeout=30), endpoint, False

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

    def _call_ollama(self, cfg, context):
        endpoint = cfg.get('llm_endpoint', 'http://localhost:11434')
        model = cfg.get('llm_model', 'llama3.1:8b')

        parsed = urllib.parse.urlparse(endpoint)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        use_ssl = parsed.scheme == 'https'

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": self._build_user_message(context)},
            ],
            # "temperature": 0.3,
            "temperature": 0.5,
            # "max_tokens": 60,
            "max_tokens": 2048,
            "stream": False,
            "options": {"num_ctx": 8192},
        }

        body = json.dumps(payload).encode('utf-8')
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }

        if use_ssl:
            conn = http.client.HTTPSConnection(host, port, timeout=30)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=30)

        try:
            conn.request("POST", "/v1/chat/completions", body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode('utf-8')
        finally:
            conn.close()

        if resp.status != 200:
            raise RuntimeError(f"Ollama returned HTTP {resp.status}: {raw[:200]}")

        data = json.loads(raw)
        # Return the full content - let _sanitize() handle extraction
        return data['choices'][0]['message']['content']

    # ------------------------------------------------------------------
    # Claude backend
    # ------------------------------------------------------------------

    def _call_claude(self, cfg, context):
        api_key = cfg.get('claude_api_key', '')
        if not api_key:
            raise RuntimeError("claude_api_key is not set in profile ai_config.")

        model = cfg.get('claude_model', 'claude-haiku-4-5-20251001')

        payload = {
            "model": model,
            "max_tokens": 60,
            "system": SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": self._build_user_message(context)},
            ],
        }

        body = json.dumps(payload).encode('utf-8')
        headers = {
            "Content-Type":      "application/json",
            "Content-Length":    str(len(body)),
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
        }

        conn = http.client.HTTPSConnection("api.anthropic.com", timeout=30)
        try:
            conn.request("POST", "/v1/messages", body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode('utf-8')
        finally:
            conn.close()

        if resp.status != 200:
            raise RuntimeError(f"Claude API returned HTTP {resp.status}: {raw[:200]}")

        data = json.loads(raw)
        return data['content'][0]['text']

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
