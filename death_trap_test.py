#!/usr/bin/env python3
# Copyright (c) 2026 Bob Amstadt
# SPDX-License-Identifier: MIT
"""
Death-trap avoidance test for the explore skill.

Simulates the room scenario where the LLM must decide whether to enter
a deadly exit. Tests whether the current skill prompt correctly instructs
the LLM to avoid it — without requiring a live MUD connection.

Usage:
  python death_trap_test.py [options]

Options:
  --profile NAME    Profile name in the profiles JSON (default: first profile)
  --config FILE     Path to mud_client_profiles.json
                    (default: ~/Dropbox/tintin/mud_client_profiles.json)
  --edit            Open the skill instructions in $EDITOR before running
  --instructions F  Use instructions from FILE instead of the profile value
  --scenario NAME   Scenario to test (default: narrow_ledge)
                    Available: narrow_ledge, narrow_ledge_marked, odd_room
  --repeat N        Run the test N times and report pass rate (default: 1)
  --model MODEL     Override the LLM model name (e.g. gemma4-e4b-128k)
  --list-scenarios  List available scenarios and exit
"""

import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse

# ---------------------------------------------------------------------------
# Skill system prompt — matches SKILL_SYSTEM_PROMPT in skill_engine.py
# ---------------------------------------------------------------------------

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

Navigation commands:
  goto:<target>       Navigate to a known location via BFS pathfinding.
    goto:vnum:123       — room by number
    goto:Otto           — landmark named "Otto" (a configured fixed location)
    goto:mob:white rook — room where that mob was last seen
    goto:Temple Square  — room whose name contains "Temple Square"
  The harness resolves the target and injects directions automatically.
  If unreachable, a warning is shown and the command is skipped.
  While navigating, each turn's MUD output begins with:
      [Harness: goto:<target> in progress]   — still moving
      [Harness: goto:<target> arrived — <room>]  — destination reached
  Wait for the "arrived" line before acting on the destination.

  explore:            Navigate to the nearest room with unknown (unmapped) exits.
  The harness injects on arrival:
      [Harness: explore: arrived at <room> — known: dir→dest; assumed: dir→dest; unknown: dir]
  Stop and choose which unknown exit to try based on the room description.
  If the whole map is explored you will see:
      [Harness: explore: map appears complete — no unmapped exits found]

Landmark commands:
  setlandmark:<name>  Set a named landmark at the current room (replaces any existing).
                      Example: setlandmark:bank
  unsetlandmark:<name> Remove a named landmark.
                      Example: unsetlandmark:bank

Danger commands:
  markdangerous:<dir>  Mark an exit from the CURRENT room as a death-trap link; it will be
                      avoided by all future goto: and explore: pathfinding.
  markblocked:<dir>   Mark an exit from the CURRENT room as blocked (locked door,
                      insufficient key/level, etc.). Use immediately after the MUD
                      refuses a movement attempt. <dir> is the direction word you tried
                      (north/south/east/west/up/down or abbreviation). Blocked exits are
                      shown as "blocked:" in [Room: ...] and skipped by pathfinding.
                      Example: markblocked:north

Per-turn room annotation (always present):
  Each turn begins with:
    [Room: <name> (<key>) — known: dir→dest, ...; assumed: dir→dest; unknown: dir, ...; blocked: dir, ...]
  Use this to decide which exits are safe to traverse and which are unexplored.

Command ledger:
  - Each turn's payload includes "Commands sent this skill session so far"
    with per-command counts and the last several commands. This is the
    AUTHORITATIVE record of what has been dispatched — trust it over your
    own memory when deciding whether something was already done (buff sent,
    speedwalk already fired, etc.).

Loop detection: If the last 3 or more commands in your ledger are all "explore:", you
are stuck in an explore loop — the harness keeps returning you to the same room. Break
out by reading the [Room: ...] line and sending ONE direction word for an unknown or
assumed exit directly. Do NOT send explore: again until you have successfully moved.
More generally, if the last 8 commands are drawn from only 2-3 distinct values cycling
repeatedly, you are stuck — choose a fundamentally different action to break the pattern.
"""

# ---------------------------------------------------------------------------
# Test scenarios — each simulates a death-trap decision point
# ---------------------------------------------------------------------------

SCENARIOS = {
    "narrow_ledge": {
        "description": (
            "The Narrow Ledge — room description warns about free fall east; "
            "east is listed as unknown (not yet marked)"
        ),
        "room_description": (
            "You are in the process of walking all the way around the ledge since\n"
            "there is no way of turning around on this all too narrow path.  There IS\n"
            "another exit from here but that leads right into mid-air and with high\n"
            "probability of a free fall session afterwards."
        ),
        "mud_output": (
            "[Harness: explore: arrived at The Narrow Ledge — "
            "known: north→The Narrow Ledge, south→The Narrow Ledge; unknown: east]\n"
            "[Room: The Narrow Ledge (vnum:7110) — "
            "known: north→The Narrow Ledge, south→The Narrow Ledge; unknown: east]\n"
            "The Narrow Ledge\n"
            "   You are in the process of walking all the way around the ledge since\n"
            "there is no way of turning around on this all too narrow path.  There IS\n"
            "another exit from here but that leads right into mid-air and with high\n"
            "probability of a free fall session afterwards.\n"
            "[ Exits: n e s ]\n"
            "476H 100M 147V 0%T 0%O >"
        ),
        "plan_step": "walk_exit",
        "prior_commands": ["explore:"],
        "deadly_exits": ["east", "e"],
        "safe_commands": ["north", "south", "n", "s", "markdangerous:", "markdangerous"],
    },
    "narrow_ledge_marked": {
        "description": (
            "The Narrow Ledge — east is already marked DANGEROUS(death-trap); "
            "agent must still avoid it and choose n or s"
        ),
        "room_description": (
            "You are in the process of walking all the way around the ledge since\n"
            "there is no way of turning around on this all too narrow path.  There IS\n"
            "another exit from here but that leads right into mid-air and with high\n"
            "probability of a free fall session afterwards."
        ),
        "mud_output": (
            "[Harness: explore: arrived at The Narrow Ledge — "
            "known: north→The Narrow Ledge, south→The Narrow Ledge; "
            "DANGEROUS(death-trap): east]\n"
            "[Room: The Narrow Ledge (vnum:7108) — "
            "known: north→The Narrow Ledge, south→The Narrow Ledge; "
            "DANGEROUS(death-trap): east]\n"
            "The Narrow Ledge\n"
            "   You are in the process of walking all the way around the ledge since\n"
            "there is no way of turning around on this all too narrow path.  There IS\n"
            "another exit from here but that leads right into mid-air and with high\n"
            "probability of a free fall session afterwards.\n"
            "[ Exits: n e s ]\n"
            "476H 100M 147V 0%T 0%O >"
        ),
        "plan_step": "walk_exit",
        "prior_commands": ["explore:", "north", "explore:", "north", "explore:"],
        "deadly_exits": ["east", "e"],
        "safe_commands": ["north", "south", "n", "s", "markdangerous:", "markdangerous",
                          "explore:"],
    },
    "odd_room": {
        "description": (
            "The Odd Room With Smooth Walls — room entry causes immediate fall; "
            "only exit is down; agent arrived here by accident and must avoid going further"
        ),
        "room_description": (
            "As you enter you hear a loud click from one of the walls... and you fall\n"
            "and fall and fall...\n"
            "...\n"
            "... And come to an abrupt end.  THIS is strange indeed.  There is an arched\n"
            "entryway leading down."
        ),
        "mud_output": (
            "[Room: The Odd Room With Smooth Walls (vnum:7050) — unknown: down]\n"
            "The Odd Room With Smooth Walls\n"
            "   As you enter you hear a loud click from one of the walls... and you fall\n"
            "and fall and fall...\n"
            ".\n"
            ".\n"
            ".\n"
            ".\n"
            "... And come to an abrupt end.  THIS is strange indeed.  There is an arched\n"
            "entryway leading down.\n"
            "[ Exits: d ]\n"
            "476H 100M 147V 0%T 0%O >"
        ),
        "plan_step": "walk_exit",
        "prior_commands": ["explore:", "west"],
        "deadly_exits": ["down", "d"],
        "safe_commands": ["markdangerous:", "markdangerous", "explore:"],
    },
}

# ---------------------------------------------------------------------------
# LLM backends — standalone, no mud_client dependency
# ---------------------------------------------------------------------------

def call_ollama(cfg, system_prompt, messages, max_tokens=2048):
    endpoint = cfg.get('llm_endpoint', 'http://localhost:11434')
    model = cfg.get('llm_model', 'llama3.1:8b')
    parsed = urllib.parse.urlparse(endpoint)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    use_ssl = parsed.scheme == 'https'
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "temperature": 0.7,
        "max_tokens": max_tokens,
        "stream": False,
        "options": {"num_ctx": 131072},
    }
    body = json.dumps(payload).encode('utf-8')
    headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    conn = (http.client.HTTPSConnection(host, port, timeout=180) if use_ssl
            else http.client.HTTPConnection(host, port, timeout=180))
    try:
        conn.request("POST", "/v1/chat/completions", body=body, headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            raw = resp.read().decode('utf-8')
            raise RuntimeError(f"Ollama returned HTTP {resp.status}: {raw[:200]}")
        data = json.loads(resp.read().decode('utf-8'))
        return data['choices'][0]['message']['content']
    finally:
        conn.close()


def call_claude(cfg, system_prompt, messages, max_tokens=2048):
    api_key = cfg.get('claude_api_key', '')
    if not api_key:
        raise RuntimeError("claude_api_key is not set in profile ai_config.")
    model = cfg.get('claude_model', 'claude-haiku-4-5-20251001')
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
        "stream": False,
    }
    body = json.dumps(payload).encode('utf-8')
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    conn = http.client.HTTPSConnection("api.anthropic.com", timeout=180)
    try:
        conn.request("POST", "/v1/messages", body=body, headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            raw = resp.read().decode('utf-8')
            raise RuntimeError(f"Claude API returned HTTP {resp.status}: {raw[:200]}")
        data = json.loads(resp.read().decode('utf-8'))
        return data['content'][0]['text']
    finally:
        conn.close()


def call_llm(ai_config, system_prompt, messages):
    backend = ai_config.get('llm_backend', 'ollama').lower()
    if backend == 'ollama':
        return call_ollama(ai_config, system_prompt, messages)
    elif backend == 'claude':
        return call_claude(ai_config, system_prompt, messages)
    else:
        raise RuntimeError(f"Unknown llm_backend: {backend!r}")


# ---------------------------------------------------------------------------
# Prompt construction — mirrors skill_engine.py
# ---------------------------------------------------------------------------

PLAN_STEP_ID_RE = re.compile(r'^\s*-\s*\[[ xX]\]\s*(\w+)\s*:', re.MULTILINE)
PLAN_STEP_RE = re.compile(r'^(\s*-\s*\[)[ xX](\]\s*)(\w+)(\s*:.*)')


def _parse_plan_steps(text):
    return PLAN_STEP_ID_RE.findall(text or "")


def _render_plan(plan_text, plan_steps, current_step):
    if not plan_text or not plan_steps:
        return plan_text
    current_idx = plan_steps.index(current_step) if current_step in plan_steps else -1
    result = []
    for line in plan_text.split('\n'):
        m = PLAN_STEP_RE.match(line)
        if m:
            prefix, mid, step_id, rest = m.group(1), m.group(2), m.group(3), m.group(4)
            if step_id in plan_steps:
                idx = plan_steps.index(step_id)
                checked = "x" if (current_idx >= 0 and idx < current_idx) else " "
                marker = "  ← CURRENT STEP" if step_id == current_step else ""
                result.append(f"{prefix}{checked}{mid}{step_id}{rest}{marker}")
            else:
                result.append(line)
        else:
            result.append(line)
    return '\n'.join(result)


def build_system_prompt(instructions, char_name, landmarks, skill_name,
                        available_skills, watch_stats):
    parts = [SKILL_SYSTEM_PROMPT]
    if char_name:
        parts.append(
            f"\nYour character's name is {char_name}. When another character "
            f"addresses {char_name} directly, recognize that you are being spoken to."
        )
    if landmarks:
        parts.append("\nKnown goto: landmarks: " + ", ".join(landmarks))
    parts.append(f"\n=== Skill: {skill_name} ===")
    if instructions:
        parts.append("\nInstructions from the user:\n" + instructions)
    if watch_stats:
        parts.append("\nWatched stats (reported each turn): " + ", ".join(watch_stats))
    if available_skills:
        parts.append(
            "\nAvailable skills you can switch to (use exact name in switch_skill): "
            + ", ".join(available_skills)
        )
    return "\n".join(parts)


def build_user_message(scenario, plan_text, plan_steps, current_step):
    parts = []
    if plan_text and plan_steps:
        parts.append("SKILL PLAN (re-read each turn):")
        parts.append(_render_plan(plan_text, plan_steps, current_step))
        parts.append(f"Current step: {current_step}")
        parts.append(f"Valid plan_step values (use EXACTLY one): {', '.join(plan_steps)}")
        parts.append("")
    room_desc = scenario.get("room_description", "").strip()
    if room_desc:
        parts.append(f"Current room description: {room_desc}")
        parts.append("")
    parts.append("MUD output since last command:")
    parts.append(scenario.get("mud_output", "").strip())
    parts.append("Turn random (0-99): 42")
    parts.append("")
    parts.append("Watched stats:")
    parts.append("  (none declared)")
    parts.append("")
    parts.append("Current combat target: none")
    parts.append("Harness target_killed flag: False")
    parts.append("special_timer: 0")
    parts.append("")
    prior = scenario.get("prior_commands", [])
    if prior:
        counts = {}
        for c in prior:
            counts[c] = counts.get(c, 0) + 1
        count_str = ", ".join(f"{c}={n}" for c, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        last_n = prior[-15:]
        parts.append(
            f"Commands sent this skill session so far ({len(prior)} total):\n"
            f"  counts: {count_str}\n"
            f"  last {len(last_n)}: {', '.join(last_n)}"
        )
    else:
        parts.append("Commands sent this skill session so far (0 total): (none yet)")
    parts.append("")
    parts.append("Reply with the JSON object now.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_response(text):
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    m = _JSON_RE.search(cleaned)
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
        cmds = []
    cmds = [str(c).strip().lower() for c in cmds if isinstance(c, (str, int, float)) and str(c).strip()]
    return {
        "commands": cmds,
        "complete": bool(obj.get("complete", False)),
        "switch_skill": obj.get("switch_skill"),
        "plan_step": obj.get("plan_step"),
        "note": str(obj.get("note", "")),
    }


def evaluate(result, scenario):
    """Return (passed: bool, verdict: str)."""
    if result is None:
        return False, "FAIL — could not parse LLM response"
    deadly = set(scenario["deadly_exits"])
    cmds = result["commands"]
    walked = [c for c in cmds if c in deadly]
    if walked:
        return False, f"FAIL — walked deadly exit: {walked}"
    if not cmds:
        return True, "PASS — sent no commands (safe wait)"
    safe = set(scenario.get("safe_commands", []))
    unsafe_cmds = [c for c in cmds if c not in safe and not c.startswith("markdangerous")]
    if unsafe_cmds:
        return None, f"UNCERTAIN — sent: {cmds} (not a known deadly exit, but not a recognised safe command either)"
    return True, f"PASS — safe command(s): {cmds}"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = os.path.expanduser("~/.mud_client_profiles.json")
DEFAULT_LLM_CONFIG = os.path.expanduser("~/.mud_client_llm_local.json")


def load_profile(config_path, profile_name=None):
    with open(config_path) as f:
        data = json.load(f)
    # Skip the _settings key
    profiles = {k: v for k, v in data.items() if not k.startswith('_')}
    if not profiles:
        raise RuntimeError("No profiles found in config file.")
    if profile_name:
        if profile_name not in profiles:
            raise RuntimeError(f"Profile '{profile_name}' not found. Available: {', '.join(profiles)}")
        return profile_name, profiles[profile_name]
    # Auto-select: use last_profile if set, otherwise first
    last = data.get('_settings', {}).get('last_profile')
    if last and last in profiles:
        return last, profiles[last]
    name = next(iter(profiles))
    return name, profiles[name]


def load_llm_config(llm_config_path, profile_name):
    """Load per-profile LLM config from the llm_local file, if it exists."""
    if not os.path.exists(llm_config_path):
        return {}
    with open(llm_config_path) as f:
        data = json.load(f)
    return data.get(profile_name, {})


def extract_skill(profile, skill_name="explore"):
    skills = profile.get("skills", {})
    if skill_name not in skills:
        raise RuntimeError(
            f"Skill '{skill_name}' not found in profile. "
            f"Available: {', '.join(skills)}"
        )
    return dict(skills[skill_name])


def collect_available_skills(profile):
    skills = list(profile.get("skills", {}).keys())
    targets = list(profile.get("skill_targets", {}).keys())
    available = sorted([s for s in skills if not s.startswith("_")] + targets)
    if "_default" in skills:
        available = ["_default"] + available
    return available


def collect_landmarks(profile):
    return list(profile.get("landmarks", {}).keys())


# ---------------------------------------------------------------------------
# Editor integration
# ---------------------------------------------------------------------------

def edit_in_editor(text, suffix=".txt"):
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "nano"))
    with tempfile.NamedTemporaryFile(mode='w', suffix=suffix, delete=False) as f:
        f.write(text)
        fname = f.name
    try:
        subprocess.call([editor, fname])
        with open(fname) as f:
            return f.read()
    finally:
        os.unlink(fname)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_test(args):
    scenario = SCENARIOS[args.scenario]
    print(f"\nScenario: {args.scenario}")
    print(f"  {scenario['description']}")
    print()

    # Load profile
    profile_name, profile = load_profile(args.config, args.profile)
    print(f"Profile: {profile_name}")

    # LLM config: start from profile's ai_config, overlay with llm_local file
    ai_config = dict(profile.get("ai_config", {}))
    llm_local = load_llm_config(args.llm_config, profile_name)
    ai_config.update(llm_local)
    if args.model:
        ai_config['llm_model'] = args.model
        ai_config['claude_model'] = args.model
    backend = ai_config.get('llm_backend', '?')
    model = ai_config.get('llm_model') or ai_config.get('claude_model', '?')
    print(f"LLM backend: {backend}  model: {model}")
    print()

    # Load skill
    skill_cfg = extract_skill(profile, "explore")

    # Override instructions from file if requested
    if args.instructions:
        with open(args.instructions) as f:
            skill_cfg["instructions"] = f.read()
        print(f"Instructions loaded from: {args.instructions}")

    # Optionally open instructions in editor
    if args.edit:
        print("Opening skill instructions in editor...")
        original = skill_cfg.get("instructions", "")
        modified = edit_in_editor(original, suffix=".md")
        if modified.strip() != original.strip():
            skill_cfg["instructions"] = modified
            print("Instructions updated.")
            print()
            print("--- Modified instructions ---")
            print(modified.strip())
            print("----------------------------")
        else:
            print("No changes made.")
        print()

    char_name = profile.get("character", "Ollyama")
    landmarks = collect_landmarks(profile)
    available_skills = collect_available_skills(profile)
    watch_stats = skill_cfg.get("watch_stats", [])
    plan_text = skill_cfg.get("plan", "")
    plan_steps = _parse_plan_steps(plan_text)
    current_step = scenario["plan_step"]

    system_prompt = build_system_prompt(
        instructions=skill_cfg.get("instructions", ""),
        char_name=char_name,
        landmarks=landmarks,
        skill_name="explore",
        available_skills=available_skills,
        watch_stats=watch_stats,
    )
    user_message = build_user_message(scenario, plan_text, plan_steps, current_step)

    passes = 0
    fails = 0
    uncertain = 0

    for i in range(args.repeat):
        run_label = f"Run {i+1}/{args.repeat}" if args.repeat > 1 else "Running test"
        print(f"{run_label}...", end=" ", flush=True)

        try:
            raw = call_llm(ai_config, system_prompt, [{"role": "user", "content": user_message}])
        except Exception as e:
            print(f"ERROR — LLM call failed: {e}")
            fails += 1
            continue

        result = parse_response(raw)
        passed, verdict = evaluate(result, scenario)

        print(verdict)
        if result:
            print(f"  commands: {result['commands']}")
            print(f"  plan_step: {result['plan_step']}")
            print(f"  note: {result['note']}")

        if passed is True:
            passes += 1
        elif passed is False:
            fails += 1
        else:
            uncertain += 1

        if args.repeat == 1 and args.verbose:
            print()
            print("--- Raw LLM response ---")
            print(raw.strip())
            print("------------------------")
            print()
            print("--- System prompt (first 2000 chars) ---")
            print(system_prompt)
            print("...")
            print("--- User message (first 2000 chars) ---")
            print(user_message)
            print("...")

    if args.repeat > 1:
        total = args.repeat
        print(f"\nResults: {passes}/{total} passed, {fails}/{total} failed, {uncertain}/{total} uncertain")
        if fails == 0:
            print("All runs avoided the death trap.")
        elif passes == 0:
            print("PROBLEM: All runs walked into the death trap!")
        else:
            print(f"Unreliable: {fails} runs walked into the death trap.")


def main():
    parser = argparse.ArgumentParser(
        description="Test death-trap avoidance for the explore skill.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--profile", default=None,
                        help="Profile name (default: last used or first)")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help=f"Path to mud_client_profiles.json (default: {DEFAULT_CONFIG})")
    parser.add_argument("--llm-config", default=DEFAULT_LLM_CONFIG,
                        dest="llm_config",
                        help=f"Path to mud_client_llm_local.json (default: {DEFAULT_LLM_CONFIG})")
    parser.add_argument("--edit", action="store_true",
                        help="Open skill instructions in $EDITOR before running")
    parser.add_argument("--instructions", default=None, metavar="FILE",
                        help="Load instructions from FILE instead of profile")
    parser.add_argument("--scenario", default="narrow_ledge",
                        choices=list(SCENARIOS.keys()),
                        help="Scenario to test (default: narrow_ledge)")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="Run the test N times and report pass rate")
    parser.add_argument("--list-scenarios", action="store_true",
                        help="List available scenarios and exit")
    parser.add_argument("--model", default=None, metavar="MODEL",
                        help="Override LLM model name (e.g. gemma4-e4b-128k)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show raw LLM response and prompts")
    args = parser.parse_args()

    if args.list_scenarios:
        print("Available scenarios:")
        for name, s in SCENARIOS.items():
            print(f"  {name}: {s['description']}")
        return

    run_test(args)


if __name__ == "__main__":
    main()
