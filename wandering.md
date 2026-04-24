# MUD Wandering System

## Overview

The wandering system lets the LLM autonomously explore a MUD and build a room map. The agent navigates through unknown exits, marks important locations as landmarks, and remembers rooms that are dangerous to enter. The harness manages all pathfinding; the LLM only needs to issue high-level commands.

Each time the agent moves into a new room, the harness records a directional link in the room graph. Over time, this graph covers the entire accessible world. The `explore:` command is the primary tool for driving exploration: it finds the nearest room with an unmapped exit and navigates there automatically, then lets the LLM decide which unknown direction to try.

---

## Per-Turn Room Context

Every turn, before any other output, the harness injects a room annotation:

```
[Room: Temple of Midgaard (vnum:3001) — known: n→Market Square, s→City Gate; assumed: w→Road; unknown: e, u]
```

Fields:
- **known** — exits with confirmed two-way links (you have walked through them)
- **assumed** — reverse-constructed links (you came from that direction; going back is likely safe)
- **unknown** — exits listed by the MUD that have never been traversed

Use this line to decide what to explore next, even without issuing `explore:`.

---

## Implemented LLM Commands

### `explore:`

Navigate to the nearest room with at least one **unknown** exit and stop there.

On arrival the harness injects:
```
[Harness: explore: arrived at Dark Alley (vnum:4210) — known: s→Market; assumed: none; unknown: n, e]
```

The LLM then chooses which unknown direction to try based on the room description. Issue the direction as a normal MUD command (e.g. `"n"` or `"northeast"`).

If the whole map appears fully explored:
```
[Harness: explore: map appears complete — no unmapped exits found]
```

Example JSON:
```json
{"commands": ["explore:"], "note": "finding nearest unmapped exit"}
```

Death-trap links are automatically avoided during the BFS search.

---

### `setlandmark:<name>`

Mark the current room as a named landmark. Replaces any existing landmark with the same name. Persisted to the profile immediately.

```json
{"commands": ["setlandmark:healer"], "note": "marking healer location"}
```

Landmarks appear in the system prompt each turn and can be used as `goto:` targets:
```json
{"commands": ["goto:healer"]}
```

---

### `unsetlandmark:<name>`

Remove a previously set landmark.

```json
{"commands": ["unsetlandmark:old_bank"], "note": "landmark no longer valid"}
```

---

### `markdangerous:`

Mark the **last direction taken** as a death-trap link. Future `goto:` and `explore:` pathfinding will never route through this link.

Use this when you notice the room you just entered is harmful (e.g. instant damage, no-flee zone, obvious trap description) but did not actually die. If you do die, the harness records the trap automatically.

```json
{"commands": ["markdangerous:"], "note": "last room caused instant damage"}
```

---

## Automatic Behaviors

### Per-turn room annotation

Always injected at the top of each turn's context (see above). No command needed.

### Death detection and trap recording

When the text `"You are DEAD!"` (or equivalent) is detected:
1. The harness records the last traversed link (previous room + direction) in `profile['death_trap_links']`.
2. Any active `goto:` or `explore:` navigation is cancelled.
3. A system message is shown:
   ```
   [Harness: death trap recorded — north from Temple Square]
   ```

### Death-trap avoidance in pathfinding

Both `goto:` and `explore:` use BFS that silently skips all recorded death-trap links. If a destination can only be reached through a death trap, the harness reports no path found.

---

## Data Storage

All data is persisted in the profile JSON file (`~/.mud_client_profiles.json`).

| Key | Format | Description |
|-----|--------|-------------|
| `profile['landmarks']` | `{name: room_key}` | Named goto: targets |
| `profile['death_trap_links']` | `{"room_key:direction": true}` | Links never to traverse |
| `profile['rooms']` | `{room_key: {name, exits, ...}}` | Room graph nodes |
| `profile['room_links']` | `{room_key: {direction: {dest, assumed}}}` | Room graph edges |

To manually remove a false-positive death trap, delete the relevant entry from `death_trap_links` in the JSON file, or wait for `marksafe:` to be implemented (see Deferred Commands below).

---

## Writing a Wandering Skill

Minimal skill config:

```json
{
  "instructions": "Explore the MUD by finding and walking through unknown exits. Set landmarks at important locations (healers, shops, banks). Use markdangerous: if you enter a room that seems instantly fatal. Recall if HP drops below 50%. Never set complete to true.",
  "watch_stats": ["hp", "max_hp", "hunger", "thirst"]
}
```

Tips:
- Start from a known safe room (e.g. `goto:recall_point` first).
- Set `setlandmark:start` before exploring so you can return.
- Combine `explore:` with `goto:` for structured coverage: explore a zone, then `goto:` back to start and explore a different direction.
- Watch hunger/thirst — a long wander needs food/drink management.

---

## Deferred Commands (Planned, Not Yet Implemented)

The following commands are designed and ready to be built. This section documents their full intended behaviour.

---

### `tagroom:<tag>`

Attach a searchable keyword to the current room. Multiple tags can be set on the same room with multiple calls.

**Storage:** `profile['rooms'][room_key]['tags']` — a list of strings.

**Intended behaviour:**
- `tagroom:shop` — marks the current room as a shop
- `tagroom:healer` — marks it as a healer location
- `tagroom:bank` — marks it as a bank

**Planned goto: extension:** `goto:tag:shop` would navigate to the nearest room whose `tags` list contains `"shop"`. The `_resolve_goto` function would need a new resolution step (before room-name substring search) that performs a BFS-nearest search over rooms with the matching tag.

**Use cases:** The LLM can tag rooms it discovers as shops or services without needing to remember a fixed name. Later it can navigate back with `goto:tag:healer`.

---

### `forgetroom:`

Remove the current room from the map and all links pointing to it.

**Intended behaviour:**
1. Delete `profile['rooms'][current_room_hash]`.
2. Delete `profile['room_links'][current_room_hash]`.
3. Scan all other rooms' link tables and remove any link whose `dest` equals `current_room_hash`.
4. Show a system message confirming deletion.
5. The room will be re-added automatically on the next visit.

**Use case:** The room tracker occasionally misidentifies a score screen or status output as a room. `forgetroom:` lets the LLM clean up the map without manual JSON editing. Should only be used when the LLM is confident the current "room" is a false entry (no description, nonsensical exits, etc.).

**Implementation notes:** Iterate `profile['room_links']` and call `link_val.pop(direction)` for any link whose `_link_dest(link_val)[0] == current_room_hash`. Then pop the room key itself from both dicts.

---

### `marksafe:`

Remove the most recently traversed link from `profile['death_trap_links']`.

**Intended behaviour:**
- Operates on `_death_trap_key(previous_room_hash, last_movement_direction)`.
- Deletes that key from `profile['death_trap_links']` and saves.
- Shows a system message confirming removal.

**Use case:** False positives can occur — a room might record as a death trap because of a mob kill, lag-induced HP loss, or an area-effect spell. `marksafe:` lets the LLM (or the user, via the console) undo an incorrect trap marking without editing the JSON file.

**Implementation notes:** Nearly identical to `markdangerous:` but removes instead of adds. Add as a sibling branch in the `_on_skill_result` command dispatch loop.
