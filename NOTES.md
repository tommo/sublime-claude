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

**Message Flow:**
- `SystemMessage` - initialization
- `AssistantMessage` with `ToolUseBlock` - tool request
- `can_use_tool` callback - permission check
- `UserMessage` with `ToolResultBlock` - tool result (not in AssistantMessage!)
- `AssistantMessage` with `TextBlock` - response text
- `ResultMessage` - completion (duration_ms, total_cost_usd, etc.)

**Permission Callback:**
```python
async def can_use_tool(tool_name: str, tool_input: dict, context) -> PermissionResult:
    # Must return PermissionResultAllow or PermissionResultDeny dataclasses
    # NOT plain dicts (SDK docs are outdated)
    return PermissionResultAllow(updated_input=tool_input)
```

**Key Gotchas:**
- `ToolResultBlock` is in `UserMessage.content`, not `AssistantMessage`
- When permission denied, SDK sends `TextBlock` directly (no `ToolResultBlock`)
- Must mark denied tools as error manually in the permission callback

### Output View
- Read-only scratch view controlled by plugin
- Region-based rendering allows in-place updates
- Custom syntax highlighting (ClaudeOutput.sublime-syntax)
- Ayu Mirage theme (ClaudeOutput.hidden-tmTheme)
- Syntax-specific settings (ClaudeOutput.sublime-settings) for font size
- Prompt delimiters: `◎ prompt text ▶` (supports multiline with indented continuation)
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
- View title shows spinner + current tool when working
- Closing output view stops its session

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
- Editor: `get_open_files`, `get_symbols`, `goto_symbol`
- Blackboard: `bb_write`, `bb_read`, `bb_list`, `bb_delete`
- Sessions: `spawn_session`, `list_sessions`
- Custom: `sublime_eval`, `sublime_tool`, `list_tools`

**Blackboard patterns:**
- `plan` - implementation steps, architecture decisions
- `walkthrough` - progress report for user (markdown)
- `decisions` - key choices and rationale
- `commands` - project-specific commands that work
- Data persists across sessions, survives context loss

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

## TODO
- [ ] Streaming text (currently waits for full response?)
- [ ] Image/file drag-drop to context
- [ ] Cost tracking dashboard
- [ ] Session search/filter
- [ ] Click to expand/collapse tool sections
- [ ] MCP tool parameters (pass args to saved tools)

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
