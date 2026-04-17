# AIWanderer — Capabilities & Roadmap

> Last updated: 2026-04-17

## Overview

AIWanderer is an AI-powered MUD (Multi-User Dungeon) client combining a traditional graphical client with an LLM-driven skill engine. The application connects to MUD servers, provides a real-time GUI for manual play, and lets the user delegate named tasks ("skills") to an LLM agent that monitors MUD output and issues commands autonomously until the task is complete.

---

## Current Capabilities

### Networking & Connectivity
- SSL/TLS encrypted connections to MUD servers
- Configurable port and SSL mode (full, starttls, none)
- Telnet protocol negotiation (IAC, WILL, WONT, etc.)
- Non-blocking I/O via a dedicated receive thread with a message queue
- Graceful connect/disconnect with visual status indicators

### GUI Interface
- Built with tkinter for cross-platform compatibility
- Dark theme with color-coded output:
  - Cyan — system messages
  - Yellow — player commands
  - Red — errors
  - White — raw MUD server output
- Real-time scrolling text display
- Manual command input with Enter-to-send
- Skills menu — start/stop named skills and manage skill definitions

### Configuration (Split-File Design)
- **Shared profiles** (`mud_client_profiles.json`, e.g. in Dropbox) — connection details, room map, skills, skill templates, skill targets
- **Host-local LLM config** (`mud_client_llm_local.json`) — LLM backend, endpoint, model, API key (not shared; machine-specific)
- **Host-local UI config** (`mud_client_ui_local.json`) — window geometry and display state (not shared)
- Named connection profiles: host, port, SSL mode, character name, password
- Auto-login sequences with learned prompt-response pairs
- Prompt normalization — replaces numeric stat values with wildcards so prompts match across sessions
- Last-used profile auto-selection on startup

### Room Mapping & World Model
- Real-time room tracking as the player moves
- Hash-based room identity (room description → unique ID)
- Exit detection from room description text
- Bidirectional room link graph stored per-profile
- Collision zone detection — distinguishes physically different rooms that share identical descriptions using (x, y, z) coordinate tracking
- BFS pathfinding to any known room in the graph (`PathFinder.bfs_path`)
- Persistent map data survives across sessions

### Session Logging
- Timestamped log files written to `~/mud_sessions/session_YYYY-MM-DD_HH-MM-SS.log`
- Separate log categories: MUD output, player commands, AI commands, AI reasoning, system events, errors
- ANSI escape code stripping for clean, readable logs

### Skill System (Primary AI Feature)

A **skill** is a named, LLM-driven task the user delegates to the agent. The agent runs the skill to completion, issuing MUD commands each turn based on the skill's instructions and a stream of watched stats and recent MUD output.

**Skill types:**
- **Named skills** (`profile["skills"]`) — standalone skills with fixed instructions (e.g. `group_tank`)
- **Skill templates** (`profile["skill_templates"]`) — parameterized instruction sets with `{{placeholder}}` fields (e.g. `kill_target_from_otto`)
- **Skill targets** (`profile["skill_targets"]`) — named bindings of a template to a specific set of params (e.g. `white rook`, `black queen`)

**Multi-step plans:**
- Skills can declare a markdown plan (checklist of named steps)
- The harness tracks the current step, renders the plan with checkboxes each turn, and validates step names returned by the LLM
- Rescue events automatically reset the plan to a configurable `rescue_restart_step`

**Turn structure:**
- Each turn delivers: skill instructions, SKILL PLAN with current step marked, recent MUD output (compressed during combat), watched stat values, current combat target, rescue/kill flags, and a command ledger
- The LLM replies with a JSON object: `{commands, complete, plan_step, note}`
- Empty `commands` means "wait and re-evaluate"; `complete: true` stops the skill

**Combat compression:**
- Repetitive attack-verb lines against the current target are collapsed into a summary (`[combat: N hits on X, M hits from X, K misses]`)
- Lines involving known group members are always preserved verbatim so the LLM can see ally-attack events and trigger rescues

**Group battle snapshots:**
- Every 5 turns during combat, a summary is injected: `[Group battle snapshot: mobs attacked: ...; PCs attacked: ...]`
- Tracks which mobs and which PCs are being attacked, using WHO-list and group-membership data to distinguish PCs from mobs

**Command ledger:**
- Full per-command dispatch counts and last-N commands are included every turn
- Acts as the authoritative record of what has been sent (guards against duplicate speedwalks, duplicate buffs, etc.)

**Rolling conversation history:**
- Last 3 user+assistant turn pairs are kept in the LLM conversation
- Older turns are dropped to bound context growth; the command ledger carries durable state

**Lifecycle management:**
- Skill engine is non-blocking: LLM I/O runs in a background thread; results are delivered on the Tk main thread
- Pending-turn queuing: if a prompt arrives while a prior LLM call is in flight, it is queued and fired once (with mud_lines accumulated, and sticky flags OR-merged)
- After a rescue, the command ledger and conversation history are cleared to prevent stale state from confusing the LLM on retry

### Rescue System
- Configurable per-profile: rescue command, HP threshold (fixed), and damage multiplier (relative to observed max single hit from the opponent)
- Auto-fires when HP drops below the threshold during combat; sends the rescue command once per combat encounter
- Sets `rescue_just_fired` flag consumed by the skill engine on the next turn
- Rescue settings UI dialog

### Group Awareness
- Tracks group membership in real time by parsing join/leave messages from MUD output
- `group_members` set (lowercase PC names) shared with the skill engine
- WHO-list cache updated from `WHO` command output; used to distinguish player characters from mobs in combat output
- `group_tank` skill — waits for a group invite, joins with `follow <player>`, then performs continuous tank duty (rescue allies, request heals, communicate)

### AI State Tracking
- Character stat monitoring parsed from MUD prompts: HP, MP, MV (and their maximums), tank%, opp%
- SCORE command parsing: level, class, race, XP, gold, alignment, hunger, thirst
- WHO list parsing: player name, level, class; used by skill engine to distinguish PCs from mobs
- Room graph and exploration state persisted to `profile["ai_state"]`
- Danger-room detection: marks rooms where HP dropped on entry

### LLM Integration
**Supported backends (configured per-host in `mud_client_llm_local.json`):**
- **Ollama** (local) — OpenAI-compatible REST API, configurable endpoint and model
- **Claude** (Anthropic) — configurable model and API key

**Skill engine context sent to the LLM each turn:**
- Skill instructions and SKILL PLAN with current step
- Recent MUD output (compressed during combat)
- Watched stat values (HP, max HP, tank%, opp%, etc.)
- Combat target and rescue/kill event flags
- Command ledger (counts + last-N dispatched commands)
- Group battle snapshot (every 5 combat turns)

### Skill Editor UI
- Accessible via **Settings → Skills → Manage Skills...**
- Tabbed dialog with separate lists for Skills, Templates, and Targets
- New / Edit / Delete on each tab; double-click to edit
- `SkillEditDialog` — in-app form for creating and editing skill definitions, including instructions, watched stats, and plan text

### Utility Tools
- `clear_room_data.py` — interactive script to reset map data for one or all profiles, with automatic backup before clearing
- `export_skill_template.py` — print a skill template as human-readable text; optionally render it with a target's params to preview the exact prompt the LLM will receive

---

## Known Limitations (as of 2026-04-17)

- Generic LLM advisor (exploration mode) is disabled to avoid interference with skill completion; autonomous BFS exploration is not active
- No handling of inventory management (picking up items, using equipment)
- Collision zone resolution is heuristic and can drift over long sessions
- No multi-server or multi-character session support

---

## Future Goals

### Near-Term

- [ ] **Inventory & equipment tracking** — parse `inventory` and `equipment` output; expose to skill engine context
- [ ] **Improved survival logic** — identify and pathfind to healers, food sources, and water sources

### Medium-Term

- [ ] **Visual map display** — render the room graph as a 2D map in a side panel
- [ ] **Quest / objective tracking** — agent can accept, track, and pursue in-game quests
- [ ] **NPC dialogue trees** — structured handling of multi-turn NPC conversations
- [ ] **Multi-profile comparison** — overlay maps from different characters to build a fuller world model

### Long-Term

- [ ] **Memory-augmented LLM** — give the skill engine access to a persistent knowledge base about the game world (NPCs, lore, item locations)
- [ ] **Multi-agent support** — run multiple characters simultaneously, coordinating actions via shared skill state
- [ ] **Plugin / scripting system** — allow users to add custom triggers, aliases, and automation scripts
- [ ] **Web or headless mode** — run the agent without the GUI for server-side or cloud deployments

---

## Technical Stack

| Layer | Technology |
|---|---|
| Language | Python 3.6+ |
| GUI | tkinter (stdlib) |
| Networking | socket, ssl, http.client (stdlib) |
| Threading | threading, queue (stdlib) |
| Data persistence | JSON (profiles, LLM config, UI config) |
| Local LLM | Ollama (OpenAI-compatible API) |
| Cloud LLM | Anthropic Claude API |
| External dependencies | None required for core features |

---

## Project File Overview

```
AIWanderer/
├── mud_client.py              Main GUI application and connection management
├── ai_agent.py                AI state tracker: room graph, stats, WHO list, danger rooms
├── skill_engine.py            LLM-driven skill execution engine
├── llm_advisor.py             LLM backend integration (Ollama & Claude)
├── mud_parser.py              Stateless text parsing utilities
├── session_logger.py          Session log file management
├── clear_room_data.py         Utility: reset room/map data in profiles
├── export_skill_template.py   Utility: print a skill template as human-readable text
├── requirements.txt           Python dependencies
└── README.md                  Setup and usage documentation
```
