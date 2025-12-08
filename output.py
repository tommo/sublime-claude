"""Structured output view with region tracking."""
import sublime
import sublime_plugin
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Callable, Any


# Status constants
PENDING = "pending"
DONE = "done"
ERROR = "error"

# Permission button constants
PERM_ALLOW = "allow"
PERM_DENY = "deny"
PERM_ALLOW_ALL = "allow_all"
PERM_ALLOW_SESSION = "allow_session"  # Allow same tool for 30s


@dataclass
class PermissionRequest:
    """A pending permission request."""
    id: int
    tool: str
    tool_input: dict
    callback: Callable[[str], None]  # Called with PERM_ALLOW, PERM_DENY, or PERM_ALLOW_ALL
    region: tuple = (0, 0)  # Region in view
    button_regions: Dict[str, tuple] = field(default_factory=dict)  # button_type -> (start, end)


@dataclass
class ToolCall:
    """A single tool call."""
    name: str
    tool_input: dict
    status: str = PENDING  # pending, done, error


@dataclass
class TodoItem:
    """A todo item from TodoWrite."""
    content: str
    status: str  # pending, in_progress, completed


@dataclass
class Conversation:
    """A single prompt + tools + response + meta."""
    prompt: str = ""
    tools: List[ToolCall] = field(default_factory=list)  # ordered list of tool calls
    text_chunks: List[str] = field(default_factory=list)
    todos: List[TodoItem] = field(default_factory=list)  # current todo state
    todos_all_done: bool = False  # True when all todos completed (don't carry to next)
    duration: float = 0.0
    region: tuple = (0, 0)


class OutputView:
    """Structured output view - readonly, plugin-controlled."""

    SYMBOLS = {
        "pending": "☐",
        "done": "✔",
        "error": "✘",
    }

    def __init__(self, window: sublime.Window):
        self.window = window
        self.view: Optional[sublime.View] = None
        self.conversations: List[Conversation] = []
        self.current: Optional[Conversation] = None
        self.pending_permission: Optional[PermissionRequest] = None
        self._permission_queue: List[PermissionRequest] = []  # Queue for multiple requests
        self.auto_allow_tools: set = set()  # Tools auto-allowed for this session
        self._last_allowed_tool: Optional[str] = None  # Track last tool we allowed
        self._last_allowed_time: float = 0  # Timestamp of last allow
        self._pending_context_region: tuple = (0, 0)  # Region for context display
        self._cleared_content: Optional[str] = None  # For undo clear

    def show(self) -> None:
        # If we already have a view, just focus it
        if self.view and self.view.is_valid():
            self.window.focus_view(self.view)
            return

        # Create new view
        self.view = self.window.new_file()
        self.view.set_name("Claude")
        self.view.set_scratch(True)
        self.view.set_read_only(True)
        self.view.settings().set("claude_output", True)
        self.view.settings().set("word_wrap", True)
        self.view.settings().set("gutter", True)
        self.view.settings().set("line_numbers", False)
        self.view.settings().set("fold_buttons", True)
        self.view.settings().set("font_size", 12)
        try:
            self.view.assign_syntax("Packages/ClaudeCode/ClaudeOutput.sublime-syntax")
            self.view.settings().set("color_scheme", "Packages/ClaudeCode/ClaudeOutput.hidden-tmTheme")
        except Exception as e:
            print(f"[Claude] Error setting syntax/theme: {e}")
        self.window.focus_view(self.view)

    def set_name(self, name: str) -> None:
        """Update the output view title."""
        if self.view and self.view.is_valid():
            # Check if this is the active Claude view
            window = self.view.window()
            is_active = window and window.settings().get("claude_active_view") == self.view.id()
            prefix = "◉ " if is_active else ""
            self.view.set_name(f"{prefix}Claude: {name}")

    def _write(self, text: str, pos: Optional[int] = None) -> int:
        """Write text at position (or end). Returns end position."""
        if not self.view or not self.view.is_valid():
            return 0

        self.view.set_read_only(False)
        if pos is None:
            pos = self.view.size()
        self.view.run_command("claude_insert", {"pos": pos, "text": text})
        self.view.set_read_only(True)
        return pos + len(text)

    def _replace(self, start: int, end: int, text: str) -> int:
        """Replace region with text. Returns new end position."""
        if not self.view or not self.view.is_valid():
            return end

        self.view.set_read_only(False)
        self.view.run_command("claude_replace", {"start": start, "end": end, "text": text})
        self.view.set_read_only(True)
        return start + len(text)

    def _scroll_to_end(self) -> None:
        if self.view and self.view.is_valid():
            self.view.run_command("move_to", {"to": "eof"})

    # --- Public API ---

    def set_pending_context(self, context_items: list) -> None:
        """Show pending context at end of view."""
        if not self.view or not self.view.is_valid():
            return

        # Remove old context display
        start, end = self._pending_context_region
        if end > start:
            self._replace(start, end, "")

        if not context_items:
            self._pending_context_region = (0, 0)
            return

        # Build context display
        names = [item.name for item in context_items]
        text = f"\n📎 {', '.join(names)} ({len(names)} file{'s' if len(names) > 1 else ''})\n"

        # Write at end
        start = self.view.size()
        end = self._write(text)
        self._pending_context_region = (start, end)
        self._scroll_to_end()

    def prompt(self, text: str, context_names: List[str] = None) -> None:
        """Start a new conversation with a prompt."""
        self.show()

        # Clear pending context display
        start, end = self._pending_context_region
        if end > start:
            self._replace(start, end, "")
            self._pending_context_region = (0, 0)

        # Save previous conversation and carry over incomplete todos
        prev_todos = []
        if self.current:
            self.conversations.append(self.current)
            # Carry todos forward only if not all completed
            if not self.current.todos_all_done:
                prev_todos = self.current.todos

        # Start new
        self.current = Conversation(prompt=text, todos=prev_todos)

        # Render prompt with optional context indicator
        start = self.view.size()
        prefix = "\n" if start > 0 else ""
        # Indent continuation lines to align with first line after ◎
        lines = text.split("\n")
        if len(lines) > 1:
            indented = lines[0] + "\n" + "\n".join("  " + l for l in lines[1:])
        else:
            indented = text
        if context_names:
            context_str = ", ".join(context_names)
            line = f"{prefix}◎ {indented}  📎 {context_str} ▶\n"
        else:
            line = f"{prefix}◎ {indented} ▶\n"
        end = self._write(line)
        self.current.region = (start, end)
        self._scroll_to_end()

    def tool(self, name: str, tool_input: dict = None) -> None:
        """Add a pending tool."""
        if not self.current:
            print(f"[Claude] tool({name}) - no current conversation")
            return

        print(f"[Claude] tool({name}) - adding pending")
        tool_input = tool_input or {}
        self.current.tools.append(ToolCall(name=name, tool_input=tool_input))

        # Capture TodoWrite state
        if name == "TodoWrite" and "todos" in tool_input:
            self.current.todos = [
                TodoItem(content=t.get("content", ""), status=t.get("status", "pending"))
                for t in tool_input["todos"]
            ]

        self._render_current()
        self._scroll_to_end()

    def tool_done(self, name: str) -> None:
        """Mark most recent pending tool with this name as done."""
        if not self.current:
            print(f"[Claude] tool_done({name}) - no current conversation")
            return
        # Find the last pending tool with this name
        for tool in reversed(self.current.tools):
            if tool.name == name and tool.status == PENDING:
                tool.status = DONE
                print(f"[Claude] tool_done({name}) - marked done")
                self._render_current()
                return
        # No pending tool found, add as done
        print(f"[Claude] tool_done({name}) - not found pending, adding as done")
        self.current.tools.append(ToolCall(name=name, tool_input={}, status=DONE))
        self._render_current()

    def tool_error(self, name: str) -> None:
        """Mark most recent pending tool with this name as error."""
        if not self.current:
            return
        # Find the last pending tool with this name
        for tool in reversed(self.current.tools):
            if tool.name == name and tool.status == PENDING:
                tool.status = ERROR
                self._render_current()
                return
        # No pending tool found, add as error
        self.current.tools.append(ToolCall(name=name, tool_input={}, status=ERROR))
        self._render_current()

    def text(self, content: str) -> None:
        """Add response text."""
        if not self.current:
            return

        self.current.text_chunks.append(content)
        self._render_current()
        self._scroll_to_end()

    def meta(self, duration: float, cost: float = None) -> None:
        """Set completion meta."""
        if not self.current:
            return

        self.current.duration = duration
        self._render_current()
        self._scroll_to_end()

    def clear(self) -> None:
        """Clear all output (can undo with Cmd+Z)."""
        if self.view and self.view.is_valid():
            # Save content for undo
            self._cleared_content = self.view.substr(sublime.Region(0, self.view.size()))
            self.view.set_read_only(False)
            self.view.run_command("claude_clear_all")
            self.view.set_read_only(True)
        self.conversations = []
        self.current = None
        self.pending_permission = None
        self._permission_queue.clear()
        self.auto_allow_tools.clear()

    def undo_clear(self) -> None:
        """Restore content from last clear."""
        if self._cleared_content and self.view and self.view.is_valid():
            self._write(self._cleared_content)
            self._cleared_content = None
            self._scroll_to_end()

    # --- Permission UI ---

    def permission_request(self, pid: int, tool: str, tool_input: dict, callback: Callable[[str], None]) -> None:
        """Show a permission request in the view."""
        import time
        self.show()

        # Check if tool is auto-allowed for session
        if tool in self.auto_allow_tools:
            print(f"[Claude] permission_request pid={pid}: auto-allowed (in auto_allow_tools)")
            callback(PERM_ALLOW)
            return

        # Check if user chose "allow for 30s" recently
        now = time.time()
        if self._last_allowed_tool == tool and (now - self._last_allowed_time) < 30:
            print(f"[Claude] permission_request pid={pid}: auto-allowing (30s window)")
            callback(PERM_ALLOW)
            return

        print(f"[Claude] permission_request pid={pid}: showing prompt")
        # Create the request
        perm = PermissionRequest(
            id=pid,
            tool=tool,
            tool_input=tool_input,
            callback=callback,
        )

        # If there's already a pending permission, queue this one
        if self.pending_permission and self.pending_permission.callback:
            print(f"[Claude] permission_request pid={pid}: queued (existing pending)")
            self._permission_queue.append(perm)
            return

        # Show this one
        self.pending_permission = perm
        self._render_permission()
        self._scroll_to_end()

    def _render_permission(self) -> None:
        """Render the permission request block."""
        if not self.pending_permission or not self.view:
            return

        perm = self.pending_permission
        tool = perm.tool
        tool_input = perm.tool_input

        # Format tool details
        detail = ""
        if tool == "Bash" and "command" in tool_input:
            cmd = tool_input["command"]
            if len(cmd) > 80:
                cmd = cmd[:80] + "..."
            detail = cmd
        elif tool in ("Read", "Edit", "Write") and "file_path" in tool_input:
            detail = tool_input["file_path"]
        elif tool == "Glob" and "pattern" in tool_input:
            detail = tool_input["pattern"]
        elif tool == "Grep" and "pattern" in tool_input:
            detail = tool_input["pattern"]
        else:
            # Generic: show first param
            for k, v in list(tool_input.items())[:1]:
                detail = f"{k}: {str(v)[:60]}"

        # Build permission block
        lines = [
            "\n",
            f"  ⚠ Allow {tool}",
        ]
        if detail:
            lines.append(f": {detail}")
        lines.append("?\n")
        lines.append("    ")

        # Track button positions relative to block start
        text_before_buttons = "".join(lines)

        # Buttons
        btn_y = "[Y] Allow"
        btn_n = "[N] Deny"
        btn_s = "[S] Allow 30s"
        btn_a = f"[A] Always"

        lines.append(btn_y)
        lines.append("  ")
        lines.append(btn_n)
        lines.append("  ")
        lines.append(btn_s)
        lines.append("  ")
        lines.append(btn_a)
        lines.append("\n")

        text = "".join(lines)

        # Write to view
        start = self.view.size()
        end = self._write(text)
        perm.region = (start, end)

        # Calculate button regions (absolute positions)
        btn_start = start + len(text_before_buttons)
        perm.button_regions[PERM_ALLOW] = (btn_start, btn_start + len(btn_y))
        btn_start += len(btn_y) + 2  # +2 for "  "
        perm.button_regions[PERM_DENY] = (btn_start, btn_start + len(btn_n))
        btn_start += len(btn_n) + 2
        perm.button_regions[PERM_ALLOW_SESSION] = (btn_start, btn_start + len(btn_s))
        btn_start += len(btn_s) + 2
        perm.button_regions[PERM_ALLOW_ALL] = (btn_start, btn_start + len(btn_a))

        # Add regions for highlighting
        self._add_button_regions()

    def _add_button_regions(self) -> None:
        """Add sublime regions for button highlighting."""
        if not self.pending_permission or not self.view:
            return

        perm = self.pending_permission
        for btn_type, (start, end) in perm.button_regions.items():
            region_key = f"claude_btn_{btn_type}"
            self.view.add_regions(
                region_key,
                [sublime.Region(start, end)],
                f"claude.permission.button.{btn_type}",
                "",
                sublime.DRAW_NO_OUTLINE,
            )

    def _clear_permission(self) -> None:
        """Remove permission block from view (but keep pending_permission for same-tool detection)."""
        if not self.pending_permission or not self.view:
            return

        perm = self.pending_permission
        start, end = perm.region

        # Remove button regions
        for btn_type in perm.button_regions:
            self.view.erase_regions(f"claude_btn_{btn_type}")

        # Remove text
        self._replace(start, end, "")
        # Don't clear pending_permission - keep it to detect rapid same-tool requests
        # It will be overwritten when a different tool request comes in

    def handle_permission_click(self, point: int) -> bool:
        """Check if point is on a permission button. Returns True if handled."""
        if not self.pending_permission:
            return False

        # Check if already responded (callback cleared)
        if self.pending_permission.callback is None:
            return False

        perm = self.pending_permission
        for btn_type, (start, end) in perm.button_regions.items():
            if start <= point <= end:
                # Mark as handled immediately to prevent double-processing
                callback = perm.callback
                perm.callback = None
                self._respond_permission_with_callback(btn_type, callback, perm.tool)
                return True
        return False

    def _respond_permission_with_callback(self, response: str, callback, tool: str) -> None:
        """Respond to a permission request with given callback."""
        import time

        # Handle "allow all" - remember for this session
        if response == PERM_ALLOW_ALL:
            self.auto_allow_tools.add(tool)
            response = PERM_ALLOW

        # Handle "allow 30s" - set timed auto-allow
        if response == PERM_ALLOW_SESSION:
            self._last_allowed_tool = tool
            self._last_allowed_time = time.time()
            response = PERM_ALLOW

        # Clear the UI
        self._clear_permission()
        self.pending_permission = None

        # Call the callback
        callback(response)

        # Process next queued permission if any
        self._process_permission_queue()

    def _process_permission_queue(self) -> None:
        """Process the next permission request in queue."""
        import time

        while self._permission_queue:
            perm = self._permission_queue.pop(0)

            # Check if auto-allowed now (user may have clicked "Always" or "30s")
            if perm.tool in self.auto_allow_tools:
                print(f"[Claude] _process_queue pid={perm.id}: auto-allowed")
                perm.callback(PERM_ALLOW)
                continue

            now = time.time()
            if self._last_allowed_tool == perm.tool and (now - self._last_allowed_time) < 30:
                print(f"[Claude] _process_queue pid={perm.id}: auto-allowing (30s window)")
                perm.callback(PERM_ALLOW)
                continue

            # Show this one
            print(f"[Claude] _process_queue pid={perm.id}: showing prompt")
            self.pending_permission = perm
            self._render_permission()
            self._scroll_to_end()
            break

    def handle_permission_key(self, key: str) -> bool:
        """Handle Y/N/A key press. Returns True if handled."""
        if not self.pending_permission:
            return False

        # Check if already responded
        if self.pending_permission.callback is None:
            return False

        perm = self.pending_permission
        key = key.lower()
        response = None
        if key == "y":
            response = PERM_ALLOW
        elif key == "n":
            response = PERM_DENY
        elif key == "s":
            response = PERM_ALLOW_SESSION
        elif key == "a":
            response = PERM_ALLOW_ALL

        if response:
            # Mark as handled immediately
            callback = perm.callback
            perm.callback = None
            self._respond_permission_with_callback(response, callback, perm.tool)
            return True
        return False

    def _render_current(self) -> None:
        """Re-render current conversation in place."""
        if not self.current or not self.view:
            return

        # Build the full text for this conversation
        lines = []

        # Prompt (newline before only if not at start)
        prefix = "\n" if self.current.region[0] > 0 else ""
        # Indent continuation lines
        prompt_lines = self.current.prompt.split("\n")
        if len(prompt_lines) > 1:
            indented_prompt = prompt_lines[0] + "\n" + "\n".join("  " + l for l in prompt_lines[1:])
        else:
            indented_prompt = self.current.prompt
        lines.append(f"{prefix}◎ {indented_prompt} ▶\n")

        # Tools
        for tool in self.current.tools:
            symbol = self.SYMBOLS[tool.status]
            # Show tool with optional detail
            detail = ""
            if tool.tool_input:
                if tool.name == "Bash" and "command" in tool.tool_input:
                    cmd = tool.tool_input["command"]
                    # Truncate long commands
                    if len(cmd) > 60:
                        cmd = cmd[:60] + "..."
                    detail = f": {cmd}"
                elif tool.name == "Read" and "file_path" in tool.tool_input:
                    detail = f": {tool.tool_input['file_path']}"
                elif tool.name == "Edit" and "file_path" in tool.tool_input:
                    detail = f": {tool.tool_input['file_path']}"
                    # Show diff for Edit tool
                    old = tool.tool_input.get("old_string", "")
                    new = tool.tool_input.get("new_string", "")
                    if old or new:
                        diff_lines = []
                        for line in old.splitlines():
                            diff_lines.append(f"- {line}")
                        for line in new.splitlines():
                            diff_lines.append(f"+ {line}")
                        if diff_lines:
                            detail += "\n```diff\n" + "\n".join(diff_lines) + "\n```"
                elif tool.name == "Write" and "file_path" in tool.tool_input:
                    detail = f": {tool.tool_input['file_path']}"
                elif tool.name == "Glob" and "pattern" in tool.tool_input:
                    detail = f": {tool.tool_input['pattern']}"
                elif tool.name == "Grep" and "pattern" in tool.tool_input:
                    detail = f": {tool.tool_input['pattern']}"
            lines.append(f"  {symbol} {tool.name}{detail}\n")

        # Text response
        if self.current.text_chunks:
            lines.append("\n")
            lines.append("".join(self.current.text_chunks))
            if not self.current.text_chunks[-1].endswith("\n"):
                lines.append("\n")
        elif self.current.tools:
            # Add blank line after tools if no text yet
            lines.append("\n")

        # Todo list (if any)
        if self.current.todos:
            lines.append("\n  ───── Tasks ─────\n")
            for todo in self.current.todos:
                if todo.status == "completed":
                    icon = "✓"
                elif todo.status == "in_progress":
                    icon = "▸"
                else:
                    icon = "○"
                lines.append(f"  {icon} {todo.content}\n")
            # Mark as done so next conversation starts fresh
            if all(t.status == "completed" for t in self.current.todos):
                self.current.todos_all_done = True

        # Meta
        if self.current.duration > 0:
            lines.append(f"\n  @done({self.current.duration:.1f}s)\n")

        text = "".join(lines)

        # Replace the region
        start, end = self.current.region
        new_end = self._replace(start, end, text)
        self.current.region = (start, new_end)


# --- Helper commands for text manipulation ---

class ClaudeInsertCommand(sublime_plugin.TextCommand):
    """Insert text at position."""
    def run(self, edit, pos: int, text: str):
        self.view.insert(edit, pos, text)


class ClaudeReplaceCommand(sublime_plugin.TextCommand):
    """Replace region with text."""
    def run(self, edit, start: int, end: int, text: str):
        region = sublime.Region(start, end)
        self.view.replace(edit, region, text)


class ClaudeClearAllCommand(sublime_plugin.TextCommand):
    """Clear all text (undoable)."""
    def run(self, edit):
        region = sublime.Region(0, self.view.size())
        self.view.erase(edit, region)


class ClaudeUndoClearCommand(sublime_plugin.TextCommand):
    """Undo clear - restore saved content."""
    def run(self, edit):
        # Get the OutputView instance from claude_code module
        from . import claude_code
        window = self.view.window()
        if window:
            session = claude_code.get_session(window)
            if session:
                session.output.undo_clear()
