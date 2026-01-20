"""Structured output view with region tracking."""
import sublime
import sublime_plugin
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Callable, Any

from .constants import SPINNER_FRAMES


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
    result: Optional[str] = None  # tool result content


@dataclass
class TodoItem:
    """A todo item from TodoWrite."""
    content: str
    status: str  # pending, in_progress, completed


@dataclass
class Conversation:
    """A single prompt + tools + response + meta."""
    prompt: str = ""
    # Events in time order - either ToolCall or str (text chunk)
    events: List = field(default_factory=list)
    todos: List[TodoItem] = field(default_factory=list)  # current todo state
    todos_all_done: bool = False  # True when all todos completed (don't carry to next)
    working: bool = True  # True while processing, False when done
    duration: float = 0.0
    region: tuple = (0, 0)
    context_names: List[str] = field(default_factory=list)  # Context files used

    @property
    def tools(self) -> List[ToolCall]:
        """Get all tool calls (for compatibility)."""
        return [e for e in self.events if isinstance(e, ToolCall)]

    @property
    def text_chunks(self) -> List[str]:
        """Get all text chunks (for compatibility)."""
        return [e for e in self.events if isinstance(e, str)]


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
        self._render_pending: bool = False  # Debounce flag for rendering
        # Inline input state
        self._input_mode: bool = False  # True when user can type in input region
        self._input_start: int = 0  # Start position of editable input region
        self._input_area_start: int = 0  # Start of entire input area (context + marker)
        self._input_marker: str = "◎ "  # Marker for input line
        self._spinner_frame: int = 0  # Current spinner animation frame

    def show(self, focus: bool = True) -> None:
        # If we already have a view, optionally focus it
        if self.view and self.view.is_valid():
            if focus:
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

        # Initialize cursor position to enable mouse selection
        # CRITICAL: Sublime requires a valid cursor for mouse interaction
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(0, 0))

        # Ensure view can receive mouse events
        self.view.settings().set("is_widget", False)

        if focus:
            self.window.focus_view(self.view)

    def set_name(self, name: str) -> None:
        """Update the output view title."""
        self._name = name  # Store for refresh_title
        self._update_title()

    def _update_title(self) -> None:
        """Refresh the view title based on current state."""
        if not self.view or not self.view.is_valid():
            return
        name = getattr(self, '_name', 'Claude')
        # Check if this is the active Claude view
        window = self.view.window()
        is_active = window and window.settings().get("claude_active_view") == self.view.id()
        # ◉ = active+working, ◇ = active+idle, • = inactive+working, (none) = inactive+idle
        is_working = self.current and self.current.working
        if is_active:
            prefix = "◉ " if is_working else "◇ "
        else:
            prefix = "• " if is_working else ""
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

    def _scroll_to_end(self, force: bool = False) -> None:
        """Scroll to end, respecting user scroll position.

        Args:
            force: If True, always scroll. If False, only scroll if cursor is near end.
        """
        if not self.view or not self.view.is_valid():
            return

        # In input mode, always keep input visible
        if self._input_mode:
            self.view.show(self._input_start, keep_to_left=False, animate=False)
            return

        # Always scroll during active streaming
        if self.current and self.current.working:
            force = True

        # Check if we should auto-scroll
        if not force:
            sel = self.view.sel()
            if sel:
                cursor = sel[0].end()
                size = self.view.size()
                # Only auto-scroll if cursor is near end (within 200 chars)
                if size > 200 and cursor < size - 200:
                    return  # User scrolled up, don't auto-scroll

        # Scroll to end without moving cursor
        end = self.view.size()
        self.view.show(end, keep_to_left=False, animate=False)

    # --- Inline Input ---

    def enter_input_mode(self) -> None:
        """Enter input mode - show prompt marker and allow typing."""
        # print(f"[Claude] enter_input_mode: called, view_valid={self.view and self.view.is_valid()}, already_in_input={self._input_mode}")
        if not self.view or not self.view.is_valid():
            return
        if self._input_mode:
            # print(f"[Claude] enter_input_mode: already in input mode, returning")
            return  # Already in input mode

        # Exit any current conversation's working state
        if self.current and self.current.working:
            # print(f"[Claude] enter_input_mode: current conversation is working, can't enter input mode")
            return  # Can't input while working

        # Additional safety: check if there's a pending render that should complete first
        if self._render_pending:
            # print(f"[Claude] enter_input_mode: pending render, deferring...")
            # Schedule input mode entry after pending render completes
            sublime.set_timeout(self.enter_input_mode, 20)
            return

        # Safety: check for and clean up any stale input markers from previous sessions
        # This can happen after Sublime restart when OutputView state is lost but view content remains
        # BUT: Don't clean up fresh context that was just added
        from . import claude_code
        session = claude_code.get_session_for_view(self.view)
        has_pending_context = session and session.pending_context
        # print(f"[Claude] enter_input_mode: has_pending_context={has_pending_context}, pending_context={session.pending_context if session else None}")

        content = self.view.substr(sublime.Region(0, self.view.size()))
        # print(f"[Claude] enter_input_mode: view_size={self.view.size()}, last_50_chars={repr(content[-50:])}")
        if content:
            lines = content.split('\n')
            # Check last few lines for stale input markers
            cleanup_start = -1
            # print(f"[Claude] enter_input_mode: checking last 5 lines for cleanup: {lines[-5:]}")
            for i in range(len(lines) - 1, max(-1, len(lines) - 5), -1):
                line = lines[i]
                # Input marker: starts with "◎ " but no " ▶" (which prompts have)
                is_input_marker = line.startswith(self._input_marker) and ' ▶' not in line
                # Context line: only treat as stale if we don't have actual pending context
                is_context_line = line.startswith('📎 ') and not has_pending_context
                # print(f"[Claude] enter_input_mode: line[{i}]={repr(line)}, is_input_marker={is_input_marker}, is_context_line={is_context_line}")
                if is_input_marker or is_context_line:
                    cleanup_start = len('\n'.join(lines[:i]))
                    if i > 0:
                        cleanup_start += 1
                    continue
                elif line.strip():
                    break
            if cleanup_start >= 0 and cleanup_start < self.view.size():
                # print(f"[Claude] enter_input_mode: CLEANUP deleting from {cleanup_start} to {self.view.size()}")
                self.view.set_read_only(False)
                self.view.run_command("claude_replace", {
                    "start": cleanup_start,
                    "end": self.view.size(),
                    "text": ""
                })
                # Reset context region since we just deleted content
                self._pending_context_region = (0, 0)
            # else:
                # print(f"[Claude] enter_input_mode: no cleanup needed, cleanup_start={cleanup_start}")

        # Set read-only false first but DON'T set _input_mode yet
        # This prevents on_modified from saving wrong draft during setup
        self.view.set_read_only(False)

        # Clear any existing context region since we'll render it with input
        # print(f"[Claude] enter_input_mode: _pending_context_region={self._pending_context_region}")
        if self._pending_context_region[1] > self._pending_context_region[0]:
            # print(f"[Claude] enter_input_mode: clearing old context region")
            self._replace(self._pending_context_region[0], self._pending_context_region[1], "")
            self._pending_context_region = (0, 0)
            # _replace sets view to read-only, so set it back to False for append operations
            self.view.set_read_only(False)

        # Build input area (context + marker)
        self._input_area_start = self.view.size()
        # print(f"[Claude] enter_input_mode: building input area at position {self._input_area_start}")

        # Add newline prefix only if view has content AND doesn't already end with newline
        prefix = ""
        if self.view.size() > 0:
            last_char = self.view.substr(self.view.size() - 1)
            # print(f"[Claude] enter_input_mode: view has content, last_char={repr(last_char)}")
            if last_char != "\n":
                prefix = "\n"

        if prefix:
            # print(f"[Claude] enter_input_mode: adding newline prefix")
            self.view.run_command("append", {"characters": prefix})
        self._input_area_start = self.view.size()

        # Add context line if any
        from . import claude_code
        session = claude_code.get_session_for_view(self.view)
        if session and session.pending_context:
            names = [item.name for item in session.pending_context]
            ctx_line = f"📎 {', '.join(names)}\n"
            # print(f"[Claude] enter_input_mode: adding context line: {repr(ctx_line)}")
            self.view.run_command("append", {"characters": ctx_line})
        # else:
            # print(f"[Claude] enter_input_mode: no pending context to add, session={session is not None}")

        # Add input marker
        # print(f"[Claude] enter_input_mode: adding input marker at {self.view.size()}")
        self.view.run_command("append", {"characters": self._input_marker})
        self._input_start = self.view.size()  # After the marker
        # print(f"[Claude] enter_input_mode: input marker added, _input_start={self._input_start}")

        # NOW set input mode - after _input_start is correctly positioned
        # This ensures on_modified won't save wrong content as draft
        self._input_mode = True
        self.view.settings().set("claude_input_mode", True)

        # Move cursor to input position
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(self._input_start, self._input_start))
        self.view.show(self._input_start)

        # print(f"[Claude] enter_input_mode: COMPLETED, view_size={self.view.size()}, _input_mode={self._input_mode}, final_content={repr(self.view.substr(sublime.Region(0, self.view.size())))}")

    def exit_input_mode(self, keep_text: bool = False) -> str:
        """Exit input mode and return the input text."""
        if not self.view or not self._input_mode:
            return ""

        # Get the input text
        input_text = self.get_input_text()

        if not keep_text:
            # Remove entire input area (context + marker + text)
            start = getattr(self, '_input_area_start', self._input_start - len(self._input_marker))
            if start > 0:
                start -= 1  # Include preceding newline
            self.view.run_command("claude_replace", {
                "start": max(0, start),
                "end": self.view.size(),
                "text": ""
            })

        self._input_mode = False
        self.view.settings().set("claude_input_mode", False)
        self.view.set_read_only(True)
        return input_text

    def get_input_text(self) -> str:
        """Get current text in input region."""
        if not self.view or not self._input_mode:
            return ""
        return self.view.substr(sublime.Region(self._input_start, self.view.size()))

    def is_input_mode(self) -> bool:
        """Check if currently in input mode."""
        return self._input_mode

    def reset_input_mode(self) -> None:
        """Force reset input mode state - use when state gets corrupted."""
        if not self.view:
            return

        # Try to clean up leftover input markers in view content
        # Input markers are EXACTLY "◎ " (the marker) possibly followed by user text
        # Prompt lines are "◎ ... ▶" (have the arrow indicator)
        content = self.view.substr(sublime.Region(0, self.view.size()))
        cleanup_start = -1

        # Find input area at end - must be input marker (not prompt) or context line
        lines = content.split('\n')
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            # Input marker line: starts with "◎ " but does NOT contain " ▶" (which prompts have)
            is_input_marker = line.startswith(self._input_marker) and ' ▶' not in line
            is_context_line = line.startswith('📎 ')
            if is_input_marker or is_context_line:
                # Found input area - calculate position to remove
                cleanup_start = len('\n'.join(lines[:i]))
                if i > 0:
                    cleanup_start += 1  # Account for newline before this line
                # Continue checking for context lines above
                continue
            elif line.strip():
                # Non-empty line that's not input area - stop looking
                break

        if cleanup_start >= 0 and cleanup_start < self.view.size():
            self.view.set_read_only(False)
            self.view.run_command("claude_replace", {
                "start": cleanup_start,
                "end": self.view.size(),
                "text": ""
            })
            self.view.set_read_only(True)

        self._input_mode = False
        self._input_start = 0
        self._input_area_start = 0
        self.view.settings().set("claude_input_mode", False)
        self.view.set_read_only(True)
        # Also clear any pending regions that might be stale
        self._pending_context_region = (0, 0)

        # Re-enter input mode after reset to restore a clean, working state
        # Use a timeout to ensure view state is fully reset before re-entering
        sublime.set_timeout(self.enter_input_mode, 10)

    def is_in_input_region(self, point: int) -> bool:
        """Check if a point is within the editable input region."""
        if not self._input_mode:
            return False
        return point >= self._input_start

    # --- Public API ---

    def set_pending_context(self, context_items: list) -> None:
        """Show pending context - integrated with input mode if active."""
        # print(f"[Claude] set_pending_context: called with {len(context_items)} items, _input_mode={self._input_mode}")
        if not self.view or not self.view.is_valid():
            # print(f"[Claude] set_pending_context: view invalid, returning")
            return

        # If in input mode, re-render the whole input area with new context
        if self._input_mode:
            # print(f"[Claude] set_pending_context: already in input mode, re-rendering")
            # Save current input text
            input_text = self.get_input_text()
            # Exit and re-enter to refresh context display
            self.exit_input_mode(keep_text=False)
            self.enter_input_mode()
            # Restore input text
            if input_text:
                self.view.run_command("append", {"characters": input_text})
                end = self.view.size()
                self.view.sel().clear()
                self.view.sel().add(sublime.Region(end, end))
            return

        # Not in input mode - show context at end of view
        # print(f"[Claude] set_pending_context: not in input mode, showing at end")
        # Remove old context display
        start, end = self._pending_context_region
        # print(f"[Claude] set_pending_context: old region ({start}, {end})")
        if end > start:
            # print(f"[Claude] set_pending_context: removing old context display")
            self._replace(start, end, "")

        if not context_items:
            # print(f"[Claude] set_pending_context: no items, clearing region")
            self._pending_context_region = (0, 0)
            return

        # Build context display
        names = [item.name for item in context_items]
        text = f"\n📎 {', '.join(names)} ({len(names)} file{'s' if len(names) > 1 else ''})\n"
        # print(f"[Claude] set_pending_context: writing context text: {repr(text)}")

        # Write at end
        start = self.view.size()
        end = self._write(text)
        self._pending_context_region = (start, end)
        # print(f"[Claude] set_pending_context: wrote at ({start}, {end}), view_size now={self.view.size()}")
        self._scroll_to_end()

    def prompt(self, text: str, context_names: List[str] = None) -> None:
        """Start a new conversation with a prompt."""
        self.show()

        # Cancel any pending render - we're starting fresh
        self._render_pending = False

        # Exit input mode if active (query is starting)
        # Save any typed text as draft so it's not lost
        if self._input_mode:
            # Get session to save draft
            from . import claude_code
            session = claude_code.get_session_for_view(self.view)
            if session:
                session.draft_prompt = self.get_input_text()
            self.exit_input_mode(keep_text=False)
        else:
            # Not in input mode, but check for stale input markers
            # This can happen after restart or if state got corrupted
            content = self.view.substr(sublime.Region(0, self.view.size()))
            lines = content.split('\n')
            # Check if last non-empty lines look like input area (marker without ▶, context, or queued line)
            for line in reversed(lines[-5:]):  # Check last 5 lines
                if not line.strip():
                    continue
                is_input_marker = line.startswith(self._input_marker) and ' ▶' not in line
                is_context_line = line.startswith('📎 ')
                is_queued_line = line.startswith('⏳ ')
                if is_input_marker or is_context_line or is_queued_line:
                    # Found stale input marker - clean it up
                    self.reset_input_mode()
                break  # Only check the last non-empty line

        # Clear pending context display
        start, end = self._pending_context_region
        if end > start:
            self._replace(start, end, "")
            self._pending_context_region = (0, 0)

        # Finalize and save previous conversation
        prev_todos = []
        if self.current:
            # Ensure previous conversation is marked as done
            if self.current.working:
                self.current.working = False
                self._render_current()
            self.conversations.append(self.current)
            # Carry todos forward only if not all completed
            if not self.current.todos_all_done:
                prev_todos = self.current.todos

        # Start new
        self.current = Conversation(prompt=text, todos=prev_todos, context_names=context_names or [])
        self._update_title()  # Show working indicator

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
            line = f"{prefix}◎ {indented} ▶\n  📎 {context_str}\n"
            # print(f"[Claude] prompt: writing with context: {repr(line)}")
        else:
            line = f"{prefix}◎ {indented} ▶\n"
            # print(f"[Claude] prompt: writing without context: {repr(line)}")
        end = self._write(line)
        self.current.region = (start, end)
        # print(f"[Claude] prompt: wrote prompt at region ({start}, {end}), view_size={self.view.size()}")
        self._scroll_to_end()

    def tool(self, name: str, tool_input: dict = None) -> None:
        """Add a pending tool."""
        if not self.current:
            print(f"[Claude] tool({name}) - no current conversation")
            return

        print(f"[Claude] tool({name}) - adding pending")
        tool_input = tool_input or {}
        tool_call = ToolCall(name=name, tool_input=tool_input)
        self.current.events.append(tool_call)

        # Capture TodoWrite state
        if name == "TodoWrite" and "todos" in tool_input:
            self.current.todos = [
                TodoItem(content=t.get("content", ""), status=t.get("status", "pending"))
                for t in tool_input["todos"]
            ]

        self._render_current()

    def tool_done(self, name: str, result: str = None) -> None:
        """Mark most recent pending tool with this name as done."""
        if not self.current:
            print(f"[Claude] tool_done({name}) - no current conversation")
            return
        # Find the last pending tool with this name
        for event in reversed(self.current.events):
            if isinstance(event, ToolCall) and event.name == name and event.status == PENDING:
                event.status = DONE
                event.result = result
                print(f"[Claude] tool_done({name}) - marked done")
                self._render_current()
                return
        # No pending tool found, add as done
        print(f"[Claude] tool_done({name}) - not found pending, adding as done")
        self.current.events.append(ToolCall(name=name, tool_input={}, status=DONE, result=result))
        self._render_current()

    def tool_error(self, name: str, result: str = None) -> None:
        """Mark most recent pending tool with this name as error."""
        if not self.current:
            return
        # Find the last pending tool with this name
        for event in reversed(self.current.events):
            if isinstance(event, ToolCall) and event.name == name and event.status == PENDING:
                event.status = ERROR
                event.result = result
                self._render_current()
                return
        # No pending tool found, add as error
        self.current.events.append(ToolCall(name=name, tool_input={}, status=ERROR))
        self._render_current()

    def text(self, content: str) -> None:
        """Add response text."""
        if not self.current:
            return

        self.current.events.append(content)
        self._render_current()

    def meta(self, duration: float, cost: float = None) -> None:
        """Set completion meta - marks conversation as done."""
        if not self.current:
            return

        self.current.duration = duration
        self.current.working = False
        self._render_current()

    def interrupted(self) -> None:
        """Show interrupted indicator."""
        if not self.current:
            return
        self.current.working = False
        # Mark any pending tools as error
        for event in self.current.events:
            if isinstance(event, ToolCall) and event.status == PENDING:
                event.status = ERROR
        # Clear any pending permission prompt
        if self.pending_permission:
            self._remove_permission_block()
            self.pending_permission = None
        # Append interrupted text
        self.current.events.append("\n\n*[interrupted]*\n")
        self._render_current()

    def clear(self) -> None:
        """Clear all output (can undo with Cmd+Z)."""
        # Remember if we were in input mode
        was_input_mode = self._input_mode
        # Check if agent is currently working (before we clear state)
        was_working = self.current and self.current.working

        if self._input_mode:
            self.exit_input_mode(keep_text=False)
        if self.view and self.view.is_valid():
            # Save content for undo
            self._cleared_content = self.view.substr(sublime.Region(0, self.view.size()))
            self.view.set_read_only(False)
            self.view.run_command("claude_clear_all")
            self.view.set_read_only(True)
            # Reset view settings that might be stale from old session
            self.view.settings().set("claude_input_mode", False)
        self.conversations = []
        self.current = None
        self.pending_permission = None
        self._permission_queue.clear()
        self.auto_allow_tools.clear()
        self._pending_context_region = (0, 0)
        self._input_mode = False
        self._input_start = 0
        self._input_area_start = 0
        # Clean up any tracked permission region
        if self.view:
            self.view.erase_regions("claude_permission_block")

        # If agent was working, create a stub conversation to receive further output
        # This prevents output from being silently discarded after clear
        if was_working:
            self.current = Conversation(prompt="(continued)", working=True)
            self.current.region = (0, 0)  # Will be set on first render
            self._update_title()  # Show working indicator
            return  # Don't enter input mode while working

        # Re-enter input mode if we were in it
        if was_input_mode:
            self.enter_input_mode()

    def undo_clear(self) -> None:
        """Restore content from last clear."""
        if self._cleared_content and self.view and self.view.is_valid():
            self._write(self._cleared_content)
            self._cleared_content = None
            self._scroll_to_end()

    def reset_active_states(self) -> None:
        """Reset active states when reconnecting after Sublime restart.

        Clears pending permissions, marks pending tools as interrupted,
        resets input mode, and resets the view title to remove any stale spinner.
        """
        # Reset input mode state (view settings may persist across restart)
        self.reset_input_mode()

        # Clear permission state
        if self.pending_permission:
            self._remove_permission_block()
            self.pending_permission = None
        self._permission_queue.clear()

        # Mark any pending tools in current conversation as error
        if self.current:
            had_pending = False
            for event in self.current.events:
                if isinstance(event, ToolCall) and event.status == PENDING:
                    event.status = ERROR
                    had_pending = True
            if had_pending:
                self.current.events.append("\n\n*[session reconnected]*\n")
                self._render_current()

    def _remove_permission_block(self) -> None:
        """Remove permission block from view without callback."""
        if not self.pending_permission or not self.view:
            return
        perm = self.pending_permission
        # Remove button regions
        for btn_type in perm.button_regions:
            self.view.erase_regions(f"claude_btn_{btn_type}")
        # Get current region from tracked region (auto-adjusted for text shifts)
        regions = self.view.get_regions("claude_permission_block")
        if regions:
            region = regions[0]
            self._replace(region.begin(), region.end(), "")
        self.view.erase_regions("claude_permission_block")

    # --- Permission UI ---

    def clear_stale_permission(self, current_pid: int) -> None:
        """Clear permission UI if it's for an older request (bridge moved on)."""
        if not self.pending_permission:
            return

        # If pending permission is for an older pid, it's stale
        if self.pending_permission.id < current_pid:
            print(f"[Claude] clearing stale permission pid={self.pending_permission.id} (current={current_pid})")
            self._clear_permission()
            self.pending_permission = None
            self._permission_queue.clear()  # Also clear queue - they're all stale

    def clear_all_permissions(self) -> None:
        """Clear all pending permissions (called when query finishes)."""
        if self.pending_permission:
            print(f"[Claude] clearing leftover permission pid={self.pending_permission.id}")
            self._clear_permission()
            self.pending_permission = None
        self._permission_queue.clear()

    def permission_request(self, pid: int, tool: str, tool_input: dict, callback: Callable[[str], None]) -> None:
        """Show a permission request in the view."""
        import time
        self.show(focus=False)  # Don't steal focus from other views

        # IMPORTANT: Ensure input mode is OFF so permission keys (Y/N/S/A) work
        # Permission keys require claude_input_mode=false in Default.sublime-keymap
        if self.view:
            self.view.settings().set("claude_input_mode", False)

        # NOTE: Don't call clear_stale_permission here - concurrent permissions are valid
        # Stale permission cleanup is handled by clear_all_permissions() on query completion

        # Check if tool is auto-allowed for session (match against saved patterns)
        for pattern in self.auto_allow_tools:
            if self._match_auto_allow_pattern(tool, tool_input, pattern):
                print(f"[Claude] permission_request pid={pid}: auto-allowed (matched pattern: {pattern})")
                callback(PERM_ALLOW)
                return

        # Check if user chose "allow for 30s" recently
        now = time.time()
        if self._last_allowed_tool == tool and (now - self._last_allowed_time) < 30:
            print(f"[Claude] permission_request pid={pid}: auto-allowing (30s window)")
            callback(PERM_ALLOW)
            return

        # Create the request
        perm = PermissionRequest(
            id=pid,
            tool=tool,
            tool_input=tool_input,
            callback=callback,
        )

        # If there's already a pending permission, queue this one
        if self.pending_permission and self.pending_permission.callback:
            print(f"[Claude] permission_request pid={pid}: queued (existing pending pid={self.pending_permission.id})")
            self._permission_queue.append(perm)
            return

        # Show this one
        print(f"[Claude] permission_request pid={pid}: showing prompt (queue={len(self._permission_queue)})")
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

        # Format tool details and display name
        detail = ""
        display_tool = tool
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
        elif tool == "Skill" and "skill" in tool_input:
            # Show skill name as the tool name for better clarity
            skill_name = tool_input["skill"]
            display_tool = f"Skill: {skill_name}"
            # Show args if present
            if "args" in tool_input and tool_input["args"]:
                detail = tool_input["args"]
        else:
            # Generic: show first param
            for k, v in list(tool_input.items())[:1]:
                detail = f"{k}: {str(v)[:60]}"

        # Build permission block
        lines = [
            "\n",
            f"  ⚠ Allow {display_tool}",
        ]
        if detail:
            lines.append(f": {detail}")
        lines.append("?\n")
        lines.append("    ")

        # Track button positions relative to block start
        text_before_buttons = "".join(lines)

        # Check if this is a dangerous command that shouldn't have "Always allow"
        hide_always = False
        if tool == "Bash" and "command" in tool_input:
            cmd = tool_input["command"]
            # Hide "Always" for dangerous commands: rm, git checkout, git reset
            dangerous_patterns = [
                'rm ', 'rm\t',
                'git checkout', 'git reset',
                'git clean', 'git stash drop'
            ]
            if any(pattern in cmd for pattern in dangerous_patterns):
                hide_always = True

        # Buttons
        btn_y = "[Y] Allow"
        btn_n = "[N] Deny"
        btn_s = "[S] Allow 30s"

        # Create descriptive "Always" button based on what pattern will be saved
        always_hint = ""
        if tool == "Bash" and "command" in tool_input:
            cmd = tool_input["command"]
            first_word = cmd.split()[0] if cmd.split() else ""
            if '/' in first_word:
                first_word = first_word.split('/')[-1]
            if first_word:
                always_hint = f" `{first_word}:*`"
        elif tool in ("Read", "Write", "Edit") and "file_path" in tool_input:
            import os
            dir_path = os.path.dirname(tool_input["file_path"])
            if dir_path:
                # Shorten long paths
                if len(dir_path) > 25:
                    dir_path = "..." + dir_path[-22:]
                always_hint = f" in `{dir_path}/`"
        btn_a = f"[A] Always{always_hint}"

        lines.append(btn_y)
        lines.append("  ")
        lines.append(btn_n)
        lines.append("  ")
        lines.append(btn_s)
        if not hide_always:
            lines.append("  ")
            lines.append(btn_a)
        else:
            lines.append("  (Always disabled for safety)")
        lines.append("\n")

        text = "".join(lines)

        # Write to view
        start = self.view.size()
        end = self._write(text)
        perm.region = (start, end)

        # Add tracked region for the whole permission block (auto-adjusts when text shifts)
        self.view.add_regions(
            "claude_permission_block",
            [sublime.Region(start, end)],
            "",
            "",
            sublime.HIDDEN,
        )

        # Calculate button regions (absolute positions)
        btn_start = start + len(text_before_buttons)
        perm.button_regions[PERM_ALLOW] = (btn_start, btn_start + len(btn_y))
        btn_start += len(btn_y) + 2  # +2 for "  "
        perm.button_regions[PERM_DENY] = (btn_start, btn_start + len(btn_n))
        btn_start += len(btn_n) + 2
        perm.button_regions[PERM_ALLOW_SESSION] = (btn_start, btn_start + len(btn_s))
        btn_start += len(btn_s) + 2
        if not hide_always:
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

        # Remove button regions
        for btn_type in perm.button_regions:
            self.view.erase_regions(f"claude_btn_{btn_type}")

        # Get current region from tracked region (auto-adjusted for text shifts)
        regions = self.view.get_regions("claude_permission_block")
        if regions:
            region = regions[0]
            self._replace(region.begin(), region.end(), "")

            # Update conversation region end to account for removed permission block
            # This prevents _do_render from extending and overwriting content
            if self.current:
                self.current.region = (self.current.region[0], self.view.size())

        self.view.erase_regions("claude_permission_block")
        # Don't clear pending_permission - keep it to detect rapid same-tool requests
        # It will be overwritten when a different tool request comes in

    def _make_auto_allow_pattern(self, tool: str, tool_input: dict) -> str:
        """Create a fine-grained auto-allow pattern from tool and input.

        For Bash: extracts command prefix (first word) -> "Bash(git:*)"
        For Read/Write/Edit: uses directory path -> "Read(/src/)"
        For MCP tools: uses full tool name
        """
        import os

        if not tool_input:
            return tool

        if tool == "Bash":
            command = tool_input.get("command", "")
            if command:
                # Extract first word as prefix (e.g., "git", "npm", "python")
                first_word = command.split()[0] if command.split() else ""
                if first_word:
                    # Strip path prefix (e.g., /usr/bin/git -> git)
                    if '/' in first_word:
                        first_word = first_word.split('/')[-1]
                    return f"Bash({first_word}:*)"
        elif tool in ("Read", "Write", "Edit"):
            file_path = tool_input.get("file_path", "")
            if file_path:
                # Use directory as pattern (like Claude CLI)
                dir_path = os.path.dirname(file_path)
                if dir_path:
                    return f"{tool}({dir_path}/)"
        elif tool == "Skill":
            skill_name = tool_input.get("skill", "")
            if skill_name:
                return f"Skill({skill_name})"
        # For other tools, just use the tool name
        return tool

    def _match_auto_allow_pattern(self, tool: str, tool_input: dict, pattern: str) -> bool:
        """Check if tool use matches an auto-allow pattern.

        Supports:
            - Simple tool match: "Bash" matches any Bash command
            - Prefix match: "Bash(git:*)" matches commands starting with "git"
            - Directory match: "Read(/src/)" matches files under /src/
        """
        import fnmatch
        import os

        # Parse pattern: "Tool(specifier)" or just "Tool"
        if '(' in pattern and pattern.endswith(')'):
            paren_idx = pattern.index('(')
            parsed_tool = pattern[:paren_idx]
            specifier = pattern[paren_idx + 1:-1]
        else:
            parsed_tool = pattern
            specifier = None

        # Tool name must match
        if not fnmatch.fnmatch(tool, parsed_tool):
            return False

        # No specifier = match all uses of this tool
        if specifier is None:
            return True

        # Bash command matching
        if tool == "Bash":
            command = tool_input.get("command", "")
            if not command:
                return False
            # Prefix match with :*
            if specifier.endswith(":*"):
                prefix = specifier[:-2]
                # Check if command or any sub-command starts with prefix
                if command.startswith(prefix):
                    return True
                # Extract first word of command
                first_word = command.split()[0] if command.split() else ""
                if '/' in first_word:
                    first_word = first_word.split('/')[-1]
                return first_word.startswith(prefix)
            return command == specifier

        # Read/Write/Edit directory matching
        if tool in ("Read", "Write", "Edit"):
            file_path = tool_input.get("file_path", "")
            if not file_path:
                return False
            # Directory match (pattern ends with /)
            if specifier.endswith('/'):
                return file_path.startswith(specifier) or os.path.dirname(file_path) + '/' == specifier
            # Glob match
            if any(c in specifier for c in ['*', '?', '[']):
                return fnmatch.fnmatch(file_path, specifier)
            return file_path == specifier

        # Skill matching
        if tool == "Skill":
            skill_name = tool_input.get("skill", "")
            return skill_name == specifier

        return False

    def _respond_permission_with_callback(self, response: str, callback, tool: str, tool_input: dict = None) -> None:
        """Respond to a permission request with given callback."""
        import time

        # Handle "allow all" - save to project settings and remember for this session
        if response == PERM_ALLOW_ALL:
            pattern = self._make_auto_allow_pattern(tool, tool_input)
            self.auto_allow_tools.add(pattern)
            self._save_auto_allowed_tool(pattern)
            response = PERM_ALLOW

        # Handle "allow 30s" - set timed auto-allow
        if response == PERM_ALLOW_SESSION:
            self._last_allowed_tool = tool
            self._last_allowed_time = time.time()
            response = PERM_ALLOW

        # Clear the UI
        self._clear_permission()
        self.pending_permission = None

        # Move cursor to end so auto-scroll resumes
        self._move_cursor_to_end()

        # Call the callback
        callback(response)

        # Process next queued permission if any
        self._process_permission_queue()

    def _save_auto_allowed_tool(self, tool: str) -> None:
        """Save a tool to the auto-allowed list in project settings."""
        import os
        import json

        # Get project directory
        folders = self.window.folders()
        if not folders:
            print(f"[Claude] Cannot save auto-allowed tool: no project folder")
            return

        project_dir = folders[0]
        settings_dir = os.path.join(project_dir, ".claude")
        settings_path = os.path.join(settings_dir, "settings.json")

        # Load current settings
        settings = {}
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r") as f:
                    settings = json.load(f)
            except Exception as e:
                print(f"[Claude] Error loading settings: {e}")
                return

        # Add tool to auto-allowed list
        auto_allowed = settings.get("autoAllowedMcpTools", [])
        if tool not in auto_allowed:
            auto_allowed.append(tool)
            settings["autoAllowedMcpTools"] = auto_allowed

            # Save settings
            os.makedirs(settings_dir, exist_ok=True)
            try:
                with open(settings_path, "w") as f:
                    json.dump(settings, f, indent=2)
                print(f"[Claude] Saved auto-allowed tool: {tool}")
                sublime.status_message(f"Auto-allowed: {tool}")
            except Exception as e:
                print(f"[Claude] Failed to save settings: {e}")

    def _move_cursor_to_end(self) -> None:
        """Move cursor to end of view."""
        if self.view and self.view.is_valid():
            end = self.view.size()
            self.view.sel().clear()
            self.view.sel().add(sublime.Region(end, end))
            self.view.show(end)

    def _process_permission_queue(self) -> None:
        """Process the next permission request in queue."""
        import time

        while self._permission_queue:
            perm = self._permission_queue.pop(0)

            # Check if auto-allowed now (user may have clicked "Always" or "30s")
            auto_allowed = False
            for pattern in self.auto_allow_tools:
                if self._match_auto_allow_pattern(perm.tool, perm.tool_input, pattern):
                    print(f"[Claude] _process_queue pid={perm.id}: auto-allowed (matched: {pattern})")
                    perm.callback(PERM_ALLOW)
                    auto_allowed = True
                    break
            if auto_allowed:
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
            self._respond_permission_with_callback(response, callback, perm.tool, perm.tool_input)
            return True
        return False

    def _render_current(self, auto_scroll: bool = True) -> None:
        """Re-render current conversation in place (debounced)."""
        if not self.current or not self.view:
            return

        # Debounce: if render already pending, skip
        if self._render_pending:
            return
        self._render_pending = True
        self._auto_scroll = auto_scroll  # Store for _do_render
        sublime.set_timeout(self._do_render, 10)

    def advance_spinner(self) -> None:
        """Advance spinner animation frame and re-render if working."""
        if self.current and self.current.working:
            self._spinner_frame += 1
            self._render_current(auto_scroll=False)  # Don't scroll during spinner updates

    def _do_render(self) -> None:
        """Actually perform the render."""
        self._render_pending = False
        if not self.current or not self.view:
            return

        # Don't render while in input mode - it would corrupt the input region
        if self._input_mode:
            return

        # Validate region bounds - protect against stale region data
        view_size = self.view.size()
        start, end = self.current.region
        if start > view_size or end > view_size:
            # Region is invalid - recalculate from view content
            # Find last prompt marker and use that as start
            content = self.view.substr(sublime.Region(0, view_size))
            # Look for the last prompt marker that matches our prompt
            prompt_marker = f"◎ {self.current.prompt[:20]}"
            last_pos = content.rfind(prompt_marker)
            if last_pos >= 0:
                start = last_pos
                end = view_size
                self.current.region = (start, end)
            else:
                # Can't find our prompt - skip this render
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
        # Include context indicator if present
        if self.current.context_names:
            context_str = ", ".join(self.current.context_names)
            lines.append(f"{prefix}◎ {indented_prompt} ▶\n")
            lines.append(f"  📎 {context_str}\n")
        else:
            lines.append(f"{prefix}◎ {indented_prompt} ▶\n")

        # Events in time order (text chunks and tools interleaved)
        if self.current.events:
            lines.append("\n")
            for event in self.current.events:
                if isinstance(event, str):
                    # Text chunk
                    lines.append(event)
                    if not event.endswith("\n"):
                        lines.append("\n")
                elif isinstance(event, ToolCall):
                    # Tool call
                    symbol = self.SYMBOLS[event.status]
                    detail = self._format_tool_detail(event)
                    lines.append(f"  {symbol} {event.name}{detail}\n")

        # Working indicator at bottom (animated)
        if self.current.working:
            spinner = SPINNER_FRAMES[self._spinner_frame % len(SPINNER_FRAMES)]
            lines.append(f"  {spinner}\n")

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
        view_size = self.view.size()
        # If there's content after our region, extend end to clean it up
        # This handles race conditions where content was orphaned from previous renders
        # BUT: Don't extend if there's a permission block - that's intentional content after the region
        if end < view_size and not (self.pending_permission and self.pending_permission.callback):
            end = view_size
        new_end = self._replace(start, end, text)
        self.current.region = (start, new_end)

        # Update title to reflect working state
        self._update_title()

        # Re-render permission block if pending (it may have been shifted)
        if self.pending_permission and self.pending_permission.callback:
            self._remove_permission_block()
            self._render_permission()

        # Scroll after render completes (only if auto_scroll is enabled)
        if getattr(self, '_auto_scroll', True):
            self._scroll_to_end()

    def _format_tool_detail(self, tool: ToolCall) -> str:
        """Format tool detail string."""
        detail = ""
        tool_input = tool.tool_input or {}

        if tool.name == "Skill" and "skill" in tool_input:
            # Show the actual skill name instead of just "Skill"
            skill_name = tool_input["skill"]
            detail = f": {skill_name}"
        elif tool.name == "Bash" and "command" in tool_input:
            cmd = tool_input["command"]
            detail = f": {cmd}"
            # Show output for completed Bash commands
            if tool.result and tool.status in (DONE, ERROR):
                detail += self._format_bash_result(tool.result)
        elif tool.name == "Read" and "file_path" in tool_input:
            detail = f": {tool_input['file_path']}"
            # Show line count for completed Read
            if tool.result and tool.status == DONE:
                detail += self._format_read_result(tool.result)
        elif tool.name == "Edit" and "file_path" in tool_input:
            detail = f": {tool_input['file_path']}"
            # Show diff for Edit tool
            old = tool_input.get("old_string", "")
            new = tool_input.get("new_string", "")
            if old or new:
                detail += self._format_edit_diff(old, new)
        elif tool.name == "Write" and "file_path" in tool_input:
            detail = f": {tool_input['file_path']}"
        elif tool.name == "Glob" and "pattern" in tool_input:
            detail = f": {tool_input['pattern']}"
            # Show file count for completed Glob
            if tool.result and tool.status == DONE:
                detail += self._format_glob_result(tool.result)
        elif tool.name == "Grep" and "pattern" in tool_input:
            detail = f": {tool_input['pattern']}"
            # Show match count for completed Grep
            if tool.result and tool.status == DONE:
                detail += self._format_grep_result(tool.result)
        elif tool.name == "WebSearch" and "query" in tool_input:
            detail = f": {tool_input['query']}"
        elif tool.name == "WebFetch" and "url" in tool_input:
            detail = f": {tool_input['url']}"
        elif tool.name == "Task" and "subagent_type" in tool_input:
            # Show agent type and description for Task launches
            subagent = tool_input["subagent_type"]
            desc = tool_input.get("description", "")
            detail = f": {subagent}" + (f" - {desc}" if desc else "")
        elif tool.name == "NotebookEdit" and "notebook_path" in tool_input:
            detail = f": {tool_input['notebook_path']}"
        elif tool.name == "TodoWrite" and "todos" in tool_input:
            # Show todo count for TodoWrite
            todos = tool_input["todos"]
            count = len(todos) if isinstance(todos, list) else "?"
            detail = f": {count} task{'s' if count != 1 else ''}"
        elif tool.name in ("ask_user", "mcp__sublime__ask_user") and "question" in tool_input:
            question = tool_input["question"]
            detail = f": {question}"
            # Show Q&A for completed ask_user
            if tool.result and tool.status == DONE:
                detail += self._format_ask_user_result(tool.result, question)
        # Generic MCP tool result display
        elif tool.name.startswith("mcp__sublime__") and tool.result and tool.status == DONE:
            detail += self._format_mcp_result(tool.result)

        return detail

    def _format_bash_result(self, result: str) -> str:
        """Format Bash command output (head + tail if long)."""
        if not result or not result.strip():
            return ""
        lines = result.strip().split("\n")
        max_head = 3
        max_tail = 5
        max_width = 80
        output_lines = []

        def truncate(line):
            return line[:max_width] + "…" if len(line) > max_width else line

        if len(lines) <= max_head + max_tail:
            # Show all lines
            for line in lines:
                output_lines.append(f"    │ {truncate(line)}")
        else:
            # Show head
            for line in lines[:max_head]:
                output_lines.append(f"    │ {truncate(line)}")
            # Show omitted count
            omitted = len(lines) - max_head - max_tail
            output_lines.append(f"    │ ... ({omitted} more lines)")
            # Show tail
            for line in lines[-max_tail:]:
                output_lines.append(f"    │ {truncate(line)}")

        return "\n" + "\n".join(output_lines)

    def _format_glob_result(self, result: str) -> str:
        """Format Glob result as file count."""
        if not result or not result.strip():
            return " → 0 files"
        lines = [l for l in result.strip().split("\n") if l.strip()]
        return f" → {len(lines)} files"

    def _format_grep_result(self, result: str) -> str:
        """Format Grep result as match count."""
        if not result or not result.strip():
            return " → 0 matches"
        lines = [l for l in result.strip().split("\n") if l.strip()]
        # Try to count unique files
        files = set()
        for line in lines:
            if ":" in line:
                files.add(line.split(":")[0])
        if files:
            return f" → {len(lines)} matches in {len(files)} files"
        return f" → {len(lines)} matches"

    def _format_read_result(self, result: str) -> str:
        """Format Read result as line count."""
        if not result or not result.strip():
            return " → 0 lines"
        lines = result.strip().split("\n")
        return f" → {len(lines)} lines"

    def _format_mcp_result(self, result: str) -> str:
        """Format generic MCP tool result."""
        try:
            import re
            import ast
            import json

            # Extract the 'text' field from MCP format: {'type': 'text', 'text': '...'}
            match = re.search(r"'text':\s*'((?:[^'\\]|\\.)*)'", result)
            if not match:
                # Try double quotes
                match = re.search(r'"text":\s*"((?:[^"\\]|\\.)*)"', result)

            if match:
                text = match.group(1)
                # Decode escapes
                text = text.replace('\\n', '\n').replace("\\'", "'").replace('\\"', '"')
                # Try to parse as JSON for pretty display
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        # Compact single-line for small results
                        compact = json.dumps(data, ensure_ascii=False)
                        if len(compact) < 60:
                            return f" → {compact}"
                        # Multi-line for larger results
                        lines = []
                        for k, v in list(data.items())[:5]:
                            v_str = str(v)[:50]
                            lines.append(f"    │ {k}: {v_str}")
                        if len(data) > 5:
                            lines.append(f"    │ ... ({len(data) - 5} more)")
                        return "\n" + "\n".join(lines)
                    elif isinstance(data, list):
                        return f" → [{len(data)} items]"
                except:
                    # Plain text, show truncated
                    if len(text) > 60:
                        return f" → {text[:60]}..."
                    return f" → {text}" if text else ""

            return ""
        except:
            return ""

    def _format_ask_user_result(self, result: str, question: str) -> str:
        """Format ask_user Q&A result."""
        try:
            import re
            import codecs
            # Extract answer from various formats
            # Format: {'type': 'text', 'text': '{\n  "answer": "...",\n  "cancelled": false\n}'}

            # Look for "answer": "..." pattern - allow escaped chars inside
            match = re.search(r'"answer":\s*"((?:[^"\\]|\\.)*)"', result)
            if match:
                answer = match.group(1)
                # Decode unicode escapes (e.g., \\u4e2d -> 中)
                # Handle double-escaped: \\\\u -> \\u first
                answer = answer.replace('\\\\u', '\\u')
                answer = codecs.decode(answer, 'unicode_escape')
                return f"\n    → {answer}"

            # Check for cancelled
            if '"cancelled": true' in result or '"cancelled":true' in result:
                return "\n    → (cancelled)"

            return ""
        except Exception as e:
            print(f"[Claude] _format_ask_user_result error: {e}, result={result[:50]}")
            return ""

    def _format_edit_diff(self, old: str, new: str) -> str:
        """Format Edit diff using unified diff format."""
        import difflib
        if not old and not new:
            return ""

        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)

        # Ensure last lines have newlines for clean diff
        if old_lines and not old_lines[-1].endswith('\n'):
            old_lines[-1] += '\n'
        if new_lines and not new_lines[-1].endswith('\n'):
            new_lines[-1] += '\n'

        diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=''))

        if not diff:
            return ""

        # Skip the --- and +++ header lines, start from @@
        diff_lines = []
        for line in diff:
            if line.startswith('---') or line.startswith('+++'):
                continue
            # Remove trailing newline for display
            diff_lines.append(line.rstrip('\n'))

        if not diff_lines:
            return ""

        return "\n```diff\n" + "\n".join(diff_lines) + "\n```"


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
        from .core import get_active_session
        window = self.view.window()
        if window:
            session = get_active_session(window)
            if session:
                session.output.undo_clear()
