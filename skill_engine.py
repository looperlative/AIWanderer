# Copyright (c) 2026 Bob Amstadt
# SPDX-License-Identifier: MIT
"""
Skill engine for AIWanderer.

A "skill" is a named, reusable task the user can hand to the LLM (e.g.
"kill a white rook"). When active, the engine feeds the LLM a running
stream of MUD output and watched character stats, and the LLM replies
with structured JSON containing the next MUD commands plus a flag
indicating whether the task is complete.

Configuration (stored per-profile in profile["skills"][name]):
    {
        "instructions": "<free-form strategy the LLM follows>",
        "watch_stats":  ["hp", "max_hp", "mp", "mv"]
    }

The LLM backend (endpoint, model, api key) is reused from the advisor's
profile["ai_config"] so there is one place to configure.
"""

import json
import random
import re
import threading
import time

from llm_advisor import LLMAdvisor


# Keep the last N user+assistant turn pairs in the rolling history.
# Bounds recency drift: the system prompt stays a constant fraction of
# context instead of shrinking as the session runs.
HISTORY_TURN_PAIRS = 3

# Emit a group-combat snapshot into the LLM context every N turns.
BATTLE_SNAPSHOT_TURNS = 5

# Attack-verb lines collapsed during combat turns. Anything not matching
# these (HP lines, kill lines, tells, room titles, non-target mob
# mentions) is preserved verbatim.
_COMBAT_VERB_RE = re.compile(
    r'\b(?:hits?|miss(?:es)?|slash(?:es)?|crush(?:es)?|pierces?|stabs?|'
    r'bash(?:es)?|strikes?|claws?|bites?|punch(?:es)?|kicks?|mauls?|'
    r'slices?|grazes?|pounds?|smites?|parr(?:y|ies)|dodges?)\b',
    re.IGNORECASE)


SKILL_SYSTEM_PROMPT = """You are an agent controlling a character in a text-based MUD.
The user has given you a named skill to execute from start to finish.

You will receive, each turn:
  - The skill's instructions (your playbook).
  - Recent MUD output since your last command.
  - A snapshot of the watched character stats the skill cares about.
  - Optional flags such as whether a rescue just fired.

You must reply with a single JSON object and NOTHING ELSE. Schema:

  {
    "commands": ["mud command 1", "mud command 2", ...],
    "complete": false,
    "switch_skill": null,
    "plan_step": "the identifier of the step you are now executing",
    "note": "one short sentence about what you're doing"
  }

Rules:
  - `commands` is a list of literal MUD commands to send, in order. Empty list
    means "wait one tick and re-evaluate" — valid and normal.
  - Each command must be a real MUD input line (e.g. "kill rook", "cast bless",
    "n", "tell otto heal"). Do not include prose, quotes, numbering, or prefixes.
  - Set `complete` to true ONLY when the plan's "done" step is reached: all
    prior steps completed. Follow the SKILL PLAN in the user message.
  - `switch_skill`: set to the exact name of another available skill to stop
    this skill and immediately start that one. Set to null (the default) to
    continue running. Use this when a fundamentally different task is needed
    (e.g. the default skill detects hunger and switches to `acquire_food`).
    The new skill starts fresh. `switch_skill` and `complete` are mutually
    exclusive — if `switch_skill` is set, `complete` is ignored.
  - `plan_step`: set to the EXACT identifier string of the step you are
    currently executing, copied verbatim from the SKILL PLAN in the user
    message. Do NOT invent, abbreviate, or paraphrase step names — only the
    identifiers listed in the plan are valid. If you advance this turn, emit
    the NEW step's identifier. The harness tracks position using exact matches.
  - Rescues: if the turn payload says a rescue fired, the harness has already
    reset your plan step to the rescue_restart_step. Read the current plan step
    from the SKILL PLAN section and follow it.
  - `note` is a short status string shown in the UI; keep it under 80 chars.
  - CRITICAL: Respond with the raw JSON object and NOTHING ELSE. No markdown,
    no ```json fences, no commentary. The first character of your reply must
    be '{' and the last must be '}'.

Movement:
  - When a skill's instructions give you a speedwalk string (e.g. "5n4w4s"),
    send the whole string as ONE command in a single turn. Speedwalks are
    executed atomically by the MUD and halt automatically on combat, so you
    do not need to step by step.
  - Only fall back to per-step movement when the instructions explicitly
    tell you to, or when recovering from an interruption where a speedwalk
    is not appropriate.

Goto (navigate to a known location):
  - Use goto:<target> to move somewhere without knowing the exact path.
    goto:vnum:123       — room by number
    goto:Otto           — landmark named "Otto" (a configured fixed location)
    goto:mob:white rook — room where that mob was last seen
    goto:Temple Square  — room whose name contains "Temple Square"
  - The harness resolves the target via BFS and injects the directions
    automatically. If unreachable, a warning is shown and the command is
    skipped. Combine freely: ["goto:Otto", "tell otto heal"].
  - While navigating, each turn's MUD output begins with:
      [Harness: goto:<target> in progress]   — still moving
      [Harness: goto:<target> arrived — <room>]  — destination reached
    Wait for the "arrived" line before acting on the destination.

Command ledger:
  - Each turn's payload includes "Commands sent this skill session so far"
    with per-command counts and the last several commands. This is the
    AUTHORITATIVE record of what has been dispatched — trust it over your
    own memory when deciding whether something was already done (buff sent,
    speedwalk already fired, etc.).
"""


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

# Plan markdown regexes.
# _PLAN_STEP_RE matches a single step line and captures its components for re-rendering.
# _PLAN_STEP_ID_RE extracts step identifiers in order from a plan markdown string.
_PLAN_STEP_RE = re.compile(r'^(\s*-\s*\[)[ xX](\]\s*)(\w+)(\s*:.*)')
_PLAN_STEP_ID_RE = re.compile(r'^\s*-\s*\[[ xX]\]\s*(\w+)\s*:', re.MULTILINE)

# Extract the entity name that follows "the" near the end of an attack line.
# Captures everything between "the " and the terminal punctuation so we can
# strip trailing intensity words in a second pass.
_MOB_THE_RE = re.compile(r'\bthe\s+([a-z][a-z\s]{0,29}?)[.!]\s*$', re.IGNORECASE)

# Strip trailing intensity phrases from a raw mob-name candidate.
# Ordered longest-first so "very hard" is removed before a bare "hard" pass.
_MOB_INTENSITY_RE = re.compile(
    r'\s+(?:very|extremely|quite|rather|exceedingly)\s+\w+$'
    r'|\s+(?:hard|lightly|softly|barely|savagely|viciously|brutally|effortlessly)$',
    re.IGNORECASE)

# Matches the MUD prompt line to extract tank% and opp% directly from the buffer.
# Example: "209H 100M 113V 100%T 97%O >"
_PROMPT_COMBAT_RE = re.compile(
    r'\b\d+H\s+\d+M\s+\d+V\s+(\d+)%T\s+(\d+)%O\s*>',
    re.IGNORECASE
)


def _parse_plan_steps(text):
    """Return ordered list of step identifiers from plan markdown string."""
    return _PLAN_STEP_ID_RE.findall(text or "")


def render_skill(template_cfg, params):
    """Return a new skill cfg dict with `{{name}}` placeholders in `instructions`
    and `reminders` substituted from `params`.

    Copies non-templated fields (`watch_stats`, etc.) through unchanged. Raises
    `KeyError` listing every placeholder referenced in the template text that
    wasn't supplied in `params`, so callers can surface a useful error.
    """
    out = dict(template_cfg or {})
    out.pop("placeholders", None)
    missing = set()

    def sub(match):
        key = match.group(1)
        if key not in params:
            missing.add(key)
            return match.group(0)
        return str(params[key])

    for field in ("instructions", "reminders"):
        text = template_cfg.get(field) if template_cfg else None
        if isinstance(text, str) and text:
            out[field] = _PLACEHOLDER_RE.sub(sub, text)

    raw_plan = template_cfg.get("plan") if template_cfg else None
    if isinstance(raw_plan, str) and raw_plan:
        out["plan"] = _PLACEHOLDER_RE.sub(sub, raw_plan)
    elif isinstance(raw_plan, list):
        # Legacy JSON array format — convert to markdown on the fly.
        lines = []
        for step_obj in raw_plan:
            step_id = step_obj.get("step", "?")
            desc = _PLACEHOLDER_RE.sub(sub, step_obj.get("description", ""))
            lines.append(f"- [ ] {step_id}: {desc}")
        out["plan"] = "\n".join(lines)

    if missing:
        raise KeyError(
            "Missing skill template parameters: " + ", ".join(sorted(missing)))
    return out


class SkillEngine:
    """
    Runs an LLM-driven skill to completion.

    Threading mirrors LLMAdvisor: LLM I/O happens in a background thread
    per turn, and results are delivered on the Tk main thread via
    master.after(0, on_result).
    """

    def __init__(self, client):
        self.client = client
        self._call_count = 0
        # Active-session state (None when idle)
        self._skill_name = None
        self._skill_cfg = None
        self._messages = []       # rolling conversation for this skill session
        self._busy = False
        self._pending = False     # a turn arrived while busy; fire one more when idle
        self._cmd_history = []    # every command the engine has dispatched this session
        self._plan_step = None    # current plan step identifier (tracked each turn)
        self._plan_steps = []     # ordered step IDs parsed from plan markdown
        self._deferred_rescue = False  # rescue flag preserved when pending turn is skipped
        self._battle_attacked_mobs = set()           # mob names hit since last snapshot
        self._battle_attacked_pcs  = set()           # PC names hit since last snapshot
        self._battle_turns_since_snap = 0
        self._battle_snapshot_inflight = False       # True while a snapshot is in an undelivered LLM call
        self._injected_user_message = None
        self._idle_since = None             # timestamp when idle began; None while active

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def is_active(self):
        return self._skill_name is not None

    def active_name(self):
        return self._skill_name

    def start(self, name, cfg):
        """Begin a new skill session. Discards any prior in-flight state."""
        self._skill_name = name
        self._skill_cfg = dict(cfg or {})
        self._messages = []
        self._busy = False
        self._pending = False
        self._cmd_history = []
        plan = self._skill_cfg.get("plan", "")
        self._plan_steps = _parse_plan_steps(plan) if isinstance(plan, str) else []
        self._plan_step = self._plan_steps[0] if self._plan_steps else None
        self._deferred_rescue = False
        self._battle_attacked_mobs = set()
        self._battle_attacked_pcs  = set()
        self._battle_turns_since_snap = 0
        self._battle_snapshot_inflight = False
        self._injected_user_message = None
        self._idle_since = None

    def stop(self):
        """Cancel the active skill. In-flight LLM replies are discarded."""
        self._skill_name = None
        self._skill_cfg = None
        self._messages = []
        self._busy = False
        self._pending = False
        self._cmd_history = []
        self._plan_step = None
        self._plan_steps = []
        self._deferred_rescue = False
        self._battle_attacked_mobs = set()
        self._battle_attacked_pcs  = set()
        self._battle_turns_since_snap = 0
        self._battle_snapshot_inflight = False
        self._injected_user_message = None
        self._idle_since = None

    def inject_user_message(self, text: str):
        """Queue a freeform user instruction to prepend to the next skill turn."""
        self._injected_user_message = text

    def record_dispatched(self, commands):
        """Called by the client after it sends commands returned by a turn."""
        for c in commands:
            if c:
                self._cmd_history.append(str(c))

    # ------------------------------------------------------------------
    # Turn trigger
    # ------------------------------------------------------------------

    def on_prompt(self, mud_lines, stats, combat_mob, rescue_just_fired, on_result,
                  target_killed=False):
        """
        Signal that a MUD prompt just arrived. Builds the next LLM turn and
        fires a background request. If a prior turn is still in flight, sets
        a "pending" flag; the engine will fire exactly one more turn when
        that one returns.

        on_result — callable(result_dict, skill_name_at_fire_time) on main thread.
                    result_dict is {commands, complete, note} or None on error.
        """
        if not self.is_active():
            return
        # Merge in any rescue flag that was preserved when a commands-sent turn
        # skipped the immediate pending chain (so it isn't lost).
        rescue_just_fired = rescue_just_fired or self._deferred_rescue
        self._deferred_rescue = False
        if self._busy:
            # OR-merge sticky flags so a rescue or kill signal raised while
            # an earlier turn is in flight isn't lost when the payload is
            # overwritten by a later, flag-less trigger.
            # Accumulate mud_lines so earlier output (e.g. room description)
            # is not discarded when a later prompt (e.g. auto-score) arrives
            # before the in-flight LLM call completes.
            prev_mud_lines = []
            prev_rescue = False
            prev_killed = False
            if self._pending and self._pending_payload is not None:
                prev_mud_lines, _, _, prev_rescue, prev_killed = self._pending_payload
            self._pending = True
            self._pending_payload = (list(prev_mud_lines) + list(mud_lines), stats, combat_mob,
                                     rescue_just_fired or prev_rescue,
                                     target_killed or prev_killed)
            self._pending_on_result = on_result
            return
        self._fire_turn(mud_lines, stats, combat_mob, rescue_just_fired, on_result,
                        target_killed)

    def _fire_turn(self, mud_lines, stats, combat_mob, rescue_just_fired, on_result,
                   target_killed=False):
        self._busy = True
        if rescue_just_fired:
            restart = (self._skill_cfg or {}).get("rescue_restart_step")
            if restart is not None:
                self._plan_step = restart
                self._cmd_history = []  # stale ledger would block speedwalk on retry
                self._messages = []     # stale combat history causes bad-state LLM reasoning
        skill_at_fire = self._skill_name
        user_msg = self._build_user_message(mud_lines, stats, combat_mob,
                                            rescue_just_fired, target_killed)
        self._messages.append({"role": "user", "content": user_msg})
        self._call_count += 1
        thread = threading.Thread(
            target=self._worker,
            args=(skill_at_fire, on_result),
            daemon=True,
            name=f"skill-{self._call_count}",
        )
        thread.start()

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def _worker(self, skill_at_fire, on_result):
        logger = self.client.session_logger
        system_prompt = self._build_system_prompt()
        msgs = list(self._messages)
        logger.log_llm_prompt(
            f"[skill:{skill_at_fire}]\n"
            f"[system]\n{system_prompt}\n"
            f"[user]\n{msgs[-1]['content']}"
        )

        # Throttle _default skill when idle to avoid spamming the LLM.
        if skill_at_fire == "_default" and self._idle_since is not None:
            idle_secs = int(time.time() - self._idle_since)
            if idle_secs > 15:
                time.sleep(10)

        result = None
        raw = None
        error = None
        try:
            raw = self._call_llm(system_prompt, msgs, max_tokens=2048)
            logger.log_llm_response(raw or "")
            result = self._parse(raw)
        except Exception as e:
            error = str(e)

        master = self.client.master

        def deliver():
            # If the session was stopped or swapped out mid-flight, drop silently.
            if self._skill_name != skill_at_fire:
                self._busy = False
                return
            self._battle_snapshot_inflight = False
            if result is not None and raw is not None:
                self._messages.append({"role": "assistant", "content": raw})
            # Cap rolling history to last N turn pairs. Command ledger
            # carries durable state, so dropping old turns is safe.
            max_entries = 2 * HISTORY_TURN_PAIRS
            if len(self._messages) > max_entries:
                self._messages = self._messages[-max_entries:]
            if error:
                self.client.append_text(f"[Skill error: {error}]\n", "error")
            on_result(result, skill_at_fire)
            # Update idle tracking: idle = no commands, no switch_skill, not fighting.
            if result is not None:
                has_cmds = bool(result.get("commands"))
                has_switch = bool(result.get("switch_skill"))
                is_fighting = bool(self.client._combat_mob)
                if has_cmds or has_switch or is_fighting:
                    self._idle_since = None
                elif self._idle_since is None:
                    self._idle_since = time.time()
            # Update plan step from LLM's reply (before chaining any pending turn).
            if result is not None and self._skill_name == skill_at_fire:
                new_step = result.get("plan_step")
                if new_step:
                    if not self._plan_steps or new_step in self._plan_steps:
                        self._plan_step = new_step
                    else:
                        self.client.append_text(
                            f"[Skill] LLM returned unknown plan_step '{new_step}' "
                            f"(valid: {', '.join(self._plan_steps)}) — keeping '{self._plan_step}'.\n",
                            "error"
                        )
            # Chain one pending turn if queued — but only when no commands were
            # dispatched this turn.  If commands were sent, the MUD will reply and
            # send a fresh prompt, which will drive the next turn with up-to-date
            # context.  Chaining immediately would give the LLM stale MUD output
            # (collected while the previous LLM call was running, before the MUD
            # responded to the commands).  Preserve any rescue flag from the
            # discarded payload so it isn't lost.
            self._busy = False
            if self._pending and self.is_active():
                self._pending = False
                payload = self._pending_payload
                cb = self._pending_on_result
                self._pending_payload = None
                self._pending_on_result = None
                mud_lines, stats, combat_mob, rescue_flag, tk = payload
                if result is not None and result.get("commands"):
                    # Commands were dispatched — skip chain, wait for MUD response.
                    if rescue_flag:
                        self._deferred_rescue = True
                else:
                    self._fire_turn(mud_lines, stats, combat_mob, rescue_flag, cb, tk)
            elif (result is not None and self.is_active()
                  and not result.get("commands") and not result.get("complete")):
                # No commands dispatched, no MUD prompt incoming, skill still
                # running — re-trigger immediately so the LLM can act on the
                # new plan step without waiting for a natural MUD prompt.
                master.after(0, self.client._trigger_skill)

        master.after(0, deliver)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_system_prompt(self):
        instr = (self._skill_cfg or {}).get("instructions", "").strip()
        watch = (self._skill_cfg or {}).get("watch_stats", [])
        parts = [SKILL_SYSTEM_PROMPT]
        profile = self.client.profiles.get(self.client.current_profile, {})
        char_name = profile.get("character", "")
        if char_name:
            parts.append(f"\nYour character's name is {char_name}. When another character addresses {char_name} directly, recognize that you are being spoken to.")
        landmarks = profile.get("landmarks", {})
        if landmarks:
            parts.append("\nKnown goto: landmarks: " + ", ".join(landmarks.keys()))
        parts.append(f"\n=== Skill: {self._skill_name} ===")
        if instr:
            parts.append("\nInstructions from the user:\n" + instr)
        if watch:
            parts.append("\nWatched stats (reported each turn): " + ", ".join(watch))
        skills = self.client._current_skills()
        targets = self.client._current_skill_targets()
        available = sorted(
            [s for s in skills if not s.startswith("_")] + list(targets.keys())
        )
        if self._skill_name != "_default" and "_default" in skills:
            available = ["_default"] + available
        if available:
            parts.append("\nAvailable skills you can switch to (use exact name in switch_skill): "
                         + ", ".join(available))
        return "\n".join(parts)

    def _render_plan(self):
        """Return plan markdown with checkboxes reflecting current completion state.

        Steps before the current step get [x] (completed); the current step
        gets [ ] with a trailing marker; steps after get [ ].  The template
        always stores [ ] for every step — the harness owns checkbox state.
        """
        plan_text = (self._skill_cfg or {}).get("plan", "")
        if not plan_text or not self._plan_steps:
            return plan_text
        current_idx = (self._plan_steps.index(self._plan_step)
                       if self._plan_step in self._plan_steps else -1)
        result = []
        for line in plan_text.split('\n'):
            m = _PLAN_STEP_RE.match(line)
            if m:
                prefix, mid, step_id, rest = m.group(1), m.group(2), m.group(3), m.group(4)
                if step_id in self._plan_steps:
                    idx = self._plan_steps.index(step_id)
                    checked = "x" if (current_idx >= 0 and idx < current_idx) else " "
                    marker = "  ← CURRENT STEP" if step_id == self._plan_step else ""
                    result.append(f"{prefix}{checked}{mid}{step_id}{rest}{marker}")
                else:
                    result.append(line)
            else:
                result.append(line)
        return '\n'.join(result)

    def _build_user_message(self, mud_lines, stats, combat_mob, rescue_just_fired,
                            target_killed=False):
        parts = []
        if self._injected_user_message:
            parts.append(f"[User instruction: {self._injected_user_message}]")
            parts.append("")
            self._injected_user_message = None
        reminders = (self._skill_cfg or {}).get("reminders", "").strip()
        if reminders:
            parts.append("REMINDERS (re-read each turn):")
            parts.append(reminders)
            parts.append("")
        if (self._skill_cfg or {}).get("plan") and self._plan_steps:
            parts.append("SKILL PLAN (re-read each turn):")
            parts.append(self._render_plan())
            parts.append(f"Current step: {self._plan_step}")
            parts.append(f"Valid plan_step values (use EXACTLY one): {', '.join(self._plan_steps)}")
            parts.append("")
        parts.append("MUD output since last command:")
        self._scan_battle_targets(mud_lines)
        lines = self._compress_combat(mud_lines, combat_mob) if combat_mob else list(mud_lines)
        mud = "\n".join(lines).strip()
        parts.append(mud if mud else "(no new output)")
        self._battle_turns_since_snap += 1
        if (self._battle_turns_since_snap >= BATTLE_SNAPSHOT_TURNS
                and not self._battle_snapshot_inflight
                and (self._battle_attacked_mobs or self._battle_attacked_pcs)):
            mobs_str = ", ".join(sorted(self._battle_attacked_mobs)) or "(none)"
            pcs_str  = ", ".join(sorted(self._battle_attacked_pcs))  or "(none)"
            snap = (f"[Group battle snapshot: mobs attacked: {mobs_str}; "
                    f"PCs attacked: {pcs_str}]")
            parts.append(snap)
            self.client.append_battle_snapshot(snap)
            self._battle_attacked_mobs.clear()
            self._battle_attacked_pcs.clear()
            self._battle_turns_since_snap = 0
            self._battle_snapshot_inflight = True
        parts.append(f"Turn random (0-99): {random.randint(0, 99)}")
        parts.append("")
        parts.append("Watched stats:")
        watch = (self._skill_cfg or {}).get("watch_stats", [])
        # Labels/formatting for stats where the raw number is ambiguous.
        # `tank` and `opp` come from the prompt as percentages of max HP
        # (e.g. 87 means the tank is at 87% HP); naming/formatting them
        # clearly prevents the LLM from reading them as absolute HP.
        stat_labels = {"opp": "opponent_hp_pct", "tank": "tank_hp_pct"}
        # Override tank/opp with the freshest values parsed directly from the
        # last prompt line in the buffer.  The snapshot in `stats` can be stale
        # if a non-combat echo prompt triggered this turn before the combat
        # round's prompt arrived.
        fresh_tank, fresh_opp = None, None
        for line in reversed(mud_lines):
            m = _PROMPT_COMBAT_RE.search(line)
            if m:
                fresh_tank, fresh_opp = int(m.group(1)), int(m.group(2))
                break
        if watch:
            for key in watch:
                if key == "tank" and fresh_tank is not None:
                    val = fresh_tank
                elif key == "opp" and fresh_opp is not None:
                    val = fresh_opp
                else:
                    val = stats.get(key)
                label = stat_labels.get(key, key)
                if key in ("tank", "opp") and val is not None:
                    parts.append(f"  {label} = {val}%  (percentage of max HP)")
                elif key == "spells":
                    names = ", ".join(sorted(val.keys())) if val else "none"
                    parts.append(f"  active_spells = {names}")
                else:
                    parts.append(f"  {label} = {val}")
        else:
            parts.append("  (none declared)")
        parts.append("")
        # Use buffer-fresh opp to detect combat when the snapshot is stale.
        in_combat_by_prompt = fresh_opp is not None and fresh_opp > 0
        if combat_mob:
            combat_display = combat_mob
        elif in_combat_by_prompt:
            combat_display = f"unknown (opp={fresh_opp}% in MUD prompt — you ARE in combat)"
        else:
            combat_display = "none"
        parts.append(f"Current combat target: {combat_display}")
        parts.append(f"Harness target_killed flag: {bool(target_killed)}")
        if self._idle_since is not None:
            idle_secs = int(time.time() - self._idle_since)
            parts.append(f"special_timer: {idle_secs}")
        if rescue_just_fired:
            parts.append(
                "RESCUE FIRED: you were summoned/teleported out of combat to the "
                "rescue location (e.g. Otto's room). Combat ended because you were "
                "removed from the fight, NOT because you won. The target mob is "
                "still alive unless target_killed is true. Follow your instructions "
                "for recovering from a rescue; do NOT mark the skill complete.")
        parts.append("")
        parts.append(self._format_cmd_history())
        parts.append("")
        group_leader = getattr(self.client, '_group_leader', None)
        group_members = getattr(self.client, 'group_members', set())
        if group_leader or group_members:
            members_str = ", ".join(sorted(group_members)) if group_members else "(none yet)"
            parts.append(f"Current group: leader={group_leader or 'unknown'}, members={members_str}")
            parts.append("")
        parts.append("Reply with the JSON object now.")
        return "\n".join(parts)

    def _known_pcs(self):
        """Return the set of lowercase PC names known to be in the group.

        Combines the WHO-list cache (populated by WHO command output) with
        group_members tracked live from join/leave messages in mud_client.
        """
        agent = self.client.ai_agent
        who_list = set()
        if agent and getattr(agent, 'state', None):
            who_list = {e['name'].lower() for e in (agent.state.who_list or [])}
        return who_list | getattr(self.client, 'group_members', set())

    def _scan_battle_targets(self, lines):
        """Scan raw mud_lines for attacked entities using combat-verb patterns.

        Updates _battle_attacked_mobs (mobs that were hit) and
        _battle_attacked_pcs (PCs that were hit) based on known PC membership.
        """
        known_pcs = self._known_pcs()

        for raw in lines:
            line = raw.rstrip()
            low = line.lower().lstrip()
            if not _COMBAT_VERB_RE.search(low):
                continue
            # PC targeted: a known PC name appears in the line NOT at the start
            for pc in known_pcs:
                if pc in low and not low.startswith(pc):
                    self._battle_attacked_pcs.add(pc)
            # Mob attacked: "you <verb> … the <name>" or "<pc> <verb> … the <name>"
            is_player_attacker = low.startswith('you ') or any(
                low.startswith(pc + ' ') for pc in known_pcs)
            if not is_player_attacker:
                continue
            # Skip miss lines: "try to <verb>" and "jumps out of the way" patterns
            if re.search(r'\btry\s+to\b|\bjumps?\s+out\b', low):
                continue
            # Two-step extraction: capture raw text after "the", then strip
            # trailing intensity phrases ("very hard", "extremely hard", etc.)
            m = _MOB_THE_RE.search(low)
            if m:
                target = _MOB_INTENSITY_RE.sub('', m.group(1)).strip()
                if target and target not in known_pcs:
                    self._battle_attacked_mobs.add(target)

    def _compress_combat(self, lines, combat_mob):
        """
        Collapse repetitive attack-verb lines against the current combat_mob
        into a single summary. Preserve anything that could carry new signal:
        HP changes, kill/xp lines, tells, room titles, non-target mob mentions,
        and any line that mentions a known group member (ally-attack lines).
        """
        if not lines:
            return list(lines)
        mob = (combat_mob or "").lower().strip()
        known_pcs = self._known_pcs()
        out = []
        hits_on = hits_from = misses = 0

        def flush():
            nonlocal hits_on, hits_from, misses
            if hits_on or hits_from or misses:
                out.append(
                    f"[combat: {hits_on} hits on {combat_mob}, "
                    f"{hits_from} hits from {combat_mob} taken, "
                    f"{misses} misses]"
                )
                hits_on = hits_from = misses = 0

        for raw in lines:
            line = raw.rstrip()
            low = line.lower()
            if not line.strip():
                continue
            # Only collapse lines that are pure attack verbs AND mention the
            # target mob (so pawn/knight interrupt lines fall through).
            # Never collapse lines that also mention a known group member —
            # the LLM must see ally-attack lines verbatim to trigger rescues.
            if mob and mob in low and _COMBAT_VERB_RE.search(low):
                if known_pcs and any(pc in low for pc in known_pcs):
                    flush()
                    out.append(line)
                    continue
                # Heuristic direction: "you <verb> ... <mob>" vs "<mob> <verb> ... you"
                if low.lstrip().startswith("you "):
                    if re.search(r'\bmiss(?:es)?\b', low):
                        misses += 1
                    else:
                        hits_on += 1
                else:
                    if re.search(r'\bmiss(?:es)?\b', low):
                        misses += 1
                    else:
                        hits_from += 1
                continue
            flush()
            out.append(line)
        flush()
        return out

    def _format_cmd_history(self):
        """Compact ledger of commands dispatched this session (authoritative count)."""
        hist = self._cmd_history
        total = len(hist)
        if total == 0:
            return "Commands sent this skill session so far (0 total): (none yet)"
        # Per-command counts (exact literal string match)
        counts = {}
        for c in hist:
            counts[c] = counts.get(c, 0) + 1
        count_pairs = sorted(counts.items(), key=lambda kv: -kv[1])
        count_str = ", ".join(f"{c}={n}" for c, n in count_pairs)
        last = hist[-15:]
        return (
            f"Commands sent this skill session so far ({total} total):\n"
            f"  counts: {count_str}\n"
            f"  last {len(last)}: {', '.join(last)}"
        )

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    _JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

    def _parse(self, text):
        """Extract {commands, complete, note} from the LLM reply. Returns None on failure."""
        if not text:
            return None
        # Strip markdown code fences if present.
        cleaned = text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        m = self._JSON_RE.search(cleaned)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        cmds = obj.get("commands", [])
        if not isinstance(cmds, list):
            return None
        cmds = [str(c).strip() for c in cmds if isinstance(c, (str, int, float)) and str(c).strip()]
        plan_step_val = obj.get("plan_step")
        if plan_step_val is not None:
            plan_step_val = str(plan_step_val).strip() or None
        switch = obj.get("switch_skill")
        if switch is not None:
            switch = str(switch).strip() or None
        return {
            "commands": cmds,
            "complete": bool(obj.get("complete", False)),
            "switch_skill": switch,
            "plan_step": plan_step_val,
            "note": str(obj.get("note", ""))[:200],
        }

    # ------------------------------------------------------------------
    # LLM backend (reuses the advisor's HTTP code)
    # ------------------------------------------------------------------

    def _call_llm(self, system_prompt, messages, max_tokens=1024):
        advisor = self.client.llm_advisor
        if advisor is None:
            advisor = LLMAdvisor(self.client)
        return advisor._call_backend(system_prompt, messages, max_tokens=max_tokens)
