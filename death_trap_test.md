# Death Trap Test

Standalone script to test whether the `explore` skill prompt correctly avoids
death-trap exits — no live MUD connection required.

## Usage

```bash
# Quick test (default scenario: narrow_ledge)
python3 death_trap_test.py

# Edit the skill instructions in $EDITOR, then run the test immediately
python3 death_trap_test.py --edit

# Load instructions from a file instead of the profile (for A/B testing)
python3 death_trap_test.py --instructions my_prompt.md

# Run 5 times and report pass rate (checks reliability)
python3 death_trap_test.py --repeat 5

# Test all scenarios
for s in narrow_ledge narrow_ledge_marked odd_room; do
  python3 death_trap_test.py --scenario $s
done

# Show raw LLM response and prompt excerpts
python3 death_trap_test.py --verbose
```

## Scenarios

| Name | Description |
|---|---|
| `narrow_ledge` | The Narrow Ledge — east is unknown; room description warns of free fall |
| `narrow_ledge_marked` | The Narrow Ledge — east is already marked `DANGEROUS(death-trap)` |
| `odd_room` | The Odd Room With Smooth Walls — entry causes immediate fall; only exit is down |

List scenarios:
```bash
python3 death_trap_test.py --list-scenarios
```

## Options

| Flag | Default | Description |
|---|---|---|
| `--model MODEL` | (from llm_local) | Override LLM model name |
| `--profile NAME` | last used | Profile name in the profiles JSON |
| `--config FILE` | `~/.mud_client_profiles.json` | Path to profiles file |
| `--llm-config FILE` | `~/.mud_client_llm_local.json` | Path to LLM config override file |
| `--scenario NAME` | `narrow_ledge` | Which scenario to simulate |
| `--edit` | off | Open skill instructions in `$EDITOR` before running |
| `--instructions FILE` | — | Load instructions from a file instead of the profile |
| `--repeat N` | 1 | Run N times and report pass rate |
| `--verbose` / `-v` | off | Show raw LLM response and prompt excerpts |

## How it works

The script reconstructs the exact system + user prompt that `skill_engine.py`
sends to the LLM during a live session — including the `explore` skill
instructions, the rendered plan with checkboxes, the room annotation line, and
the command ledger. The LLM response is parsed and evaluated:

- **PASS** — no deadly exit sent; safe command or `markdangerous:` issued
- **FAIL** — the deadly exit direction appeared in `commands`
- **UNCERTAIN** — response parsed but command not in the known safe/deadly sets
