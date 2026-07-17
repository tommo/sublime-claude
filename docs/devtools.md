# ClaudeCode Devtools — Usage

Self-debug surface for **agents and humans** while Sublime Text is running with ClaudeCode loaded. Prefer the CLI from a terminal or agent shell; do not restart Sublime for normal plugin iteration.

| Surface | Entry |
|---------|--------|
| **CLI** | `python3 devtools_cli.py <action> …` (from package root) |
| **Socket** | `/tmp/sublime_claude_mcp.sock` — `{"op":"debug","action":…}` |
| **MCP** (in-session) | `debug_*` tools via sublime MCP server |
| **Command Palette** | `Claude: Devtools …` |

Source: `devtools.py`, `devtools_cli.py`, `package_reloader.py`.

---

## Install / path

Package should be a symlink into Sublime Packages:

```bash
python3 devtools_cli.py install
```

macOS target:

`~/Library/Application Support/Sublime Text/Packages/ClaudeCode` → this repo

If install reports “already installed,” you’re fine. Socket appears only while ST is running with ClaudeCode loaded (`ping` must return `ok`).

---

## CLI quick reference

Run from the package directory (or pass the full path to `devtools_cli.py`).

```bash
# Health
python3 devtools_cli.py ping

# Host state
python3 devtools_cli.py sessions
python3 devtools_cli.py snapshot              # active / focused Claude sheet
python3 devtools_cli.py snapshot 28           # by view_id
python3 devtools_cli.py composer              # sticky ◎ / pad / viewport
python3 devtools_cli.py composer 28
python3 devtools_cli.py log --tail 80
python3 devtools_cli.py log --grep composer
python3 devtools_cli.py event "note for ring buffer"

# Package reload (no ST restart)
python3 devtools_cli.py reload                # soft (default)
python3 devtools_cli.py reload --wait 3       # wait + re-ping
python3 devtools_cli.py reload --hard         # ignored_packages cycle

# Goal harness (host /goal)
python3 devtools_cli.py goal status --view-id 28
python3 devtools_cli.py goal 'your objective' --view-id 28
python3 devtools_cli.py goal pause --view-id 28
python3 devtools_cli.py goal resume --view-id 28
python3 devtools_cli.py goal clear --view-id 28

# Arbitrary main-thread Python in ST
python3 devtools_cli.py eval '__result__ = list(sublime._claude_sessions)'
```

**Flags (global):**

| Flag | Meaning |
|------|---------|
| `--view-id N` | Target session view id |
| `--tail N` | Log ring depth (default 80) |
| `--grep SUB` | Filter log messages |
| `--hard` | Reload mode = hard |
| `--mode soft\|hard` | Explicit reload mode |
| `--wait SEC` | After `reload`, sleep and re-ping (default 2) |

Options may appear **before** the action (`goal --view-id 28 'obj'`) or after (`goal 'obj' --view-id 28`).

---

## Reload (correct, no ST restart)

Use this after editing plugin Python instead of quitting Sublime.

| Mode | What happens |
|------|----------------|
| **soft** (default) | `plugin_unloaded` → unload all `ClaudeCode.*` → purge `sys.modules` → `sublime_plugin.reload_plugin` on every root `.py` with import interception so **submodules** re-exec (Terminus / AutomaticPackageReloader pattern) |
| **hard** | Add package to `Preferences.ignored_packages`, then remove it (~1s later) — full ST package unload/load |

### After reload

1. Socket briefly dies; CLI `--wait` re-pings until `ok`.
2. `plugin_loaded` **drops in-memory Session objects** (avoids stale class identity). Sheets remain; sessions reattach as **sleeping**.
3. Restore registry if you need immediate host access:

```bash
python3 devtools_cli.py eval '
from ClaudeCode.listeners import settle_startup_claude_views
settle_startup_claude_views()
__result__ = list(sublime._claude_sessions.keys())
'
```

4. Wake a sheet before agent turns / goals that need a live bridge:

```bash
python3 devtools_cli.py eval '
s = sublime._claude_sessions.get(28)
s.wake()
__result__ = {"wake": True, "session_id": s.session_id}
'
# poll until initialized
python3 devtools_cli.py eval '
s = sublime._claude_sessions.get(28)
__result__ = {"initialized": s.initialized, "working": s.working, "sleeping": s.is_sleeping}
'
```

Palette: **Claude: Devtools Reload Package** / **… (hard)**.

---

## Goal harness via devtools

Same path as typing `/goal …` in the sheet (host-owned planning → execute → verify).

```bash
# After wake + initialized
python3 devtools_cli.py goal 'Write /tmp/probe.txt with one line ok' --view-id 28
python3 devtools_cli.py goal status --view-id 28
python3 devtools_cli.py snapshot --view-id 28   # includes goal phase / plan_path
```

**Lifecycle you should see:**

1. Host materializes plan → `{project}/.claude/goals/{goal_id}/plan.md`
2. `phase=executing`, `status=active`, `working=true`
3. Agent implements; may call `update_goal(completed=true)`
4. Host may enter `phase=verifying`
5. `status=complete`, `phase=idle`

Controls: `status` | `pause` | `resume` | `clear` (same as slash commands).

Goal is **not** available on Quick Agent sessions.

---

## What each dump contains

### `ping`

`ok`, socket path, session count, ring event count, `started`, log path.

### `sessions`

All **host** sessions (not only MCP-spawned subsessions): `view_id`, name, backend, working/initialized/sleeping, goal strip, `composer_allowed`, view size.

### `snapshot [view_id]`

Windows summary + all sessions + **focus** block: deep session attrs, goal dump, composer geometry, view settings.

### `composer [view_id]`

Sticky ◎ state: `input_mode`, marker/EOF layout, viewport/layout extent, trailing empty lines, regions, `scroll_past_end`, sleeping flags.

### `log`

- In-process **ring** (survives soft reload; stored on `sublime._claude_devtools`)
- File: `$TMPDIR/claude_devtools.log`
- Bridge tail: `$TMPDIR/claude_bridge.log` (path may follow TMPDIR)

### `eval`

Runs on the ST main thread with `sublime`, `sublime_plugin`, and named helpers (`debug_*`, session tools, etc.).

**Return values:** prefer assigning `__result__` for multi-statement scripts. A leading single-line `return …` is rewritten by the MCP host; semicolon-chained `return` is **not**.

```bash
# Good
python3 devtools_cli.py eval '__result__ = {"n": len(sublime._claude_sessions)}'

# Also good (single expression form handled by host)
python3 devtools_cli.py eval 'return list(sublime._claude_sessions.keys())'
```

---

## Socket protocol

Newline-terminated JSON on `/tmp/sublime_claude_mcp.sock`.

```json
{"op": "debug", "action": "ping"}
{"op": "debug", "action": "snapshot", "view_id": 28}
{"op": "debug", "action": "composer", "view_id": 28}
{"op": "debug", "action": "log", "tail": 80, "grep": "goal"}
{"op": "debug", "action": "reload", "mode": "soft"}
{"op": "debug", "action": "goal", "args": "status", "view_id": 28}
{"op": "debug", "action": "event", "message": "repro note"}
{"code": "return debug_sessions()"}
```

Response envelope: `{"result": …, "error": null|string}`.

CLI prefers `op=debug`; if the live plugin predates that handler, it **hot-imports** `ClaudeCode.devtools` via `eval` + `importlib.reload` so agents still work mid-edit.

---

## MCP tools (agent inside a Claude sheet)

| Tool | Purpose |
|------|---------|
| `debug_ping` | Liveness + counts |
| `debug_sessions` | All host sessions |
| `debug_snapshot` | Full dump (`view_id` optional) |
| `debug_composer` | ◎ / pad / viewport |
| `debug_log` | Ring + files |
| `debug_reload` | Schedule soft/hard reload (`mode`) |
| `debug_goal` | `/goal` harness (`args`, `view_id`) |

These go through the same host functions as the CLI.

---

## Command Palette

| Command | Role |
|---------|------|
| Claude: Devtools Snapshot | Scratch JSON + clipboard |
| Claude: Devtools Sessions | Scratch JSON + clipboard |
| Claude: Devtools Composer | Scratch JSON + clipboard |
| Claude: Devtools Log | Scratch JSON |
| Claude: Devtools Reload Package | Soft reload |
| Claude: Devtools Reload Package (hard) | Hard reload |

---

## Agent playbooks

### Iterate on plugin code

```bash
# edit files…
python3 devtools_cli.py reload --wait 3
python3 devtools_cli.py eval '
from ClaudeCode.listeners import settle_startup_claude_views
settle_startup_claude_views()
__result__ = len(sublime._claude_sessions)
'
python3 devtools_cli.py ping
```

### Debug sticky ◎ / empty rows / focus

```bash
python3 devtools_cli.py sessions
python3 devtools_cli.py composer 28
python3 devtools_cli.py snapshot 28
python3 devtools_cli.py log --grep composer
```

Check: `input_mode`, `tail_has_marker`, `trailing_empty_lines`, `layout_extent` vs `viewport`, `scroll_past_end`, `composer_allowed`, `claude_sleeping`.

### Debug goal stuck “Active”

```bash
python3 devtools_cli.py goal status --view-id 28
python3 devtools_cli.py snapshot --view-id 28
# look at focus.goal.phase / status / plan_path
python3 devtools_cli.py log --grep goal
```

### Dogfood host goal product

```bash
# wake target session, wait for initialized, then:
python3 devtools_cli.py goal 'small objective with clear evidence' --view-id 28
# poll
python3 devtools_cli.py goal status --view-id 28
# plan lives under {project}/.claude/goals/{id}/plan.md
```

### Bridge / spawn hang

```bash
python3 devtools_cli.py log --tail 100
# inspect bridge_tail + ring
python3 devtools_cli.py sessions
```

---

## When to use what

| Symptom | Action |
|---------|--------|
| Code changed, classes still old | `reload` (soft); hard if soft fails |
| Empty rows under ◎ / scroll range | `composer` + `snapshot` |
| Focus thrash / no composer after turn | `sessions` → `composer_allowed`, `input_mode` |
| Goal stuck active after done | `goal status` / `snapshot` → phase |
| Bridge / spawn hang | `log` + bridge tail |
| Need arbitrary host state | `eval` with `__result__ = …` |
| Want agent-visible note in ring | `event "…"` |

---

## Files / sockets

| Path | Role |
|------|------|
| `devtools.py` | Host ring, snapshots, `dispatch`, goal wrapper |
| `devtools_cli.py` | Outside-ST CLI |
| `package_reloader.py` | Soft + hard reload implementation |
| `/tmp/sublime_claude_mcp.sock` | MCP + debug socket |
| `$TMPDIR/claude_devtools.log` | Durable event log |
| `$TMPDIR/claude_bridge.log` | Bridge process log |

---

## Gotchas

1. **Sublime must be running** — no socket ⇒ `ping` fails.
2. **Soft reload clears live Session objects** — settle + wake as needed.
3. **`list_sessions` MCP tool ≠ `debug_sessions`** — the former is only MCP-spawned subsessions; use `sessions` / `debug_sessions` for all host sheets.
4. **Goal needs a live bridge** — wake and wait for `initialized` before `goal 'objective'`.
5. **Hot-reload of only `devtools` via CLI** is fine for dump helpers; **product code** (session, listeners, goal) needs full `reload`.
6. **Hard reload** briefly disables the whole package (menus/commands go away until re-enable).
7. **Sleep banner after reload** — soft reload used to **stack** “Session paused” phantoms (one per reload) because `PhantomSet` GC left orphans that `update([])` on a *new* set could not remove. Fix: process-global registry + `view.erase_phantoms(key)` on clear/show/wake/reload. If you still see multiples from before the fix, run `python3 devtools_cli.py reload` once (or focus sheet → Enter to wake).
