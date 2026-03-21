# AIWanderer Improvement Plan

## Context
The autonomous MUD AI agent can't reliably navigate ("still not able to minimally walk around"). Root causes compound: mob detection false positives trigger premature combat, hunt mode fires too early hijacking BFS exploration, survival loops deadlock on LLM rate limits, and the goal system is purely reactive with no priority queue. Beyond fixing navigation, the user wants better entity recognition, corpse recovery, food finding, probabilistic short-term goals, smarter LLM utilization, area/mob databases, and better use of the Otto helper NPC.

**Guiding principles:**
- Python code for: movement, known resource navigation, combat thresholding, state tracking
- LLM for: parsing novel NPC dialogue, area theme classification, entity ID in ambiguous rooms, stuck-exploration advice

---

## Process

**Before starting each phase:** perform a code review of the files that phase will touch. Present findings to the user and pause for a decision on whether refactoring or cleanup should be done before proceeding with the phase work. Only continue once the user approves.

---

## Phase 0 — Fix "Can't Walk Around" (highest priority)

- [x] **0A: Raise hunt threshold + gate on frontier size** (`ai_agent.py`)
  Change `_hunt_threshold` from 6 → 15. Add frontier guard: only enter hunt mode when `len(self.state.frontier) < 5`.

- [x] **0B: Break survival deadlock loop** (`ai_agent.py`)
  Add `_survival_fail_count` int. Increment each time `_do_survival_action` can't make progress (no source known + LLM rate-limited). When it hits 3, pause survival action for 60 seconds and resume pure BFS. This breaks the "need food → need gold → no targets → LLM → rate-limited → repeat" loop.

- [x] **0C: Fix startup `_waiting_for_room` race** (`ai_agent.py`)
  If `current_room_hash` is None at tick time, send another `look` (not just wait) and back off to 3 seconds. Cap retries at 5.

- [x] **0D: Hardcode Otto's baseline capabilities** (`ai_agent.py`)
  ```python
  OTTO_KNOWN_CAPABILITIES = ['summon', 'heal', 'sanctuary', 'bless', 'armor']
  ```
  Otto's known services:
  - **heal** — restores HP, use when HP < 40%
  - **sanctuary / bless / armor** — protection buffs, apply before adventuring, ask each 2× for extended duration
  - **summon** — teleports character to Otto's location; use as escape route or before requesting services

  Otto does **not** provide food or water.

  Pre-populate `otto_capabilities` on session start so Otto is usable immediately without waiting for `tell otto help`. `buff_times = {}` always cleared on start since buffs don't survive logout. Dynamic discovery via `tell otto help` continues to extend/correct the list.

---

## Phase 1 — MUD Structure Definition + Entity Recognition + Room/Area Database

- [x] **1A: Per-profile MUD structure definition** (`mud_client.py`, `mud_parser.py`)

  MUD room output has a **strict, color-coded ordering**. For this MUD:
  1. **Room title** — ANSI color A
  2. **Room description** — ANSI color B
  3. **Objects in the room** — ANSI color C
  4. **Mobs/PCs in the room** — ANSI color D

  This structure is consistent within a MUD but varies between MUDs. Store in `ai_config`:

  ```json
  "mud_structure": {
    "room_title_color": 33,
    "room_desc_color": 37,
    "object_color": 32,
    "mob_color": 31,
    "exits_color": 36
  }
  ```

  **Color calibration UI** (`mud_client.py`): Since users can see colors but don't know ANSI codes, add a "Configure Room Colors" menu item that opens a calibration dialog:

  1. Scan the last N lines of received MUD text and collect all distinct ANSI color codes seen (parse raw escape sequences already buffered in the client)
  2. Display a dialog showing one representative sample line per detected color, rendered in that actual color (reuse the tkinter tag/color rendering already in the client)
  3. User assigns a label to each color via dropdown: `Room Title | Description | Objects | Mobs/PCs | Exits | Ignore`
  4. On confirm, write the mapping to `ai_config["mud_structure"]` in the profile — persists across sessions

  Only needs to be done once per profile. The existing `room_color` auto-detection can pre-populate the `Room Title` assignment as a hint.

  **Impact on parsing (`mud_parser.py`):** Use ANSI color transitions as section boundaries instead of regex heuristics. Lines in `mob_color` are **definitively** mob/PC lines — no confidence scoring needed. Lines in `object_color` are objects.

  New `parse_room_block(raw_text, mud_structure) -> dict`:
  ```python
  {
    "title": str,
    "description": str,
    "objects": [str],   # lines in object_color
    "mob_lines": [str], # lines in mob_color — extract names from these only
    "exits": str,
  }
  ```

  Extract mob names from `mob_lines` only — simple and reliable:
  ```python
  MOB_LINE_RE = re.compile(r'^(?:A|An|The|[A-Z])\s*(.+?)\s+(?:is|are|stands?|sits?|lies?)\s+here', re.IGNORECASE)
  ```

  **PC filtering:** Cross-reference extracted names against the `who` list cache before any combat targeting.

- [x] **1B: Buff application confirmation** (`mud_parser.py`, `ai_agent.py`)
  Depends on 1A color parsing. Detect Otto's cast message in the mob/system color section:
  ```python
  BUFF_CAST_RE = re.compile(
      r'(?:otto|a \w+)\s+(?:casts?|recites?|utters?)\s+\w+\s+(?:on you|upon you)',
      re.IGNORECASE)
  ```
  On match, mark the buff as confirmed (don't rely solely on the next score parse). If the cast message is absent after sending a buff tell, flag it so the sequence can retry rather than silently setting the provisional expiry.

- [ ] **1C: Fix mob detection false positives** (`ai_agent.py`, `mud_parser.py`)
  Depends on 1A color calibration being complete. Once `mob_color` is known, filter `detect_mobs()` input to only lines arriving in that color. A mob seen once in a `mob_color` line can be trusted immediately — no visit count needed. Wandering mobs qualify on first sight.

  Fallback when color config not yet set: require `npc_danger` win OR 2+ sightings across any rooms before attacking.

- [ ] **1D: Who list caching** (`ai_agent.py`, `mud_parser.py`)
  ~~Issue `who` periodically and parse into `state.pc_names`.~~ Done: `who` issued every 2 minutes, parsed into `state.who_list` (level+class+name), `char_class` and `char_level` updated from self-entry.
  **Remaining:** Before attacking any entity, confirm its name is NOT in `state.who_list` names.

- [ ] **1E: `entity_db` in profile JSON** (`ai_agent.py`, `mud_client.py`)

  **Mobs can wander**, so the data model must separate mob identity from room location. Two structures:

  **`mob_db`** — keyed by mob name (mob identity, persists across rooms):
  ```json
  "mob_db": {
    "mob_name_lower": {
      "display_name": str,
      "rooms_seen": {"room_hash": int},   # room_hash -> sighting count
      "total_sightings": int,
      "first_seen": iso,
      "last_seen": iso,
      "last_room": room_hash,
      "is_wanderer": bool                 # true if seen in 2+ distinct rooms
    }
  }
  ```

  **`entity_db`** — keyed by room hash (spatial index, what's here):
  ```json
  "entity_db": {
    "room_hash": {
      "mob_names": [str],          # mob_db keys seen here (may be transient for wanderers)
      "items_on_ground": [str],
      "features": [str],
      "area_theme": str | null
    }
  }
  ```

  On each room entry: update `mob_db[name]["rooms_seen"][room_hash]` count. Set `is_wanderer = True` if `len(rooms_seen) >= 2`. Update `entity_db[room_hash]["mob_names"]`.

  **For hunt targeting:** query `mob_db` (not per-room), sorted by `last_room` BFS distance. Wanderers use `last_room` as the best guess for where to find them.

- [ ] **1F: Area theme detection via LLM** (one-time per room) (`llm_advisor.py`, `ai_agent.py`)
  New `request_classification(room_name, description, callback)` in `LLMAdvisor` with 30-second rate limit and `max_tokens=5`. Prompt:
  ```
  Classify this MUD area in one word.
  Room: {name}
  Description: {description}
  Reply with one word only: [town, dungeon, forest, swamp, mountain, cave, ocean, plains, graveyard, castle, temple, shop, unknown]
  ```
  Store in `entity_db[room_hash]["area_theme"]`. When area theme changes between adjacent rooms, log `[AI] Area boundary detected: town -> forest`. Called once per room — cheap, high-signal LLM use.

---

## Phase 1G — Housekeeping

- [ ] **1G: Suppress spurious room-parse failure at login** (`mud_client.py`)
  At login, the room tracking flag fires before the character enters the game (e.g. on the password prompt). This produces a harmless `[Room parse failed]` log line once per session. Fix: gate `expecting_room_data = True` on a confirmed in-game state (e.g. after autologin completes and the entry room has been detected at least once), or detect the login menu text and suppress the flag until login is done.

## Phase 2 — Corpse Recovery

- [ ] **2A: Track corpse at death** (`ai_agent.py`)
  Add to `ExplorationState`:
  ```python
  self.corpse_room = None      # room_hash where corpse lies
  self.corpse_time = None      # monotonic time of death
  CORPSE_DECAY_SECS = 600
  ```
  In death detection block: record `corpse_room = current_room_hash`, `corpse_time = time.monotonic()`.

- [ ] **2B: Corpse recovery goal** (`ai_agent.py`)
  In `_survival_action()`, add between "critical HP" and "thirst":
  - Trigger when: corpse_room set, not yet recovered, corpse not expired, HP >= 50%
  - Action: BFS navigate to corpse_room, queue `get all corpse` on arrival
  - On pickup detected: clear `corpse_room`, set `corpse_recovered = True`

- [ ] **2C: Corpse detection patterns** (`mud_parser.py`)
  ```python
  CORPSE_RE = re.compile(
      r'(?:the\s+)?corpse\s+of\s+(?P<name>[A-Za-z\s]+?)\s+(?:is|lies?|rests?)\s+here',
      re.IGNORECASE)
  CORPSE_DECAY_RE = re.compile(
      r'(?:the\s+)?corpse\s+(?:dissolves?|decays?|crumbles?|vanishes?)',
      re.IGNORECASE)
  ```

---

## Phase 3 — Goal Priority System

- [ ] **3A: Replace implicit goal string with priority queue** (`ai_agent.py`)
  New `Goal` dataclass:
  ```python
  @dataclass
  class Goal:
      priority: int          # lower = more urgent
      name: str
      target_room: str | None
      target_name: str | None
      expires: float | None  # monotonic time
      probability_weight: float = 1.0
  ```

  `_select_goal()` returns highest-priority non-expired goal, with `random.choices()` for probabilistic tie-breaking at the same priority level.

- [ ] **3B: Goal priority table** (`ai_agent.py`)

  | Priority | Goal | Trigger | Probabilistic? |
  |---|---|---|---|
  | 0 | `respond_to_combat` | Combat initiated by mob (mob attacked us) | No |
  | 1 | `flee_combat` | HP < 15% in combat | No |
  | 2 | `get_heal` | HP < 25% | No |
  | 3 | `get_buffs` | Any protection buff missing/expired before adventuring | No |
  | 4 | `recover_corpse` | corpse_room set, HP >= 50% | No |
  | 5 | `get_water` | parched/thirsty | No |
  | 6 | `get_food` | starving/hungry | No |
  | 7 | `earn_gold` | hungry + can't afford food | No |
  | 8 | `hunt_mob` | HP >= 40%, beatable mobs known, frontier < 5 | Yes: weight = 1/(dist+1) |
  | 9 | `seek_mobs` | no beatable mobs known, HP >= 60% | Yes: weight = 0.3 |
  | 10 | `explore` | always | Yes: weight = min(1.0, frontier/10) |
  | 11 | `idle_llm` | BFS exhausted, 3+ stuck ticks | No |

  **`respond_to_combat` (priority 0):** Fires immediately when a mob attacks us (detected via `COMBAT_START_RE` with the mob as attacker, not us). Suspends whatever goal was active. Fight back (`kill <mob_name>`) unless the mob is in `npc_danger` with `deaths > 0` and no wins — in which case immediately flee. On combat end (win or flee), resume the previously suspended goal.

  Wandering mobs make this essential: a mob can attack mid-navigation regardless of current goal. The agent must react instantly rather than waiting for the next goal evaluation tick.

  **`get_buffs` (priority 3):** Triggers when any protection buff (sanctuary, bless, armor) has not been applied this session or has been active for > `BUFF_DURATION_SECS` (default 30 min). Buffs don't survive logout so `buff_times` is always cleared on session start, making this trigger on every new session before the first adventure.

  Buff application sequence via Otto:
  1. `tell otto summon` — get to Otto's location first (he can apply buffs remotely via tell, but summon ensures we're safe)
  2. `tell otto sanctuary` × 2 — stacked for extended duration
  3. `tell otto bless` × 2
  4. `tell otto armor` × 2

  Otto can also apply buffs via tell without summon if already nearby. Track application in `state.buff_times = {buff_name: time.monotonic()}`. Re-trigger when `time.monotonic() - buff_times[buff] > BUFF_DURATION_SECS`.

  **`summon` as escape hatch:** When fleeing combat (`flee_combat` goal) AND HP < 25% AND `_otto_can('summon')`, issue `tell otto summon` to teleport to safety rather than navigating. Also usable when `get_heal` or `get_buffs` triggers but Otto's room is unknown or far away.

- [ ] **3C: Active food finding** (`ai_agent.py`)
  Add `find_food_shop` sub-goal at priority 7 when: hunger not None + `food_room` is None + visited > 20 rooms. Boosts BFS toward rooms with `FOOD_VENUE_WORDS` in name (Inn, Market, Tavern, Bakery). Add LLM hint: `"Actively seeking food source. Prefer rooms named Bakery, Inn, Market, Tavern."`

---

## Phase 4 — LLM Utilization Redesign

- [ ] **4A: Specialized prompts per goal** (`llm_advisor.py`)
  Replace single giant prompt with goal-specific factory:
  - `_prompt_explore()` — current logic, trimmed
  - `_prompt_find_food()` — hunger + exits + gold context only
  - `_prompt_survival_resource()` — for get_food/get_water
  - `_prompt_hunt()` — mob distances + HP + gold context
  - `_prompt_stuck()` — for idle_llm goal, full context

- [ ] **4B: On-demand entity identification** (`llm_advisor.py`)
  `request_entity_identification(description, callback)` called only when: `detect_entities()` confidence < 0.5 AND room visited >= 3 times without confirmed mob. Prompt:
  ```
  List monster/NPC names in this MUD room description. One per line. If none: none.
  {description}
  ```
  Updates `entity_db` with high-confidence entries.

- [ ] **4C: Reduce LLM movement calls** (`ai_agent.py`)
  Only call LLM for movement if agent has been stuck (no new rooms) for >= 3 consecutive ticks. After a successful LLM-suggested move (new room), immediately return to BFS without waiting for next LLM window.

- [ ] **4D: Context token management** (`llm_advisor.py`)
  If message > 1500 chars, trim in order: recent_mud_text (→ 10 lines), inventory (→ 5 items), NPC summary (local only).

---

## Phase 5 — Mob Finding

- [ ] **5A: Mob_db hunt targeting** (`ai_agent.py`)
  Extend `_beatable_npcs()` to query `mob_db` directly (not per-room `entity_db`). A mob is a hunt candidate if: `total_sightings >= 1` (wanderers qualify on first sight via color-confirmed detection) AND NOT in `npc_danger` with `deaths > 0`. Navigate to `last_room` as the best guess for wanderers; for stationary mobs use their single known room. Maintain `_beatable_mob_cache` rebuilt only when `mob_db` changes (dirty flag).

- [ ] **5B: Area-based mob seeking** (`ai_agent.py`)
  For `seek_mobs` goal, prefer BFS toward rooms with area_theme matching character level:
  - Level 1-5: town, plains
  - Level 6-10: forest, dungeon
  - Level 11+: cave, castle

---

## Phase 6 — MUD Tick Prediction

MUD events tied to the hour boundary — sunrise, sunset, hunger/thirst messages, spell wearing off, regen pulses — all fire on the **MUD tick** (the moment the MUD clock advances one hour). Knowing when the next tick is due lets the agent:

- Re-request buffs *before* they fall off rather than discovering they're gone mid-combat
- Predict hunger/thirst messages and pre-empt them
- Schedule actions (rest, healing) in the quiet window just after a tick

- [ ] **6A: Tick event detection** (`mud_parser.py`)
  Identify messages that reliably indicate a tick just fired:
  ```python
  TICK_MARKERS = [
      r'the sun rises',
      r'the sun sets',
      r'you feel hungry',
      r'you are hungry',
      r'you feel thirsty',
      r'you are thirsty',
      r'your? \w+ spell? wears? off',
      r'you feel less (?:protected|blessed|armored)',
  ]
  ```
  New `detect_tick(text) -> bool` in `MUDTextParser`. Returns `True` if the text contains a known tick marker.

- [ ] **6B: Tick timestamp tracking** (`ai_agent.py`)
  Add to `ExplorationAgent.__init__`:
  ```python
  self._tick_times = []   # [monotonic_time, ...] of last N confirmed ticks (max 10)
  ```
  In `on_text_received`, call `detect_tick()`. On detection, append `time.monotonic()` to `_tick_times` (cap at 10), then call `_recalibrate_tick_interval()`.

- [ ] **6C: Tick interval calibration** (`ai_agent.py`)
  `_recalibrate_tick_interval()` averages the gaps between consecutive tick observations:
  ```python
  def _recalibrate_tick_interval(self):
      if len(self._tick_times) < 2:
          return
      gaps = [self._tick_times[i+1] - self._tick_times[i]
              for i in range(len(self._tick_times) - 1)]
      interval = sum(gaps) / len(gaps)
      self.state.TICK_SECS = interval   # overrides MUD-time-command calibration
      self._next_tick_at = self._tick_times[-1] + interval
  ```
  Tick-event calibration is more accurate than the `time`-command approach because it observes the actual boundary, not just the current hour. Overrides `TICK_SECS` when >= 2 events observed. Persist `TICK_SECS` via the existing `mud_time_calibration` profile key (add `source: "tick_events"` to distinguish).

- [ ] **6D: Tick prediction use** (`ai_agent.py`)
  Add `_next_tick_at = None` (monotonic). When set:
  - If a protection buff will expire within 1 tick of `_next_tick_at`, schedule `_do_buff_sequence()` to run just before the predicted tick
  - After a tick fires, check hunger/thirst state and queue food/water action immediately rather than waiting for the next survival check
  - Log `[AI] Tick in ~Xs` when within 30 seconds of predicted tick (useful for manual verification)

## Phase 7 — Level Up Handling

- [ ] **7A: Detect level-up and refresh score** (`mud_parser.py`, `ai_agent.py`)
  Detect level-up message (e.g. "You rise a level!") and immediately send `score` to update max_hp, max_mp, max_mv, and char_level. This is low priority since XP gain is currently slow, but important once combat is more effective.
  ```python
  LEVEL_UP_RE = re.compile(r'you (?:rise|gain|advance)\s+a\s+level', re.IGNORECASE)
  ```
  In `on_text_received`: if `detect_level_up()` fires, set `_last_score_request = 0.0` to force score on next tick.

---

## Profile JSON Schema Additions

```json
{
  "mob_db": {
    "large rat": {
      "display_name": "A large rat",
      "rooms_seen": {"a1b2c3": 4, "d4e5f6": 1},
      "total_sightings": 5,
      "first_seen": "2026-03-20T10:00:00",
      "last_seen": "2026-03-20T11:30:00",
      "last_room": "a1b2c3",
      "is_wanderer": true
    }
  },
  "entity_db": {
    "<room_hash>": {
      "mob_names": ["large rat", "city guard"],
      "items_on_ground": ["a rusty sword"],
      "features": ["fountain"],
      "area_theme": "town"
    }
  }
}
```

`ExplorationState` new serialized fields: `corpse_room`, `corpse_time`. `mob_db` and `entity_db` live at the profile top level (not inside `ai_state`) since they persist independently of agent session state.

---

## Critical Files

| File | Changes |
|---|---|
| `ai_agent.py` | Phase 0 fixes, entity_db population, goal system, corpse tracking, hunt optimization, Otto hardcoding |
| `mud_parser.py` | Multi-signal mob detector, corpse detection patterns |
| `llm_advisor.py` | Specialized prompt factory, entity ID request, area classification, context trimming |
| `mud_client.py` | entity_db initialization on profile create/load |

## Verification

1. Agent walks multiple rooms without getting stuck in combat or survival loops (Phase 0)
2. Room with `"A large rat is here."` → `entity_db` populated with confidence >= 0.8 (Phase 1A)
3. Die, respawn → agent navigates to corpse room and issues `get all corpse` (Phase 2)
4. `~/.mud_client_profiles.json` shows `entity_db` key after exploration (Phase 1B)
5. Session log shows `[AI] Area boundary detected:` lines at area transitions (Phase 1C)
6. Otto responds to `tell otto heal` immediately on session start (Phase 5A)
