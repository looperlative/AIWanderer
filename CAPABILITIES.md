# AIWanderer — Capabilities & Roadmap

> Last updated: 2026-03-15

## Overview

AIWanderer is an AI-powered MUD (Multi-User Dungeon) client that combines a traditional graphical client with an autonomous exploration agent. The application connects to MUD servers, provides a real-time GUI for manual play, and includes an AI agent capable of exploring the game world with minimal human intervention.

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

### Profile Management
- Named connection profiles stored in `~/.mud_client_profiles.json`
- Per-profile: host, port, SSL mode, character name, password
- Auto-login sequences with learned prompt-response pairs
- "Run once" vs. "always" trigger modes for automation
- Prompt normalization — replaces numeric stat values with wildcards so prompts match across sessions
- Last-used profile auto-selection on startup

### Room Mapping & World Model
- Real-time room tracking as the player moves
- Hash-based room identity (room description → unique ID)
- Exit detection from room description text
- Bidirectional room link graph stored per-profile
- Collision zone detection — distinguishes physically different rooms that share identical descriptions using (x, y, z) coordinate tracking
- Persistent map data survives across sessions

### Session Logging
- Timestamped log files written to `~/mud_sessions/session_YYYY-MM-DD_HH-MM-SS.log`
- Separate log categories: MUD output, player commands, AI commands, AI reasoning, system events, errors
- ANSI escape code stripping for clean, readable logs

### Autonomous Exploration Agent
- Breadth-first search (BFS) over the room graph to systematically visit unmapped areas
- Tick-based event loop integrated with the tkinter main thread (no threading race conditions)
- Character stat monitoring parsed from MUD prompts: HP, MP, MV (and their maximums)
- SCORE command parsing: level, class, race, XP, gold, alignment

**Survival logic (prioritized over exploration):**
- Low HP detection → rest or flee
- Hunger/thirst detection → seek food and water
- Dark room detection → avoid or seek a light source
- Dangerous room tracking — marks and avoids rooms where the agent was harmed

**Situational awareness:**
- Combat detection (start, rounds, flee prompts)
- Death detection with automatic respawn wait and exploration resume
- NPC/mob name extraction
- Dead-end and loop detection

### LLM Integration (Two-Tier Decision Making)
- **Tier 1 (rule-based):** BFS exploration handles all standard movement decisions
- **Tier 2 (LLM-based):** Called when BFS is exhausted or for situations requiring judgment (NPC dialogue, locked doors, puzzles, navigation choices)

**Supported LLM backends:**
- **Ollama** (local) — OpenAI-compatible REST API, default endpoint `http://localhost:11434`, configurable model (default: `llama3.1:8b`), no API key required
- **Claude** (Anthropic) — configurable model (default: `claude-haiku-4-5-20251001`), requires API key in profile config

**Context sent to the LLM:**
- Current room name, description, available exits (reported vs. mapped)
- Character stats (HP/MP/MV with max values, level, class, XP, gold, alignment)
- Survival state (hunger, thirst)
- Recent action history (last 6 actions)
- Recent MUD text (last 20 lines)
- Map progress statistics

**Rate limiting:** minimum 8-second interval between LLM calls; non-blocking background thread with callback prevents UI freezes.

### Utility Tools
- `clear_room_data.py` — interactive script to reset map data for one or all profiles, with automatic backup before clearing

---

## Known Limitations (as of 2026-03-15)

- Agent cannot yet reliably navigate from an arbitrary room to a known destination (pathfinding beyond BFS frontier is incomplete)
- No handling of inventory management (picking up items, using equipment)
- No spell or skill usage by the AI agent
- LLM context does not include inventory or equipment state
- Collision zone resolution is heuristic and can drift over long sessions
- No multi-server or multi-character session support

---

## Future Goals

### Near-Term

- [ ] **Pathfinding to known rooms** — A* or Dijkstra over the existing room graph so the agent can navigate to previously visited locations (e.g., shops, healers)
- [ ] **Inventory & equipment tracking** — parse `inventory` and `equipment` output; expose to LLM context
- [ ] **Spell/skill usage** — agent aware of available abilities and mana cost; use them in combat or for exploration (e.g., `cast fly` to access otherwise unreachable exits)
- [ ] **Improved survival logic** — identify and pathfind to healers, food sources, and water sources rather than wandering
- [ ] **Better combat decisions** — flee threshold tuning, target selection, attack command variety

### Medium-Term

- [ ] **Visual map display** — render the room graph as a 2D map in a side panel
- [ ] **Quest / objective tracking** — agent can accept, track, and pursue in-game quests
- [ ] **NPC dialogue trees** — structured handling of multi-turn NPC conversations
- [ ] **Configurable AI personality / goals** — user-settable objectives (explore, grind XP, accumulate gold, etc.)
- [ ] **Multi-profile comparison** — overlay maps from different characters to build a fuller world model

### Long-Term

- [ ] **Full LLM-driven agent mode** — replace the BFS tier with a fully LLM-driven planner for complex, open-ended play
- [ ] **Memory-augmented LLM** — give the LLM access to a persistent knowledge base about the game world (NPCs, lore, item locations)
- [ ] **Multi-agent support** — run multiple characters simultaneously, coordinating actions
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
| Data persistence | JSON (`~/.mud_client_profiles.json`) |
| Local LLM | Ollama (OpenAI-compatible API) |
| Cloud LLM | Anthropic Claude API |
| External dependencies | None required for core features |

---

## Project File Overview

```
AIWanderer/
├── mud_client.py        Main GUI application and connection management
├── ai_agent.py          Autonomous exploration agent (BFS + survival logic)
├── llm_advisor.py       LLM backend integration (Ollama & Claude)
├── mud_parser.py        Stateless text parsing utilities
├── session_logger.py    Session log file management
├── clear_room_data.py   Utility: reset room/map data in profiles
├── requirements.txt     Python dependencies
└── README.md            Setup and usage documentation
```
