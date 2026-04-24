# goto: Navigation Command

The LLM can navigate to a known location by including a `goto:` command in its
commands list. The harness resolves the target to a room key, runs BFS, and
injects the resulting direction steps — no speedwalk string needed in the skill.

## Target forms

| Form | Example | Resolves to |
|---|---|---|
| Room number | `goto:vnum:3001` | Room with that vnum |
| Landmark | `goto:otto` | Room saved with `setlandmark otto` |
| Mob name | `goto:mob:white rook` | Most recent combat room for that mob |
| Room name | `goto:Temple Square` | First room whose name contains that string |

Room name matching is case-insensitive substring. `goto:temple` matches
"The Temple Square". Use a more specific string if multiple rooms share a
similar name.

## Setting a landmark

While standing in the target room, type in the client:

```
setlandmark otto
```

The client prints a confirmation and saves it. Landmarks persist across sessions.
Any name works: `setlandmark bank`, `setlandmark inn`, etc.

## Example skill instructions

```
You are heading to get healed by Otto, then hunting the white rook.

Plan:
- [ ] go_to_otto: Navigate to Otto using goto:otto
- [ ] ask_heal: Tell Otto to heal you with: tell otto heal
- [ ] wait_heal: Wait until hp reaches max_hp before moving on
- [ ] go_to_rook: Navigate to the white rook's area using goto:mob:white rook
- [ ] kill_rook: Kill the white rook
- [ ] done: Report complete

If you are not in combat and need to reach a place, use a single goto: command.
Do not issue movement commands manually when goto: will do the work.
```

## Example LLM replies

First turn (navigating to Otto):
```json
{
  "commands": ["goto:otto"],
  "complete": false,
  "plan_step": "go_to_otto",
  "note": "Heading to Otto's location"
}
```

After arriving (asking for a heal):
```json
{
  "commands": ["tell otto heal"],
  "complete": false,
  "plan_step": "ask_heal",
  "note": "Asking Otto for a heal"
}
```

## Notes

- `goto:` commands can be mixed with other commands: `["goto:otto", "tell otto heal"]`
- If the target cannot be resolved or no path exists, a `[Skill] goto: no path found` warning is shown and the command is skipped
- The LLM is told its configured landmarks in the system prompt each turn
