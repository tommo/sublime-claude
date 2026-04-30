# Claude Terminal

An embedded PTY terminal view shared between you and the agent. You type in it like a normal terminal; the agent can run commands and read output without separate subprocess calls.

## Opening a Terminal

**Command Palette** → `Claude Terminal: Open`

Or via agent: any `terminal_run` call auto-opens a terminal for that session.

Each terminal tab shows `▫ #N Name` (idle) or `▶ #N Name` (busy). The `#N` index is how you point an agent at your terminal.

## Using as a User

Standard terminal — type commands, use arrow keys, paste with Cmd+V. The view is read-only from Sublime's perspective; all input routes through the PTY keymap.

**Key shortcuts:**

| Key | Action |
|-----|--------|
| Any key | Sent to shell |
| Cmd+V | Paste clipboard |
| Cmd+C | Copy selection |

## Agent MCP Tools

### `terminal_list`

Returns all open terminals with their index, tag, title, and state (`idle`/`running`/`exited`).

```
terminal_list()
→ [{"index": 1, "tag": "...", "title": "▫ #1 Terminal", "state": "idle"}]
```

### `terminal_run(command, wait=30, index=None)`

Run a command and wait for it to finish. Returns the captured output.

```
terminal_run("ls -la")            # session's own terminal
terminal_run("npm test", wait=60) # longer timeout
terminal_run("ls", index=1)       # user's terminal #1
terminal_run("npm run dev", wait=0) # fire-and-forget (read later)
```

**`wait`**: seconds to block. When the shell returns to prompt, capture ends — typically within 100ms for fast commands. Interactive sub-processes (Lua REPL, Python REPL, SSH remote prompt) use 300ms output quiescence as the signal instead.

**`index`**: target a specific terminal the user has open. Shown as `#N` in the tab title. Omit to use the session's own dedicated terminal.

Set `wait=0` for long-running processes (dev servers, watchers); use `terminal_read` to check progress.

### `terminal_read(index=None, lines=100)`

Read the current terminal screen buffer (last N lines). Useful after `wait=0` runs.

```
terminal_read()           # session terminal
terminal_read(index=1)    # user's #1 terminal
terminal_read(lines=50)   # only last 50 lines
```

### `terminal_send(text, index=None)`

Send raw text/keystrokes without blocking. Useful for sending input to interactive programs already running in the terminal.

```
terminal_send("q\n")        # quit a REPL
terminal_send("\x03")       # Ctrl-C
terminal_send("exit\n")     # exit SSH
```

### `terminal_close(index=None)`

Close and terminate a terminal. Rarely needed — session terminals close with the session.

## Targeting User Terminals

When you open a terminal and want the agent to use it:

1. Open it: **Claude Terminal: Open**
2. Note the `#N` in the tab title (e.g., `▫ #2 Terminal`)
3. Tell the agent: _"use terminal #2"_ or pass `index=2` directly

The agent can also discover your terminals with `terminal_list()`.

## Shell Integration

For **zsh**, **bash**, and **fish**, the terminal injects shell hooks on startup:

- **OSC 133** (`\e]133;A\a`) — emitted before each prompt, signals command completion instantly
- **OSC 7** (`\e]7;file://host/path\a`) — tracks current working directory for session restore

This makes `ls` return in ~50ms instead of waiting for a timeout heuristic.

Other shells (ksh, sh, csh) fall back to PGID-based idle detection (~100ms).

## SSH and Remote Shells

SSH sessions use output quiescence (300ms silence = done) since the remote shell's process state isn't visible locally.

```
# Connect — wait=10 gives time for login banner + prompt
terminal_run("ssh mini", wait=10)

# Remote commands — output stops when remote prompt appears
terminal_run("ls /etc", wait=10)
terminal_run("git log --oneline -20", wait=15)
```

For interactive remote sessions, prefer `wait=0` + `terminal_read` polling, or `terminal_send` to provide input.

## Interactive REPLs

REPLs (Python, Lua, Node, etc.) also use 300ms quiescence since they stay as the foreground process.

```
terminal_run("python3", wait=5)       # wait for >>> prompt
terminal_run("import sys", wait=2)    # each line
terminal_send("exit()\n")             # quit
```

Or launch and leave running with `wait=0`, then feed input via `terminal_send`.

## Session Persistence

When Sublime restarts, terminal tabs reopen and reconnect to a new shell in the same working directory (restored from OSC 7). The `#N` index is preserved so existing references still work.

## Detection Strategy Reference

| Context | Method | Latency |
|---------|--------|---------|
| Local zsh/bash/fish | OSC 133 shell hook | ~50ms |
| Local shell, no integration | PGID idle (tcgetpgrp) | ~100ms |
| Local subprocess (REPL, build) | Output quiescence | ~300ms |
| SSH / direct program | Output quiescence | ~300ms |
| Timeout | `wait` seconds exceeded | configurable |
