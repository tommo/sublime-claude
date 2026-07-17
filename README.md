# Claude Code for Sublime Text

A Sublime Text plugin for [Claude Code](https://claude.ai/claude-code), [Codex CLI](https://github.com/openai/codex), [GitHub Copilot CLI](https://github.com/features/copilot/cli), and any **Anthropic-compatible** provider (DeepSeek, GLM, Kimi, Qwen, OpenRouter, …).

## Requirements

- Sublime Text 4
- Python 3.10+ (for the bridge process)
- One or more CLI backends (authenticated):
  - Claude Code CLI + `claude-agent-sdk`
  - Codex CLI
  - GitHub Copilot CLI via `github-copilot-sdk`
  - Custom Anthropic-compatible provider — base URL + API key (uses the Claude bridge)

```bash
# Claude Code
npm install -g @anthropic-ai/claude-code
claude  # Follow prompts to authenticate
pip install claude-agent-sdk

# Codex CLI (optional)
npm install -g @openai/codex
codex  # Follow prompts to authenticate

# GitHub Copilot CLI (optional)
pip install github-copilot-sdk  # Bundles CLI binary

# Custom Anthropic-compatible provider (optional) — no extra install,
# configure under settings.custom_providers (see Custom Providers below).
```

**Note:** Authenticate your chosen CLI before using this plugin. If you see connection errors, run the CLI in terminal to login.

## Installation

1. Clone or symlink this folder to your Sublime Text `Packages` directory:

   ```bash
   # macOS
   ln -s /path/to/sublime-claude ~/Library/Application\ Support/Sublime\ Text/Packages/ClaudeCode

   # Linux
   ln -s /path/to/sublime-claude ~/.config/sublime-text/Packages/ClaudeCode

   # Windows
   mklink /D "%APPDATA%\Sublime Text\Packages\ClaudeCode" C:\path\to\sublime-claude
   ```

2. Configure your Python path if needed (see Settings below)

### Plugin Devtools (agent self-debug)

While Sublime is running, agents can inspect live sessions / sticky ◎ / goals, reload
the package without restarting ST, and drive host `/goal` from the CLI:

```bash
python3 devtools_cli.py install   # ensure Packages/ClaudeCode symlink
python3 devtools_cli.py ping
python3 devtools_cli.py sessions
python3 devtools_cli.py snapshot
python3 devtools_cli.py composer [view_id]
python3 devtools_cli.py log --tail 80
python3 devtools_cli.py reload --wait 3
python3 devtools_cli.py goal status --view-id N
```

Command Palette: **Claude: Devtools Snapshot / Sessions / Composer / Log / Reload**.  
**Usage doc (kept):** [docs/devtools.md](docs/devtools.md).

## Usage

### Commands

All commands available via Command Palette (`Cmd+Shift+P`): type "Claude"

| Command | Keybinding | Description |
|---------|------------|-------------|
| Switch Session | `Cmd+Alt+\` | Quick panel: active session, new, or switch |
| Query Selection | `Cmd+Shift+Alt+C` | Query about selected code |
| Query File | - | Query about current file |
| Add Current File | - | Add file to context |
| Add Selection | - | Add selection to context |
| Add Open Files | - | Add all open files to context |
| Add Current Folder | - | Add folder path to context |
| Clear Context | - | Clear pending context |
| New Session | - | Start a fresh Claude session |
| Codex: New Session | - | Start a fresh Codex session |
| Copilot: New Session | - | Start a fresh GitHub Copilot session |
| Start Custom Provider Session | - | Start on a pinned Anthropic-compatible provider |
| Manage Anthropic Providers | - | Add/edit/pin/test providers (wizard) |
| Set Default Provider | - | Default backend for a plain New Session |
| Change Provider for Current Session… | `Cmd+Ctrl+Alt+P` | Swap provider mid-session (resumes history) |
| Generate Provider Model Config | - | Fetch a provider's live models → alias mapping |
| Toggle Tasks Fold | `Cmd+Alt+T` | Expand/collapse the Tasks list in-view |
| Undo Message | - | Rewind last conversation turn |
| Search Sessions | - | Search all sessions by title |
| Clear Notifications | - | List and clear active notifications |
| Restart Session | - | Restart current session, keep output view |
| Resume Session... | - | Resume a previous session |
| Switch Session... | - | Switch between active sessions |
| Fork Session | - | Fork current session (branch conversation) |
| Fork Session... | - | Fork from a saved session |
| Rename Session... | - | Name the current session |
| Sleep Session | - | Put session to sleep (free resources) |
| Wake Session | - | Wake a sleeping session |
| Stop Session | - | Disconnect and stop |
| Toggle Output | `Cmd+Alt+C` | Show/hide output view |
| Clear Output | `Cmd+Ctrl+Alt+C` | Clear output view |
| Interrupt | `Alt+Escape` | Stop current query |
| Permission Mode... | - | Change permission settings |
| Manage Auto-Allowed Tools... | - | Configure tools that skip permission prompts |

### Inline Input Mode

The output view features an inline input area (marked with `◎`) where you type prompts directly:

- **Enter** - Submit prompt
- **Shift+Enter** - Insert newline (multiline prompts)
- **@** - Open context menu (add files, selection, folder, or clear context)
- **Cmd+K** - Clear output
- **Alt+Escape** - Interrupt current query

### Loop (Idle Re-prompt)

Keep nudging the agent every time it goes idle. Useful for "until satisfied" workflows.

- `/loop <prompt>` — re-fire prompt as soon as the session becomes idle
- `/loop:<duration> <prompt>` — same, but enforce a minimum gap between fires
  (e.g. `/loop:5m check builds` — wait at least 5 min before re-firing)
- `/loop:cancel` — stop the active loop
- Status bar shows `↻ loop` (or `↻ loop ≥5m`) while active
- Loop only fires when session is idle — never interrupts an in-flight query
- Stops automatically when session sleeps or stops
- Works for all backends (Claude / Codex / Copilot / DeepSeek)
- Same syntax also works as a prefix without slash: `loop:5m check builds`

When a permission prompt appears:
- **Y/N/S/A** - Respond to permission prompts

When viewing plan approval:
- **Y** - Approve plan
- **N** - Reject plan
- **V** - View plan file

### Menu

Tools > Claude Code

### Context Menu

Right-click selected text and choose "Ask Claude" to query about the selection.

## Settings

`Preferences > Package Settings > Claude Code > Settings`

```json
{
    // Path to Python 3.10+ interpreter
    "python_path": "python3",

    // Tools Claude can use without confirmation
    "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],

    // Permission mode: "default", "acceptEdits", "plan", "bypassPermissions"
    "permission_mode": "acceptEdits",

    // Default backend for a bare "New Session": "claude" (default), a built-in
    // (codex/copilot/pi/dsr), or a custom_providers key.
    // "default_backend": "claude",

    // Reasoning effort for fresh Claude sessions: low/medium/high/max.
    // Overridable per-provider (custom_providers.<name>.effort).
    "effort": "high",

    // Custom Anthropic-compatible providers — see "Custom Anthropic-Compatible
    // Providers" below.
    "custom_providers": { /* ... */ }
}
```

### Permission Modes

- `default` - Prompt for all tool actions
- `acceptEdits` - Auto-accept file operations
- `auto` - AI classifier auto-approves (Team/Enterprise plan, Sonnet 4.6+)
- `bypassPermissions` - Skip all permission checks

### Permission Prompt

When in `default` mode, tool actions show an inline prompt:

```
⚠ Allow Bash: rm file.txt?
  [Y] Allow  [N] Deny  [S] Allow 30s  [A] Always
```

- **Y** - Allow this action
- **N** - Deny (marks tool as error)
- **S** - Allow same tool for 30 seconds
- **A** - Always allow this tool (saves to project settings)

### Auto-Allowed Tools

Automatically allow specific tools without permission prompts. Configure via:

**Command:** `Claude: Manage Auto-Allowed Tools...` - UI to add/remove patterns

**Settings:** Add to project `.claude/settings.json` or user `~/.claude/settings.json`:
```json
{
  "autoAllowedMcpTools": [
    "mcp__*__*",        // All MCP tools
    "mcp__plugin_*",    // All plugin MCP tools
    "Read",             // Specific tool
    "Bash"
  ]
}
```

Supports wildcards (`*`) for pattern matching. User-level settings apply to all projects, project settings override.

### Project Settings (.sublime-project)

```json
{
  "settings": {
    "claude_additional_dirs": [
      "/path/to/extra/dir",
      "~/another/dir"
    ],
    "claude_retain": "Important context to preserve across compactions",
    "claude_env": {
      "MY_VAR": "value"
    }
  }
}
```

- **claude_additional_dirs** — Extra `--add-dir` paths for CLI access
- **claude_retain** — Content preserved across context compactions
- **claude_env** — Environment variables passed to bridge

## Custom Anthropic-Compatible Providers

Point the Claude bridge at any third-party Anthropic-compatible endpoint (same env surface as [ccm](https://github.com/nicepkg/ccm) — DeepSeek, GLM, Kimi, Qwen, OpenRouter, …). Each entry lives under `custom_providers` in settings:

```json
"custom_providers": {
  "glm": {
    "label": "GLM",
    "base_url": "https://open.bigmodel.cn/api/anthropic",
    "auth_env_var": "GLM_API_KEY",
    "opus_model": "glm-5.2[1m]",
    "sonnet_model": "glm-5.2[1m]",
    "haiku_model": "glm-4.7",
    "pinned": true,
    "effort": "high"
  }
}
```

Fields: `base_url` (required), `auth_token` or `auth_env_var` (preferred — never store keys inline), `opus_model`/`sonnet_model`/`haiku_model` alias mappings, `label`/`abbrev`, plus:

- **`pinned`** — opt-in flag controlling whether the provider appears in the **quick panels** (Start Custom Provider / Set Default). Defaults `false`, so having every key configured doesn't bloat the picker — pin only the ones you use. Toggle via **Manage Anthropic Providers**.
- **`effort`** — per-provider reasoning override (`low`/`medium`/`high`/`max`); blank → global `effort`.

### Manage UI

**Claude: Manage Anthropic Providers** — a quick-panel wizard: add/edit providers field-by-field with a review/confirm step, plus per-provider **Pin/Unpin**, **Test config** (validates base URL + resolved auth, previews the env), **Generate Model Config** (fetches the provider's live `/v1/models` and maps aliases), and **Duplicate/Delete**. "Edit raw JSON" opens the settings file.

### Change provider on the fly

**Claude: Change Provider for Current Session** (`Cmd+Ctrl+Alt+P`) swaps a running session's provider mid-conversation. Claude Code stores transcripts locally and the Anthropic API is stateless, so the session ID ports across endpoints — history is preserved via `--resume`, not lossy replay. Restricted to the Claude-bridge family (claude + custom providers).

## Context

Add files, selections, or folders as context before your query:

1. Use **Add Current File**, **Add Selection**, etc. to queue context
2. Context shown with 📎 indicator in output view
3. Context is attached to your next query, then cleared

Requires an active session (use **New Session** first).

## Sessions

Sessions are automatically saved and can be resumed later. Each session tracks:
- Session name (auto-generated from first prompt, or manually set)
- Project directory
- Cumulative cost

**Multiple sessions per window** - Each "New Session" creates a separate output view. Switch between them like normal tabs.

Use **Claude: Resume Session...** to pick and continue a previous conversation.

After Sublime restarts, orphaned output views are registered as sleeping sessions. Press Enter or use **Wake Session** to reconnect.

### Sleep/Wake

Sessions can be put to sleep to free bridge subprocess resources while keeping the view:

- **Sleep** — kills the bridge process, view shows `⏸` prefix
- **Wake** — press Enter in a sleeping view, or use **Wake Session** command
- Switch panel shows sleeping sessions with `⏸` indicator
- `auto_sleep_minutes` setting auto-sleeps idle sessions (default: 60, 0 = disabled)

## Order Table

A simple TODO list for human→agent task assignments. Add orders (tasks) that agents can subscribe to and complete.

### Commands

| Command | Keybinding | Description |
|---------|------------|-------------|
| Add Order at Cursor | `Cmd+Shift+O` | Pin an order at current cursor location |
| Add Order | - | Add order without file location |
| Show Order Table | `Cmd+Alt+O` | Open the order table view |

### Order Table View

The order table shows pending and completed orders:

| Key | Action |
|-----|--------|
| `Enter` / `g` | Go to order location |
| `Cmd+Backspace` | Delete order |
| `u` / `Cmd+Z` | Undo deletion |
| `a` | Add new order |

Orders pinned at cursor positions show a bookmark icon in the gutter.

### Agent Subscription

Agents can subscribe to order notifications via MCP:

```
order("subscribe Check for new orders")  # Subscribe with wake prompt
order("list")                            # List all orders
order("pending")                         # List pending orders only
order("complete order_1")                # Mark order as done
```

When a new order is added, subscribed agents receive a notification with order details.

## Output View

The output view shows:

- `◎ prompt ▶` - Your query (multiline supported)
- `⋯` - Working indicator (disappears when done)
- `☐ Tool` - Tool pending
- `✔ Tool` - Tool completed
- `✘ Tool` - Tool error
- Response text with syntax highlighting
- `@done(Xs)` - Completion time
- **Tasks block** - `───── Tasks · X done · Y active · Z pending ─────` with per-item `✓ done / ▸ active / ○ pending`. Folded by default (running always shown, pending capped at 3, done hidden); `Cmd+Alt+T` or **super+click** the banner / "+N more" line to expand.
- **Retry hint** - `⚠ 503 retry 3/10` (muted) appears under the spinner while the provider retries a 429/5xx, and is cleared automatically when content resumes.

View title shows session status:
- `◉` Active + working
- `◇` Active + idle
- `•` Inactive + working
- `⏸` Sleeping (bridge stopped)
- `❓` Waiting for permission/question response

Non-Claude sessions show backend name in tab title and have distinct background colors:
- **Codex** - Green-tinted background
- **Copilot** - Purple-tinted background
- **DeepSeek** - Default background

Supports markdown formatting and fenced code blocks with language-specific syntax highlighting.

## MCP Tools (Sublime Integration)

Allow Claude to query Sublime Text's editor state via MCP (Model Context Protocol).

### Setup

1. Run **Claude: Add MCP Tools to Project** from Command Palette
2. This creates `.claude/settings.json` with MCP server config
3. Start a new session - status bar shows `ready (MCP: sublime)`

### Available Tools

Claude gets two MCP tools:

**`sublime_eval`** - Execute Python code in Sublime's context:
```python
# Available helpers:
get_open_files()                    # List open file paths
get_symbols(query, file_path=None)  # Search project symbols by exact or partial name
goto_symbol(query)                  # Navigate to symbol definition
list_tools()                        # List saved tools

# Available modules: sublime, sublime_plugin
# Use 'return' to return values
```

**`sublime_tool`** - Run saved tools from `.claude/sublime_tools/<name>.py`

### Creating Saved Tools

Save reusable tools to `.claude/sublime_tools/`:

```python
# .claude/sublime_tools/find_references.py
"""Find all references to a symbol in the project"""
query = "MyClass"  # or get from context
symbols = get_symbols(query)
return [{"file": s["file"], "line": s["row"]} for s in symbols]
```

Add a docstring at the top - it's shown when calling `list_tools()`.

### Session Spawning

- `spawn_session(prompt, name?, profile?, persona_id?, backend?, fork_current?)` - Spawn a subsession
- `list_sessions()` - List active sessions in current window
- `list_backends()` - List backends usable as `spawn_session`'s `backend` arg (built-ins + custom providers), with live availability, bridge family, and models
- `list_personas()` - List available personas from persona server
- `list_profiles()` - List profiles and checkpoints

### Alarm System (Event-Driven Waiting)

Instead of polling for subsession completion, sessions can set alarms to "sleep" and wake when events occur. This enables efficient async coordination.

**Usage Pattern:**
```python
# Spawn a subsession
result = spawn_session("Run all tests", name="test-runner")
subsession_id = str(result["view_id"])

# Set alarm to wake when subsession completes (via MCP tool)
set_alarm(
    event_type="subsession_complete",
    event_params={"subsession_id": subsession_id},
    wake_prompt="Tests completed. Summarize results from test-runner."
)
# Main session ends query (goes idle), alarm monitors in background
# When subsession completes, alarm fires and injects wake_prompt
```

**Event Types:**
- `subsession_complete` - Wake when subsession finishes: `{subsession_id: str}`
- `time_elapsed` - Wake after N seconds: `{seconds: int}`
- `agent_complete` - Same as subsession_complete: `{agent_id: str}`

**MCP Tools:**
- `set_alarm(event_type, event_params, wake_prompt, alarm_id=None)`
- `cancel_alarm(alarm_id)`

Subsessions automatically notify the bridge when they complete. The alarm fires by injecting the wake_prompt into the main session as a new query.

## Subagents

### Custom Agents

Define additional agents in `.claude/settings.json`:

```json
{
  "agents": {
    "nim-expert": {
      "description": "Use for Nim language questions and idioms",
      "prompt": "You are a Nim expert. Help with Nim-specific patterns and macros.",
      "tools": ["Read", "Grep", "Glob"],
      "model": "haiku"
    },
    "test-runner": {
      "description": "Use to run tests and analyze failures",
      "prompt": "Run tests and analyze results. Focus on failures.",
      "tools": ["Bash", "Read"]
    }
  }
}
```

- **description** - When Claude should use this agent (use "PROACTIVELY" for auto-invocation)
- **prompt** - System prompt for the agent
- **tools** - Restrict available tools (read-only, execute-only, etc.)
- **model** - Use `haiku` for simple tasks, `sonnet`/`opus` for complex

Agents run with separate context, preventing conversation bloat. Custom agents override built-ins with the same name.

## Architecture

```
┌─────────────────┐     JSON-RPC/stdio     ┌─────────────────┐
│  Sublime Text   │ ◄────────────────────► │  bridge/main.py │ (Claude)
│  (Python 3.8)   │                        │  claude-agent-sdk│
│                 │                        └─────────────────┘
│                 │     JSON-RPC/stdio     ┌─────────────────┐
│                 │ ◄────────────────────► │  bridge/codex_  │ (Codex)
│                 │                        │  main.py        │
│                 │                        └────────┬────────┘
│                 │     JSON-RPC/stdio     ┌────────┴────────┐
│                 │ ◄────────────────────► │  bridge/copilot_│ (Copilot)
│                 │                        │  main.py        │
└─────────────────┘                        └─────────────────┘
        │
        │ Unix socket
        ▼
┌─────────────────┐     stdio     ┌─────────────────┐
│  mcp_server.py  │ ◄──────────► │  mcp/server.py  │
│  (socket server)│              │  (MCP server)   │
└─────────────────┘              └─────────────────┘
```

The plugin runs in Sublime's Python 3.8 environment and spawns a separate
bridge process using Python 3.10+. Each bridge translates between Sublime's
JSON-RPC protocol and the backend CLI:
- **Claude**: `bridge/main.py` — Claude Agent SDK
- **Codex**: `bridge/codex_main.py` — Codex app-server protocol
- **Copilot**: `bridge/copilot_main.py` — GitHub Copilot SDK
- **Custom Anthropic-compatible providers**: `bridge/main.py` — same Claude bridge, pointed at a third-party endpoint via `ANTHROPIC_BASE_URL` + auth (see Custom Providers)

```
sublime-claude/
├── claude_code.py         # Plugin entry point
├── core.py                # Session lifecycle
├── commands.py            # Plugin commands
├── session.py             # Session class
├── output.py              # Output rendering
├── listeners.py           # Event handlers
├── rpc.py                 # JSON-RPC client
├── mcp_server.py          # MCP socket server
├── bridge/
│   ├── main.py            # Claude bridge (Agent SDK)
│   ├── codex_main.py      # Codex bridge (app-server)
│   ├── copilot_main.py    # Copilot bridge (Copilot SDK)
│   └── rpc_helpers.py     # Shared JSON-RPC helpers
├── mcp/server.py          # MCP protocol server
│
└── Core Utilities:
    ├── constants.py       # Config & magic strings
    ├── logger.py          # Unified logging
    ├── error_handler.py   # Error handling
    ├── session_state.py   # State machine
    ├── settings.py        # Settings loader
    ├── prompt_builder.py  # Prompt utilities
    ├── tool_router.py     # Tool dispatch
    └── context_parser.py  # Context menus
```

Both bridges emit identical JSON-RPC notifications to Sublime, so the output view, permissions, and MCP tools work the same regardless of backend.

## License

VCL (Vibe-Coded License) - see LICENSE

The embedded terminal (`terminal/`) is adapted from [Terminus](https://github.com/randy3k/Terminus) by Randy Lai, used under the MIT License — see `terminal/LICENSE_TERMINUS`.
