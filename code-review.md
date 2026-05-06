# AIWanderer — Code Review

**Date:** 2026-04-30  
**Reviewer:** Claude (Sonnet 4.6)  
**Branch:** feature/wandering

---

## 1. Summary

AIWanderer is a Python tkinter MUD client with real-time LLM integration (Ollama / Claude). The architecture is sound: parsing, state tracking, skill execution, and LLM I/O are cleanly separated into distinct modules. The code is readable, well-commented where it matters, and thoughtfully designed for a personal project of this scope.

### File inventory

| File | Lines | Role |
|---|---|---|
| `mud_client.py` | 5,462 | Main GUI app, connection, room parsing, skill triggering |
| `mud_parser.py` | 1,648 | Stateless MUD text parsing (40+ regex patterns) |
| `skill_engine.py` | 821 | LLM-driven task execution engine |
| `llm_advisor.py` | 709 | LLM backend integration (Ollama, Claude) |
| `ai_agent.py` | 432 | Room graph, pathfinding, stats tracking |
| `session_logger.py` | 171 | Session logging to `~/mud_sessions/` |
| `clean_hash_rooms.py` | 172 | Utility: prune hash-keyed rooms |
| `export_skill_template.py` | 137 | Utility: export skill templates |
| **Total** | **9,552** | |

### Health ratings

| Category | Rating | Notes |
|---|---|---|
| Architecture | Good | Clean module separation, sound threading model |
| Correctness | Fair | A few real bugs; one data-loss risk on profile corruption |
| Code cleanliness | Good | Minor duplication and silent-failure patterns |
| Organization | Fair | `mud_client.py` is overloaded; no tests |
| Security | Good | Config split keeps API keys off shared profiles |

---

## 2. Potential Bugs

### B1 — ~~Bare `except:` in `cleanup_connection()`~~ **FIXED** — `mud_client.py:1406, 1412`

```python
def cleanup_connection(self):
    if self.ssl_socket:
        try:
            self.ssl_socket.close()
        except:          # <-- catches SystemExit, KeyboardInterrupt, etc.
            pass
        self.ssl_socket = None
    if self.socket:
        try:
            self.socket.close()
        except:
            pass
        self.socket = None
```

A bare `except:` intercepts `SystemExit` and `KeyboardInterrupt`, which can prevent clean shutdown. Socket `.close()` raises only `OSError` in practice.

**Fix:** Replace both with `except OSError:`.

---

### B2 — ~~`_pending_payload` / `_pending_on_result` never initialized~~ **FIXED** — `skill_engine.py:243, 290`

`__init__()`, `start()`, and `stop()` all initialize `self._pending = False` but never declare `self._pending_payload` or `self._pending_on_result`. The first assignment of these attributes happens at lines 346 and 349 (inside the `if self._busy:` branch of `on_prompt()`), which always runs before any read at lines 343 and 455 — so it's safe today.

The risk: any future refactor that reads these attributes before the first busy-turn path executes will raise `AttributeError` with no obvious cause.

**Fix:** Add to `__init__()` and `stop()`:
```python
self._pending_payload = None
self._pending_on_result = None
```

---

### B3 — ~~Race condition: `ssl_socket` nulled on main thread while receive thread uses it~~ **FIXED** — `mud_client.py:1421`

`receive_data()` runs in a daemon thread:
```python
while self.connected:
    data = self.ssl_socket.recv(4096)   # line 1415
```

`cleanup_connection()` runs on the main thread and sets `self.ssl_socket = None` (line 1402) without any lock. Between the `while self.connected:` check and the `.recv()` call, the main thread can call `disconnect()` → `cleanup_connection()`, producing:

```
AttributeError: 'NoneType' object has no attribute 'recv'
```

The `connected` flag is a soft guard, not a synchronization primitive.

**Fix:** Wrap the receive path to handle shutdown races:
```python
try:
    data = self.ssl_socket.recv(4096)
except (OSError, AttributeError):
    self.message_queue.put(("disconnect", "Connection lost\n"))
    break
```
This makes the existing try/except at line 1414 comprehensive enough to cover the race.

---

### B4 — ~~Profile load failure silently returns empty dict, enabling subsequent save to destroy user data~~ **FIXED** — `mud_client.py:625–632`

```python
except Exception as e:
    print(f"Error loading profiles: {e}")
    return {'_settings': {}}
```

If the profile JSON is corrupt (incomplete write, disk error, manual edit mistake), the app starts with an empty profile set. The next action that calls `save_profiles()` — including window resize — will overwrite the user's file with `{"_settings": {}}`, permanently destroying all room maps, skills, and character profiles.

**Fix:** At minimum, warn visually (consistent with how `save_profiles()` handles errors):
```python
except Exception as e:
    messagebox.showerror(
        "Profile Load Error",
        f"Could not load profiles from {self.profiles_file}:\n{e}\n\n"
        "The file will NOT be overwritten until you save explicitly."
    )
    self._profiles_load_failed = True   # guard save_profiles() against overwrite
    return {'_settings': {}}
```

---

### B5 — ~~`_summary_worker` silently swallows all exceptions~~ **FIXED** — `llm_advisor.py:270`

```python
except Exception:
    pass
```

A session summary failure produces zero feedback — no UI message, no log entry. The user has no way to know whether a summary was generated or not.

**Fix:** Log the failure:
```python
except Exception as e:
    self.client.session_logger.log_error(f"Session summary failed: {e}")
```

---

## 3. Code Cleanliness

### C1 — ~~Direction mapping defined twice~~ **FIXED** — `mud_client.py:144`, `ai_agent.py:32`

`MUDClient.direction_map` and `ai_agent.DIRECTION_ABBREVS` are the same logical structure:

```python
# mud_client.py
self.direction_map = {
    'n': 'north', 'north': 'north',
    's': 'south', 'south': 'south',
    ...
}

# ai_agent.py
DIRECTION_ABBREVS = {
    'n': 'north', 'north': 'north',
    's': 'south', 'south': 'south',
    ...
}
```

Any future extension (e.g., `ne`, `nw`, `enter`) must be applied in two places. The `mud_client.py` version also includes `'l': 'look'` which is absent from `DIRECTION_ABBREVS`, creating a subtle divergence.

**Fix:** Define `DIRECTION_ABBREVS` once in `ai_agent.py` (already there), import it in `mud_client.py`, and derive `direction_map` from it (adding `'look'` there).

---

### C2 — ~~`print()` used for user-visible errors~~ **FIXED** — `mud_client.py:625` (B4), `session_logger.py:71`

Both locations use `print()` for warning/error output:
- `mud_client.py:625`: profile load error
- `session_logger.py:71`: log file open failure

Once the app is launched without a terminal (desktop shortcut, `.app` bundle), these messages are invisible. The app already has `messagebox.showerror()` and `session_logger` for this purpose.

---

### C3 — ~~`start()` and `stop()` in `SkillEngine` duplicate all reset logic~~ **FIXED** — `skill_engine.py:261`

Both methods set the same 12 instance attributes to identical initial values. The only behavioral difference is that `start()` sets `_skill_name`, `_skill_cfg`, and parses the plan:

```python
def start(self, name, cfg):
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
    # ... 6 more lines identical to stop()
```

**Fix:** Extract a `_reset_state()` method containing the shared lines; call it from both `start()` and `stop()`. Cuts ~15 lines of drift risk.

---

### C4 — Repeated `master.after(0, lambda ...)` boilerplate — throughout `llm_advisor.py` and `skill_engine.py`

The pattern:
```python
master.after(0, lambda m=msg: self.client.append_text(f"[Skill error: {m}]\n", "error"))
```
appears roughly 30 times with minor variations. It's not a bug, but the variable-capture lambda idiom (`m=msg`) is easy to get wrong, and the repetition makes the threading flow harder to follow.

A two-line helper on `MUDClient` or `LLMAdvisor` would centralize this:
```python
def _ui(self, fn, *args):
    self.client.master.after(0, lambda: fn(*args))
```

---

### C5 — Undocumented reliance on CPython GIL for `self._messages` list safety — `skill_engine.py:383`

```python
msgs = list(self._messages)   # snapshot taken in background thread
```

`self._messages` is appended on the main thread (`_fire_turn`, line 366) and snapshotted in the worker thread. In CPython, `list()` on a list is effectively atomic due to the GIL. This is fine in practice but relies on an undocumented implementation detail. `LLMAdvisor` correctly uses `self._messages_lock` for the same pattern.

**Fix:** Add a `threading.Lock` to `SkillEngine` (matching `LLMAdvisor`'s pattern) and acquire it around both the append in `_fire_turn` and the `list()` in `_worker`.

---

## 4. Organizational Improvements

### O1 — ~~`mud_client.py` is 5,462 lines with 187 methods and 9 classes~~ **FIXED**

The file currently hosts:
- 1 main application class (`MUDClient`) with ~179 methods
- 8 standalone dialog classes at the bottom (lines 4626–5462):
  `RunOnceDialog`, `AIConfigDialog`, `ColorCalibrationDialog`, `ProfileDialog`,
  `SkillsDialog`, `SkillEditDialog`, `TemplateEditDialog`, `TargetEditDialog`

The dialog classes are self-contained and have no dependencies on `MUDClient` internals beyond being passed data at construction. Moving them to `ui_dialogs.py` would:
- Remove ~740 lines from `mud_client.py` (−14%)
- Make the dialogs independently readable and testable
- Require a single import change: `from ui_dialogs import RunOnceDialog, ...`

This is the lowest-risk, highest-readability split available.

---

### O2 — No automated tests

The codebase has zero test files. The highest-value targets:

| Module | Why test it |
|---|---|
| `mud_parser.py` | 40+ hand-crafted regexes; a MUD format change silently breaks parsing; pure/stateless so trivial to test |
| `ai_agent.PathFinder` | BFS over room graph; correctness is critical to safe navigation; pure function |
| `skill_engine._parse()` | JSON extraction from LLM responses; handles malformed input; pure function |
| `skill_engine.render_skill()` | Placeholder substitution; KeyError paths need coverage |

Even 20–30 pytest tests with captured MUD text fixtures would provide a meaningful regression net, especially for `mud_parser.py` where a broken pattern can silently mis-classify combat events or room exits.

---

### O3 — ~~No keepalive / heartbeat for idle sessions~~ **FIXED**

If the MUD server drops a TCP connection silently (NAT timeout, server restart), the client doesn't detect it until the user types a command or the next skill turn fires. The receive thread's `ssl_socket.recv(4096)` will block indefinitely.

**Fix:** Schedule a periodic IAC NOP (or any harmless keepalive byte) via `master.after()` every 60 seconds while connected. Alternatively, set `socket.settimeout()` to a finite value in `receive_data()` and treat `socket.timeout` as a keepalive check point rather than a hard error.

---

### O4 — Session logger startup failure is invisible once terminal is gone — `session_logger.py:71`

```python
print(f"Warning: session logging disabled — {e}")
```

When the logger silently disables itself (disk full, permission error), the user loses the entire session log with no visible indication. Since this happens at connection time, the client could instead call `self.append_text(f"[Warning: session log disabled — {e}]\n", "system")` which would appear in the main output pane.

---

## 5. Additional Findings (from secondary review)

### A1 — ~~`max_tokens` values are bare literals scattered across call sites~~ **FIXED** — `llm_advisor.py:266, 400`, `skill_engine.py:400`

Four different token limits appear as magic numbers:

```python
# llm_advisor.py
self._call_backend(..., max_tokens=256)    # initial context
self._call_backend(..., max_tokens=1000)   # session summary
self._call_backend(..., max_tokens=1024)   # advice / direct chat

# skill_engine.py
self._call_llm(..., max_tokens=2048)       # skill turns
```

These are tuning knobs — changing the skill turn budget requires finding the right call site by memory. Named constants at the top of each module (or in a shared `config.py`) would make them discoverable and easy to adjust.

**Fix:** Define e.g. `_MAX_TOKENS_SKILL_TURN = 2048` at module level and reference it at the call site.

---

### A2 — No type hints anywhere in the codebase

The codebase has no type annotations. Given the complexity of the threading model and the fact that `MUDClient` is passed as a `client` argument into `LLMAdvisor`, `SkillEngine`, `ExplorationAgent`, and `SessionLogger`, the lack of hints makes it hard to reason about what each module expects without reading all the callers.

Even partial annotations on public method signatures (class constructors, `on_prompt`, `_call_backend`, `render_skill`) would let a type checker (`mypy`, `pyright`) catch argument mismatches and `None`-dereference bugs statically.

**Fix:** Start with the inter-module boundary methods. No need to annotate private helpers or the entire `MUDClient`.

---

### A3 — ~~Main display text widget accumulates unlimited text over long sessions~~ **FIXED** — `mud_client.py`

`session_logger` caps its file output via line buffering, `skill_engine._messages` is capped at `2 * HISTORY_TURN_PAIRS` entries, and `_raw_ansi_lines` is a `deque(maxlen=500)`. However, the main tkinter `ScrolledText` widget has no pruning: every line received from the server is appended for the lifetime of the connection. An active multi-hour session with verbose MUD output can accumulate tens of thousands of lines, increasing UI repaint cost and resident memory noticeably.

**Fix:** Periodically trim the text widget — e.g., when it exceeds N lines, delete the oldest N/2 from the top. A threshold of ~5,000 lines with a trim to 2,500 is unnoticeable to the user and keeps memory bounded.

---

## 6. Minor Notes

- **`mud_client.py:625`** — `print()` on profile load error is the only error path that doesn't use `messagebox`. Inconsistent with the rest of the error handling.
- **`llm_advisor.py:18`** — The `claude_model` default is `claude-haiku-4-5-20251001`. As of April 2026, `claude-haiku-4-5` is current; no action needed but worth watching on next model rotation.
- **`clean_hash_rooms.py`** — Solid defensive utility. The backup-before-mutate pattern (`shutil.copy2`) is good practice.
- **`export_skill_template.py`** — The `--render-target` warning at line 122 correctly surfaces mis-bound templates. Printing to stdout is appropriate here since it's a CLI tool.
- **Passwords in plaintext** — `mud_client_profiles.json` stores autologin passwords in plaintext. This is user-controlled and noted in README, so no action required — but worth a one-line comment in the code near where the field is read.
