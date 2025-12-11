# Development Notes

## Architecture

```
sublime-claude/
├── claude_code.py     # Plugin commands, plugin_loaded hook
├── session.py         # Session management, bridge communication
├── rpc.py             # JSON-RPC client
├── output.py          # Structured output view with region tracking
├── bridge/
│   └── main.py        # Python 3.10+ bridge using claude-agent-sdk
```

## Key Findings

### Sublime Text Module Caching
- Sublime caches imported modules aggressively
- Touching `claude_code.py` triggers reload of all `.py` files in package root
- Enum classes cause issues when cached - switched to plain string constants
- Dataclass definitions also get cached

### Claude Agent SDK

**Installation:**
```bash
pip install claude-agent-sdk
```

**Basic Usage:**
```python
from claude_agent_sdk import ClaudeAgent, ClaudeAgentOptions

options = ClaudeAgentOptions(
    cwd="/path/to/project",
    allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
    permission_mode="default",  # or "acceptEdits", "bypassPermissions"
    can_use_tool=my_permission_callback,  # async callback
    resume=session_id,  # optional: resume from previous session
    agents=my_agents_dict,  # optional: custom subagents
)

agent = ClaudeAgent(options)
async for msg in agent.query("Your prompt here"):
    # Handle messages
    pass
```

**Message Flow:**
```
SystemMessage           → initialization (subtype: "init" or "compact_boundary")
AssistantMessage        → contains ToolUseBlock (tool request) or TextBlock (response)
  ├─ ToolUseBlock       → tool_name, tool_input, tool_use_id
  └─ TextBlock          → text content
UserMessage             → contains ToolResultBlock (⚠️ NOT in AssistantMessage!)
  └─ ToolResultBlock    → tool_use_id, content (result or error)
ResultMessage           → completion (session_id, duration_ms, total_cost_usd)
```

**Permission Callback:**
```python
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

async def can_use_tool(tool_name: str, tool_input: dict, context) -> PermissionResult:
    # ⚠️ MUST return dataclass objects, NOT plain dicts
    if user_allowed:
        return PermissionResultAllow(updated_input=tool_input)
    else:
        return PermissionResultDeny(message="User denied")
```

**Subagents (AgentDefinition):**
```python
from claude_agent_sdk import AgentDefinition

agents = {
    "my-agent": AgentDefinition(
        description="When to use this agent",
        prompt="System prompt for the agent",
        tools=["Read", "Grep"],  # restrict tools (optional)
        model="haiku",  # haiku/sonnet/opus (optional)
    )
}
options = ClaudeAgentOptions(..., agents=agents)
```

**⚠️ PITFALLS:**

1. **ToolResultBlock location** - Results are in `UserMessage.content`, NOT `AssistantMessage`
   ```python
   # WRONG: looking for tool result in AssistantMessage
   # RIGHT: check UserMessage for ToolResultBlock
   if isinstance(msg, UserMessage):
       for block in msg.content:
           if isinstance(block, ToolResultBlock):
               # Handle result
   ```

2. **Permission callback return type** - Must return `PermissionResultAllow`/`PermissionResultDeny` dataclasses
   ```python
   # WRONG: return {"allow": True}
   # RIGHT: return PermissionResultAllow(updated_input=tool_input)
   ```

3. **Denied tool handling** - SDK sends `TextBlock` directly when denied, no `ToolResultBlock`
   - Must manually mark tool as error in your UI when permission denied

4. **AgentDefinition required** - Agents dict must use `AgentDefinition` objects
   ```python
   # WRONG: agents = {"name": {"description": "...", "prompt": "..."}}
   # RIGHT: agents = {"name": AgentDefinition(description="...", prompt="...")}
   ```

5. **Async iteration** - Must use `async for` to iterate messages
   ```python
   # WRONG: for msg in agent.query(prompt)
   # RIGHT: async for msg in agent.query(prompt)
   ```

6. **Session resume** - Pass `resume=session_id` to continue, get new session_id from `ResultMessage`

7. **Text interleaving** - Text and tool calls arrive interleaved in time order
   - Don't assume all tools come first then text
   - Track events in arrival order for accurate display

8. **Interrupt handling** - Call `agent.interrupt()` to stop, check `ResultMessage.status == "interrupted"`
   - After `client.interrupt()`, let query task drain remaining messages (don't cancel immediately)
   - Set `self.interrupted = True` flag before interrupt, check in query to return correct status
   - Cancel pending permission futures on interrupt (deny them)
   - Timeout after 5s if drain takes too long, then force cancel

### Output View
- Read-only scratch view controlled by plugin
- Region-based rendering allows in-place updates
- Custom syntax highlighting (ClaudeOutput.sublime-syntax)
- Ayu Mirage theme (ClaudeOutput.hidden-tmTheme)
- Syntax-specific settings (ClaudeOutput.sublime-settings) for font size
- Prompt delimiters: `◎ prompt text ▶` (supports multiline with indented continuation)
- Working indicator: `⋯` shown at bottom while processing
- `@done(Xs)` pops conversation context in syntax

### Permission UI
Inline permission prompts in the output view:
- `[Y] Allow` - one-time allow
- `[N] Deny` - deny (marks tool as ✘)
- `[S] Allow 30s` - auto-allow same tool for 30 seconds
- `[A] Always` - auto-allow this tool for entire session

Clickable buttons + keyboard shortcuts (Y/N/S/A keys).

Multiple permission requests are queued - only one shown at a time.

### Tool Tracking
- Tools stored as ordered list (not dict by name) to support multiple calls to same tool
- `tool_done` finds last pending tool with matching name

### Session Management
- Sessions keyed by output view id (not window id) - allows multiple sessions per window
- Sessions saved to `.sessions.json` with name, project, cost, query count
- Resume via session_id passed to SDK
- `plugin_loaded` hook reconnects orphaned output views after Sublime restart
- Closing output view stops its session

**View title status indicators:**
- `◉` Active + working
- `◇` Active + idle
- `•` Inactive + working
- (no prefix) Inactive + idle

### UX Details
- New Session command creates fresh view + immediately opens input prompt
- Enter key in output view opens input prompt
- Cmd+K clears output, Cmd+Z undoes clear (custom undo via `_cleared_content`)
- Cmd+Escape interrupts current query
- Context commands require active session

### MCP Integration
- `mcp_server.py` - Unix socket server in Sublime, handles eval requests
- `mcp/server.py` - MCP stdio server, connects to Sublime socket
- Bridge loads MCP config from `.claude/settings.json` or `.mcp.json`
- Status bar shows loaded MCP servers on init

**MCP Tools:**
- Editor: `get_window_summary`, `find_file`, `get_symbols`, `goto_symbol`, `read_view`
- Terminal: `terminal_list`, `terminal_run`, `terminal_read`, `terminal_close`
- Blackboard: `bb_write`, `bb_read`, `bb_list`, `bb_delete`
- Sessions: `spawn_session`, `send_to_session`, `list_sessions`
- User: `ask_user` - ask questions via quick panel
- Custom: `sublime_eval`, `sublime_tool`, `list_tools`

**Editor tools:**
- `get_window_summary()` - Open files, active file with selection, project folders, layout
- `find_file(query, pattern?, limit?)` - Fuzzy find by partial name, optional glob filter
- `get_symbols(query, file_path?, limit?)` - Batch symbol lookup (comma-separated or JSON array)
- `read_view(file_path?, view_name?, head?, tail?, grep?, grep_i?)` - Read content from any view with head/tail/grep filtering

**Terminal tools (uses Terminus plugin):**
- `terminal_run(command, tag?)` - Run command in terminal. PREFER over Bash for long-running/interactive commands
- `terminal_read(tag?, lines?)` - Read terminal output (default 100 lines from end)
- `terminal_list()` - List open terminals
- `terminal_close(tag?)` - Close a terminal

**Blackboard patterns:**
- `plan` - implementation steps, architecture decisions
- `walkthrough` - progress report for user (markdown)
- `decisions` - key choices and rationale
- `commands` - project-specific commands that work
- Data persists across sessions, survives context loss

**Terminal usage (Terminus plugin required):**
- Agent should use `terminal_run` instead of `Bash` for long-running commands
- Pattern: `terminal_run("make build", wait=2)` → returns output after wait
- Opens new terminal automatically if none exists (tag: `claude-agent`)
- Output stays in Terminus view (avoids buffer explosion in Claude output)

**Terminus API notes:**
- `terminus_open` args: `cmd`, `shell_cmd`, `cwd`, `title`, `tag`, `auto_close`, `focus`, `post_window_hooks`
- `terminus_send_string` args: `string`, `tag`, `visible_only` - window command, uses tag to target
- `post_window_hooks`: list of `[command, args]` to run after terminal ready
- Threading: MCP socket runs in background thread; `sublime.set_timeout` callbacks don't run while sleeping
- Solution: use `post_window_hooks` to queue command on terminal open

**Terminal wait implementation:**
- `terminal_run(cmd, wait=N)` - uses `sublime.set_timeout(do_read, delay_ms)` for delay
- This lets main thread process `terminus_open` + `post_window_hooks` before reading
- New terminal gets 1s extra startup delay (vs 0.2s for existing)
- Background thread waits on `Event` for the delayed read to complete
- `terminus_open` must be scheduled via `set_timeout(do_open, 10)` to actually execute

### Subagents
- Loaded from `.claude/settings.json` `agents` key
- Built-in agents merged with project-defined (project overrides)

**Built-in agents:**
- `planner` - creates implementation plan, saves to blackboard (haiku)
- `reporter` - updates walkthrough/progress report (haiku)

**Agent definition:**
```json
{
  "description": "When to use this agent",
  "prompt": "System prompt for the agent",
  "tools": ["Read", "Grep"],  // restrict available tools
  "model": "haiku"  // haiku/sonnet/opus
}
```

### Quick Prompts
Single-key shortcuts in output view (when idle):
- `F` - "Fuck, read the damn docs" - re-read docs, continue
- `R` - Retry: read error, try different approach
- `C` - Continue

### Todo Display
- TodoWrite tool input captured and displayed at end of response
- Icons: `○` pending, `▸` in_progress, `✓` completed
- Incomplete todos carry forward to next conversation
- When all done: shown once, then cleared for next conversation

### Diff Display
- Edit tool shows inline diff in output view
- Uses ```diff fenced block with syntax highlighting
- `-` lines (old) and `+` lines (new)

### Sublime Text Commands
To modify a read-only view, need custom TextCommands:
- `claude_insert` - insert at position
- `claude_replace` - replace region

## AI Agent Guidelines

### Critical Invariants

**Session Resume - MUST pass `resume_id` when reconnecting sessions:**
```python
# CORRECT - preserves conversation context
session = Session(window, resume_id=saved_session_id)

# WRONG - loses ALL conversation history (DO NOT do this for reconnects)
session = Session(window)
```

**Permission Block Tracking:**
Use Sublime's tracked regions (`add_regions`/`get_regions`) for UI elements. Stored coordinates become stale when text shifts.

### Red Flags - STOP and Verify

1. **Removing function parameters** - likely breaks callers
2. **Changing default values** - silent behavior change
3. **"Simplifying" by removing steps** - those steps existed for a reason
4. **"Avoiding duplicates" by skipping operations** - probably load-bearing
5. **Any change justified by "cleaner" or "simpler"** - clean != correct

### Rules for AI Agents

1. **No Silent Behavior Changes** - If changing HOW something works, explicitly state:
   - What the old behavior was
   - What the new behavior is
   - Why the change is acceptable

2. **Distrust Your Own Simplifications** - When you want to remove code that seems unnecessary, STOP. Check git history. Ask the user.

3. **Context Loss is the Enemy** - Write critical decisions to blackboard/comments IMMEDIATELY. Don't trust that you'll remember.

4. **Preserve Load-Bearing Code** - Some code looks unnecessary but is critical. "To avoid duplicates" was used to justify breaking session resume - that was WRONG.

5. **Name Things by Purpose** - `resume_id` is better than `session_id` because it implies "this is FOR resuming" - harder to accidentally drop.

## TODO
- [ ] Streaming text (currently waits for full response?)
- [ ] Image/file drag-drop to context
- [ ] Cost tracking dashboard
- [ ] Session search/filter
- [ ] Click to expand/collapse tool sections
- [ ] MCP tool parameters (pass args to saved tools)

## Recent Changes (2025-12-11)

### New Features
- **`read_view` MCP tool**: Read content from any view (file buffer or scratch) in Sublime Text with filtering.
  - Accepts `file_path` for file buffers (absolute or relative to project)
  - Accepts `view_name` for scratch buffers (e.g., output panels, unnamed buffers)
  - Filtering options (applied in order: grep → head/tail):
    - `head=N` - Read first N lines
    - `tail=N` - Read last N lines
    - `grep="pattern"` - Filter lines matching regex (case-sensitive)
    - `grep_i="pattern"` - Filter lines matching regex (case-insensitive)
  - Returns: `content`, `size`, `line_count`, `original_line_count`, and filter info
  ```python
  read_view(file_path="src/main.py", head=50)
  read_view(view_name="Output", grep="ERROR")
  read_view(file_path="log.txt", grep_i="warning", tail=100)
  ```

## Recent Changes (2025-12-10)

### Bug Fixes
- **Concurrent permission requests**: Fixed bug where multiple tool permissions arriving simultaneously would clear earlier ones as "stale". Now properly queued and processed in order.
- **Permission timeout reduced**: 5min → 30s. Prevents long hangs when permission UI gets stuck.
- **Session rename persistence**: `session_id` now set immediately on resume, so renames save before first query completes.

### Improvements
- **Tool status colors**: Distinct muted colors for tool done (`#5a9484` teal) and error (`#a06a74` mauve). No longer conflicts with diff highlighting.

### New Features
- **Queued prompt**: Queue a prompt while Claude is working. Auto-sends when current query finishes.
  - Type in input mode + Enter to queue (when working)
  - Or use `Claude: Queue Prompt` command
  - Shows `⏳ <preview>...` indicator in output view
  - Shows `[queued]` in status bar spinner
- **View session history**: `Claude: View Session History...` command to browse saved sessions and view user prompts from Claude's stored `.jsonl` files.

## Recent Changes (2025-12-09)

### Bug Fixes
- **Garbled output fix** (`output.py:_do_render`): Extended replacement region to `view_size` when orphaned content exists after the conversation region. Prevents fragmented text appearing after `⋯` indicator.

### Improvements
- **Edit diff format**: Now uses `difflib.unified_diff` for readable diffs with context lines and `@@` hunks, instead of listing all `-` then all `+` lines.
- **Bash output**: Shows 3 head + 5 tail lines (was 5 head only). Better visibility of command results.

### New Features
- **`ask_user` MCP tool**: Ask user questions via quick panel. Workaround for missing `AskUserQuestion` support in Agent SDK.
  ```
  ask_user("Which auth method?", ["OAuth", "JWT", "Session"])
  ```
  Returns `{"answer": "OAuth", "question": "..."}` or `{"cancelled": true}`

## Done
- [x] Multi-session per window
- [x] Session resume/fork
- [x] Permission prompts (Y/N/S/A)
- [x] Blackboard (cross-session state)
- [x] Built-in subagents (planner, reporter)
- [x] Quick prompts (F/R/C)
- [x] Todo display from TodoWrite
- [x] Diff display for Edit tool
- [x] MCP integration (editor, blackboard, sessions)
- [x] Time-ordered events (text + tools interleaved as they arrive)
- [x] View title status indicators (◉/◇/•)
- [x] Smart auto-scroll (only when cursor near end)
- [x] Session reconnect resets stale states
