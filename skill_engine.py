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
import re
import threading

from llm_advisor import LLMAdvisor


# Keep the last N user+assistant turn pairs in the rolling history.
# Bounds recency drift: the system prompt stays a constant fraction of
# context instead of shrinking as the session runs.
HISTORY_TURN_PAIRS = 3

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
    "note": "one short sentence about what you're doing"
  }

Rules:
  - `commands` is a list of literal MUD commands to send, in order. Empty list
    means "wait one tick and re-evaluate" — valid and normal.
  - Each command must be a real MUD input line (e.g. "kill rook", "cast bless",
    "n", "tell otto heal"). Do not include prose, quotes, numbering, or prefixes.
  - Set `complete` to true ONLY when the skill's original objective is achieved
    (e.g. the target mob is dead). Partial steps (moved, healed, rescued) are
    not completion.
  - COMPLETION INVARIANT: never set `complete: true` based on combat silence,
    absence of the mob, or the fight "feeling over". You MAY only set it when
    EITHER (a) the turn payload reports `target_killed: true`, OR (b) the MUD
    output in this turn contains an explicit kill line naming the target
    (e.g. "<mob> is dead! R.I.P."). If neither is present, combat ended because
    you fled, were rescued, or were summoned — the target is still alive.
    The harness will reject `complete: true` without a kill confirmation.
  - Handle rescues yourself: if you are rescued mid-fight, ask the tank/healer
    for heals per the instructions, wait until HP recovers, then return and
    resume the attack. The skill is ONE continuous task from start to finish.
  - `note` is a short status string shown in the UI; keep it under 80 chars.
  - Respond with valid JSON only. No markdown, no code fences, no commentary
    outside the JSON object.

Movement:
  - When a skill's instructions give you a speedwalk string (e.g. "5n4w4s"),
    send the whole string as ONE command in a single turn. Speedwalks are
    executed atomically by the MUD and halt automatically on combat, so you
    do not need to step by step.
  - Only fall back to per-step movement when the instructions explicitly
    tell you to, or when recovering from an interruption where a speedwalk
    is not appropriate.

Command ledger:
  - Each turn's payload includes "Commands sent this skill session so far"
    with per-command counts and the last several commands. This is the
    AUTHORITATIVE record of what has been dispatched — trust it over your
    own memory when deciding whether something was already done (buff sent,
    speedwalk already fired, etc.).
"""


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

    def stop(self):
        """Cancel the active skill. In-flight LLM replies are discarded."""
        self._skill_name = None
        self._skill_cfg = None
        self._messages = []
        self._busy = False
        self._pending = False
        self._cmd_history = []

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
        if self._busy:
            # OR-merge sticky flags so a rescue or kill signal raised while
            # an earlier turn is in flight isn't lost when the payload is
            # overwritten by a later, flag-less trigger.
            prev_rescue = False
            prev_killed = False
            if self._pending and self._pending_payload is not None:
                _, _, _, prev_rescue, prev_killed = self._pending_payload
            self._pending = True
            self._pending_payload = (mud_lines, stats, combat_mob,
                                     rescue_just_fired or prev_rescue,
                                     target_killed or prev_killed)
            self._pending_on_result = on_result
            return
        self._fire_turn(mud_lines, stats, combat_mob, rescue_just_fired, on_result,
                        target_killed)

    def _fire_turn(self, mud_lines, stats, combat_mob, rescue_just_fired, on_result,
                   target_killed=False):
        self._busy = True
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
        logger.log_llm_prompt(f"[skill:{skill_at_fire}]\n{msgs[-1]['content']}")

        result = None
        raw = None
        error = None
        try:
            raw = self._call_llm(system_prompt, msgs, max_tokens=1024)
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
            # Chain one pending turn if queued.
            self._busy = False
            if self._pending and self.is_active():
                self._pending = False
                payload = self._pending_payload
                cb = self._pending_on_result
                self._pending_payload = None
                self._pending_on_result = None
                mud_lines, stats, combat_mob, rescue_flag, tk = payload
                self._fire_turn(mud_lines, stats, combat_mob, rescue_flag, cb, tk)

        master.after(0, deliver)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_system_prompt(self):
        instr = (self._skill_cfg or {}).get("instructions", "").strip()
        watch = (self._skill_cfg or {}).get("watch_stats", [])
        parts = [SKILL_SYSTEM_PROMPT]
        parts.append(f"\n=== Skill: {self._skill_name} ===")
        if instr:
            parts.append("\nInstructions from the user:\n" + instr)
        if watch:
            parts.append("\nWatched stats (reported each turn): " + ", ".join(watch))
        return "\n".join(parts)

    def _build_user_message(self, mud_lines, stats, combat_mob, rescue_just_fired,
                            target_killed=False):
        parts = []
        reminders = (self._skill_cfg or {}).get("reminders", "").strip()
        if reminders:
            parts.append("REMINDERS (re-read each turn):")
            parts.append(reminders)
            parts.append("")
        parts.append("MUD output since last command:")
        lines = self._compress_combat(mud_lines, combat_mob) if combat_mob else list(mud_lines)
        mud = "\n".join(lines).strip()
        parts.append(mud if mud else "(no new output)")
        parts.append("")
        parts.append("Watched stats:")
        watch = (self._skill_cfg or {}).get("watch_stats", [])
        # Labels/formatting for stats where the raw number is ambiguous.
        # `tank` and `opp` come from the prompt as percentages of max HP
        # (e.g. 87 means the tank is at 87% HP); naming/formatting them
        # clearly prevents the LLM from reading them as absolute HP.
        stat_labels = {"opp": "opponent_hp_pct", "tank": "tank_hp_pct"}
        if watch:
            for key in watch:
                val = stats.get(key)
                label = stat_labels.get(key, key)
                if key in ("tank", "opp") and val is not None:
                    parts.append(f"  {label} = {val}%  (percentage of max HP)")
                else:
                    parts.append(f"  {label} = {val}")
        else:
            parts.append("  (none declared)")
        parts.append("")
        parts.append(f"Current combat target: {combat_mob if combat_mob else 'none'}")
        parts.append(f"Harness target_killed flag: {bool(target_killed)}")
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
        parts.append("Reply with the JSON object now.")
        return "\n".join(parts)

    def _compress_combat(self, lines, combat_mob):
        """
        Collapse repetitive attack-verb lines against the current combat_mob
        into a single summary. Preserve anything that could carry new signal:
        HP changes, kill/xp lines, tells, room titles, non-target mob mentions.
        """
        if not lines:
            return list(lines)
        mob = (combat_mob or "").lower().strip()
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
            if mob and mob in low and _COMBAT_VERB_RE.search(low):
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
        return {
            "commands": cmds,
            "complete": bool(obj.get("complete", False)),
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
