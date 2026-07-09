"""Structured output view with region tracking."""
import os
import sublime
import sublime_plugin
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Callable, Any

from .constants import SPINNER_FRAMES, BACKEND_ABBREV, CONTEXT_PREFIX, BACKGROUND_PREFIX
from .output_pending import clear_pending_block
from .tool_formatters import (
    format_tool_detail,
    MEDIA_TOOLS,
    extract_media_path,
    media_display_path,
    is_image_path,
    is_video_path,
)

import re as _re

# Status-icon chars that prefix a tab title (see ClaudeOutput._update_title).
_TITLE_ICON_RE = _re.compile(r'^(?:[◉◇•❓⏸↻⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*)+')


def _title_abbrev_tokens():
    """The set of backend abbrev tokens _update_title can emit as `ABBR> `,
    resolved the same way (registry abbrev → static map → name[:2]). Used by
    strip_title_decoration so the strip stays in sync with the decoration and
    covers custom providers (As / GM / Km / …), not just the built-ins."""
    toks = set()
    try:
        from . import backends as _b
        for name, spec in _b.all_backends().items():
            tok = spec.abbrev or BACKEND_ABBREV.get(name) or name[:2].upper()
            if tok:
                toks.add(tok)
    except Exception:
        pass
    for tok in BACKEND_ABBREV.values():
        if tok:
            toks.add(tok)
    return toks


def strip_title_decoration(title):
    """Inverse of ClaudeOutput._update_title: peel status icons and any number
    of stacked `ABBR> ` prefixes (+ legacy "[x] " / "Claude: " forms) so that
    re-decorating across reconnects can't accumulate prefixes. Returns the
    clean base name (empty string if nothing left). Does NOT touch a trailing
    truncation ellipsis — callers that need it handle it themselves."""
    if not title:
        return ""
    name = title
    # Legacy forms first.
    if name.startswith("[") and "] " in name:
        name = name[name.index("] ") + 2:]
    if name.startswith("Claude: "):
        name = name[8:]
    toks = sorted(_title_abbrev_tokens(), key=len, reverse=True)  # DSR before DS
    abbr_re = _re.compile(r'^(?:%s)>\s*' % "|".join(_re.escape(t) for t in toks)) if toks else None
    # Loop so any interleaved icon/abbrev stacking from prior reconnects
    # collapses completely in one call.
    while True:
        new = _TITLE_ICON_RE.sub('', name)
        if abbr_re:
            new = abbr_re.sub('', new)
        if new == name:
            break
        name = new
    return name.strip()


# Status constants
PENDING = "pending"
DONE = "done"
ERROR = "error"
BACKGROUND = "background"

# Permission button constants
PERM_ALLOW = "allow"
PERM_DENY = "deny"
PERM_ALLOW_ALL = "allow_all"
PERM_ALLOW_SESSION = "allow_session"  # Allow same tool for 30s


PLAN_APPROVE = "approve"
PLAN_REJECT = "reject"
PLAN_VIEW = "view"


@dataclass
class PlanApproval:
    """A pending plan approval request."""
    id: int
    plan_file: str
    allowed_prompts: List[dict]
    callback: Callable[[str], None]  # Called with PLAN_APPROVE or PLAN_REJECT
    region: tuple = (0, 0)
    button_regions: Dict[str, tuple] = field(default_factory=dict)


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
class QuestionRequest:
    """A pending inline question request."""
    qid: int
    questions: List[dict]     # [{question, options, header, multiSelect}]
    current_idx: int = 0
    answers: Dict[str, str] = field(default_factory=dict)
    callback: Callable = None  # Called with answers dict or None (cancelled)
    region: tuple = (0, 0)
    button_regions: Dict[str, tuple] = field(default_factory=dict)
    selected: set = field(default_factory=set)  # multi-select toggles


@dataclass
class ToolCall:
    """A single tool call."""
    name: str
    tool_input: dict
    status: str = PENDING  # pending, done, error, background
    result: Optional[str] = None  # tool result content
    id: Optional[str] = None  # tool_use_id, for precise matching


@dataclass
class TodoItem:
    """A todo item from TodoWrite or Task* tools."""
    content: str
    status: str  # pending, in_progress, completed, cancelled, deleted
    id: str = ""  # server-assigned id (set by TaskCreate result / TaskList)


# Statuses that mean "no longer open work" — hide from widget + don't carry.
_TODO_CLOSED = frozenset({
    "completed", "cancelled", "canceled", "deleted",
})


def _todo_status_norm(status: str) -> str:
    return (status or "pending").strip().lower().replace("-", "_")


def _todo_is_open(todo: "TodoItem") -> bool:
    """True if the item is still active work (not completed/cancelled/deleted)."""
    if not (todo.content or "").strip():
        return False
    return _todo_status_norm(todo.status) not in _TODO_CLOSED


def _todo_is_active(todo: "TodoItem") -> bool:
    return _todo_status_norm(todo.status) == "in_progress"


def _todo_is_completed(todo: "TodoItem") -> bool:
    return _todo_status_norm(todo.status) == "completed"


def _open_todos(todos: list) -> list:
    """Open items only — used for widget + carry-forward between rounds."""
    return [t for t in (todos or []) if _todo_is_open(t)]


@dataclass
class GoalState:
    """Grok update_goal — one active autonomous objective (not a Task list)."""
    status: str = "active"  # active | completed | blocked
    message: str = ""
    blocked_reason: str = ""


def _goal_is_open(goal: Optional["GoalState"]) -> bool:
    """Carry sticky goal only while still active/blocked (not completed)."""
    return bool(goal) and goal.status in ("active", "blocked")


@dataclass
class Conversation:
    """A single prompt + tools + response + meta."""
    prompt: str = ""
    # Events in time order - either ToolCall or str (text chunk)
    events: List = field(default_factory=list)
    todos: List[TodoItem] = field(default_factory=list)  # current todo state
    todos_all_done: bool = False  # True when all todos completed (don't carry to next)
    goal: Optional[GoalState] = None  # sticky single goal from update_goal
    working: bool = True  # True while processing, False when done
    duration: float = 0.0
    usage: dict = None
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
        "background": "⚙",
    }

    def __init__(self, window: sublime.Window):
        self.window = window
        self.view: Optional[sublime.View] = None
        self.conversations: List[Conversation] = []
        self.current: Optional[Conversation] = None
        self.pending_permission: Optional[PermissionRequest] = None
        self._permission_queue: List[PermissionRequest] = []  # Queue for multiple requests
        self.pending_plan: Optional[PlanApproval] = None
        self.pending_question: Optional[QuestionRequest] = None
        self.auto_allow_tools: set = self._load_persisted_auto_allow()  # Tools auto-allowed for this session
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
        self._media_phantom_set = None  # inline image previews (minihtml data: URIs)
        self._media_uri_cache: Dict[str, tuple] = {}  # path|edge -> (mtime, uri, w, h)
        self._media_anchor: Dict[str, int] = {}  # abs path -> buffer pt for popup

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
        self.view.settings().set("auto_indent", False)
        self._apply_output_settings()
        sublime.load_settings("ClaudeOutput.sublime-settings").add_on_change(
            f"claude_output_{self.view.id()}", self._apply_output_settings
        )
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
        # Strip any leading status glyphs so they never accumulate in the stored
        # base name (e.g. a ↻/◇ prefix leaking back in → "◇ ↻ name").
        import re
        name = re.sub(r'^(?:[◉◇•❓⏸↻⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*)+', '', name or "") or "Claude"
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
        # ◉ = working, ◇ = idle, • = inactive+working, ❓ = questioning, ⏸ = sleeping
        from . import claude_code
        session = claude_code.get_session_for_view(self.view)
        is_sleeping = session and session.is_sleeping
        is_questioning = bool(
            (self.pending_permission and self.pending_permission.callback) or
            (self.pending_question and self.pending_question.callback) or
            (self.pending_plan and self.pending_plan.callback)
        )
        # Tab title reflects the session-level working flag so silent wake-up
        # queries (bg-task notifications, retain injects, …) still show ◉
        # even though no visible Conversation was opened for them.
        is_working = (
            (session and session.working)
            or (self.current and self.current.working)
        )
        # ↻ reflects a *confirmed pending* wake (next_wake_at in the future), not a
        # sticky flag — so it self-clears once the wake fires or its time passes.
        import time as _t
        _nxt = getattr(session, "next_wake_at", None) if session else None
        is_looping = bool(_nxt and _nxt > _t.time())
        if is_sleeping:
            prefix = "⏸ "
        elif is_questioning:
            prefix = "❓"
        elif is_looping:
            prefix = "↻ "  # self-paced loop (scheduled wake / cron armed)
        elif is_active:
            prefix = "◉ " if is_working else "◇ "
        else:
            prefix = "• " if is_working else "◇ "
        # Show backend for non-claude sessions
        backend = self.view.settings().get("claude_backend")
        if backend:
            # Prefer the registry's abbrev (covers custom providers); fall back
            # to the static constants map, then to a 2-char upper of the name.
            try:
                from . import backends as _backends
                abbr = _backends.get(backend).abbrev or BACKEND_ABBREV.get(backend, backend[:2].upper())
            except Exception:
                abbr = BACKEND_ABBREV.get(backend, backend[:2].upper())
            name = f"{abbr}> {name}"
        # Truncate to keep tab bar usable
        if len(name) > 24:
            name = name[:23] + "…"
        self.view.set_name(f"{prefix}{name}")

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

        old_size = self.view.size()
        self.view.set_read_only(False)
        self.view.run_command("claude_replace", {"start": start, "end": end, "text": text})
        self.view.set_read_only(True)
        # Calculate actual new end from view size delta (more reliable than len())
        new_size = self.view.size()
        return end + (new_size - old_size)

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

        # Check if we should auto-scroll
        if not force:
            sel = self.view.sel()
            if sel:
                cursor = sel[0].end()
                size = self.view.size()
                # Only auto-scroll if cursor is near end (within 200 chars)
                if size > 200 and cursor < size - 200:
                    return  # User is viewing history, don't auto-scroll

        # Scroll to end without moving cursor
        end = self.view.size()
        self.view.show(end, keep_to_left=False, animate=False)

    # --- Inline Input ---

    def enter_input_mode(self) -> None:
        """Enter input mode - show prompt marker and allow typing."""
        if not self.view or not self.view.is_valid():
            return
        if self._input_mode:
            return

        # Check session-level working flag (authoritative busy state)
        from . import claude_code
        session = claude_code.get_session_for_view(self.view)
        if session and session.working:
            return

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
                is_context_line = line.startswith(CONTEXT_PREFIX) and not has_pending_context
                # Background task hint lines from previous input mode
                is_bg_hint = line.strip().startswith((BACKGROUND_PREFIX, '✔ ', '✘ '))
                if is_input_marker or is_context_line or is_bg_hint:
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

        # Add background task hints
        bg_tools = self.active_background_tools()
        if bg_tools:
            for bt in bg_tools:
                detail = self._format_tool_detail(bt)
                self.view.run_command("append", {"characters": f"  {BACKGROUND_PREFIX}{bt.name}{detail}\n"})

        # Add context line if any
        from . import claude_code
        session = claude_code.get_session_for_view(self.view)
        if session and session.pending_context:
            names = [item.name for item in session.pending_context]
            ctx_line = f"{CONTEXT_PREFIX}{', '.join(names)}\n"
            # print(f"[Claude] enter_input_mode: adding context line: {repr(ctx_line)}")
            self.view.run_command("append", {"characters": ctx_line})
        # else:
            # print(f"[Claude] enter_input_mode: no pending context to add, session={session is not None}")

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

        # Pin the permission-mode banner at the fresh input area (non-baseline modes only)
        if session:
            session._update_permission_banner(show=True)
            session._update_wakeup_banner(show=True)

        # print(f"[Claude] enter_input_mode: COMPLETED, view_size={self.view.size()}, _input_mode={self._input_mode}, final_content={repr(self.view.substr(sublime.Region(0, self.view.size())))}")

    def exit_input_mode(self, keep_text: bool = False) -> str:
        """Exit input mode and return the input text."""
        if not self.view or not self._input_mode:
            return ""

        # Drop the permission-mode / wakeup banners before the input area is removed.
        from . import claude_code
        _sess = claude_code.get_session_for_view(self.view)
        if _sess:
            _sess._update_permission_banner(show=False)
            _sess._update_wakeup_banner(show=False)

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
            is_context_line = line.startswith(CONTEXT_PREFIX)
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
            # Save current input text, re-render input area with new context
            input_text = self.get_input_text()
            # Clear draft to prevent on_activated from re-filling it
            from . import claude_code
            session = claude_code.get_session_for_view(self.view)
            if session:
                session.draft_prompt = ""
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
        text = f"\n{CONTEXT_PREFIX}{', '.join(names)} ({len(names)} file{'s' if len(names) > 1 else ''})\n"
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
                is_context_line = line.startswith(CONTEXT_PREFIX)
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
        prev_goal = None
        if self.current:
            # Ensure previous conversation is marked as done
            if self.current.working:
                self.current.working = False
                self._render_current()
            self.conversations.append(self.current)
            # Cap conversation history (memory bound). Print a hint when truncating
            # so users know history is being dropped (was previously silent).
            HISTORY_CAP = 20
            if len(self.conversations) > HISTORY_CAP:
                dropped = len(self.conversations) - HISTORY_CAP
                print(f"[Claude] conversation history capped: dropped {dropped} oldest turn(s)")
                self.conversations = self.conversations[-HISTORY_CAP:]
            # Carry only still-open todos (drop completed/cancelled leftovers).
            if not self.current.todos_all_done:
                prev_todos = _open_todos(self.current.todos)
            if _goal_is_open(self.current.goal):
                prev_goal = self.current.goal

        # Start new
        self.current = Conversation(
            prompt=text, todos=prev_todos, goal=prev_goal,
            context_names=context_names or [])
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
            line = f"{prefix}◎ {indented} ▶\n  {CONTEXT_PREFIX}{context_str}\n"
            # print(f"[Claude] prompt: writing with context: {repr(line)}")
        else:
            line = f"{prefix}◎ {indented} ▶\n"
            # print(f"[Claude] prompt: writing without context: {repr(line)}")
        end = self._write(line)
        self.current.region = (start, end)
        # Track with Sublime region so it auto-adjusts when view content shifts
        self.view.add_regions(
            "claude_conversation",
            [sublime.Region(start, end)],
            "", "", sublime.HIDDEN,
        )
        self._scroll_to_end()

    def tool(self, name: str, tool_input: dict = None, tool_id: str = None, background: bool = False) -> None:
        """Add a pending tool.

        Same tool_id → upsert (ACP re-sends tool_call then tool_call_update
        with richer input; appending would leave a second ☐ that never gets
        tool_result).
        """
        if not self.current:
            return

        tool_input = tool_input or {}
        status = BACKGROUND if background else PENDING
        # Upsert by id when still open — avoids duplicate ☐ rows.
        if tool_id:
            existing = self._find_pending_or_background_by_id(tool_id)
            if existing is not None and existing.status in (PENDING, BACKGROUND):
                existing.name = name
                if tool_input:
                    existing.tool_input = tool_input
                if background:
                    existing.status = BACKGROUND
                # Fall through to TodoWrite/Task* side effects below, then re-render.
                tool_call = existing
            else:
                tool_call = ToolCall(
                    name=name, tool_input=tool_input, status=status, id=tool_id)
                self.current.events.append(tool_call)
        else:
            tool_call = ToolCall(
                name=name, tool_input=tool_input, status=status, id=tool_id)
            self.current.events.append(tool_call)

        # Capture TodoWrite state (Anthropic stock tool — full snapshot per call)
        if name == "TodoWrite" and "todos" in tool_input:
            raw = tool_input.get("todos") or []
            # Keep closed items out of the live list so they don't reappear
            # next round as fake "pending" (cancelled used to count as pending).
            self.current.todos = [
                TodoItem(
                    content=t.get("content", "") or t.get("activeForm", "") or "",
                    status=_todo_status_norm(t.get("status", "pending")),
                    id=str(t.get("id") or ""),
                )
                for t in raw
                if isinstance(t, dict)
            ]
            self.current.todos = _open_todos(self.current.todos)
            self.current.todos_all_done = not self.current.todos
        # Task* variants (this harness's split API — incremental ops)
        elif name == "TaskCreate":
            subject = tool_input.get("subject", "") or tool_input.get("description", "")
            if subject:
                # Optimistic add; id resolved when TaskList runs or via tool_result.
                self.current.todos.append(TodoItem(content=subject, status="pending"))
                self.current.todos_all_done = False
        elif name == "TaskUpdate":
            tid = tool_input.get("taskId", "")
            new_status = tool_input.get("status")
            new_subject = tool_input.get("subject")
            if tid:
                st = _todo_status_norm(new_status) if new_status else ""
                if st in ("deleted", "cancelled", "canceled"):
                    # Drop from list — cancelled is not "pending with ○".
                    self.current.todos = [
                        t for t in self.current.todos if t.id != tid]
                    if not _open_todos(self.current.todos):
                        self.current.todos_all_done = True
                else:
                    for todo in self.current.todos:
                        if todo.id == tid:
                            if new_status:
                                todo.status = st or new_status
                            if new_subject:
                                todo.content = new_subject
                            break
                    if not _open_todos(self.current.todos):
                        self.current.todos_all_done = True
        elif name == "update_goal":
            # Single sticky goal — not a multi-item Task list.
            blocked = (tool_input.get("blocked_reason") or "").strip()
            msg = (tool_input.get("message") or "").strip()
            if blocked:
                self.current.goal = GoalState(
                    status="blocked", message=msg, blocked_reason=blocked)
            elif tool_input.get("completed") is True:
                self.current.goal = GoalState(
                    status="completed", message=msg, blocked_reason="")
            else:
                prev = self.current.goal
                self.current.goal = GoalState(
                    status="active",
                    message=msg or (prev.message if prev else ""),
                    blocked_reason="",
                )

        self._render_current()

    def _find_pending_or_background_by_id(self, tool_id: str) -> Optional[ToolCall]:
        """Find a ToolCall by id across ALL conversations including current.

        Prefer still-open (pending/background) over a same-id already-done row
        left by a prior duplicate emit.
        """
        if not tool_id:
            return None

        def _scan(events):
            done = None
            for event in events:
                if not isinstance(event, ToolCall) or event.id != tool_id:
                    continue
                if event.status in (PENDING, BACKGROUND):
                    return event
                done = event
            return done

        if self.current:
            hit = _scan(self.current.events)
            if hit is not None:
                return hit
        for conv in self.conversations:
            hit = _scan(conv.events)
            if hit is not None:
                return hit
        return None

    def find_tool_by_id(self, tool_id: str) -> Optional[ToolCall]:
        return self._find_pending_or_background_by_id(tool_id)

    def active_background_tools(self) -> list:
        """Get all currently running background tools."""
        result = []
        for conv in self.conversations:
            for event in conv.events:
                if isinstance(event, ToolCall) and event.status == BACKGROUND:
                    result.append(event)
        if self.current:
            for event in self.current.events:
                if isinstance(event, ToolCall) and event.status == BACKGROUND:
                    result.append(event)
        return result

    def _is_in_current(self, target: ToolCall) -> bool:
        """Check if a tool call belongs to the current conversation."""
        if not self.current:
            return False
        return any(e is target for e in self.current.events)

    def _patch_tool_symbol(self, target: ToolCall, old_status: str) -> None:
        """Patch a tool's symbol in-place in the view (for previous conversations).

        Uses tool_input snippets (command/file_path/etc.) as a disambiguator
        so N concurrent same-name tools each find their own line — without
        this, all matches collapse to whichever line was rendered first.
        """
        if not self.view:
            return
        old_sym = self.SYMBOLS.get(old_status, "☐")
        new_sym = self.SYMBOLS.get(target.status, "☐")
        if old_sym == new_sym:
            return
        content = self.view.substr(sublime.Region(0, self.view.size()))
        import re
        # Build a disambiguating snippet from tool_input. First-line only,
        # capped to keep regex sane and avoid matching across result lines.
        snippet = ""
        for key in ("command", "file_path", "pattern", "url", "task_id", "description"):
            val = target.tool_input.get(key) if isinstance(target.tool_input, dict) else None
            if isinstance(val, str) and val.strip():
                snippet = val.split("\n", 1)[0][:120]
                break
        prefix = f"  {old_sym} {target.name}"
        if snippet:
            # Look for "  ⚙ Bash: {cmd-snippet}" — `re.escape` handles regex metas in cmd
            pattern = re.escape(prefix) + r"[^\n]*?" + re.escape(snippet)
        else:
            pattern = re.escape(prefix)
        m = re.search(pattern, content)
        if m is None and snippet:
            # Fallback: tool_input changed or detail formatter renamed — try plain prefix
            m = re.search(re.escape(prefix), content)
        if m is not None:
            self._replace(m.start() + 2, m.start() + 2 + len(old_sym), new_sym)

    def remove_tool(self, target: ToolCall) -> None:
        """Drop a tool entirely — from its conversation's events and, for an
        already-rendered past conversation, by erasing its line from the view.
        Used for background tools aborted by a reconnect or finishing with no
        surfaced result: leaving a ✘/✓ line is just noise."""
        in_current = False
        convs = list(self.conversations)
        if self.current is not None:
            convs.append(self.current)
        for conv in convs:
            for i, e in enumerate(conv.events):
                if e is target:  # identity, not dataclass __eq__
                    del conv.events[i]
                    in_current = (conv is self.current)
                    break
            else:
                continue
            break
        if in_current and self.current is not None:
            self._render_current()
            return
        if not self.view:
            return
        content = self.view.substr(sublime.Region(0, self.view.size()))
        import re
        sym = self.SYMBOLS.get(target.status, self.SYMBOLS.get(BACKGROUND, "⚙"))
        snippet = ""
        for key in ("command", "file_path", "pattern", "url", "task_id", "description"):
            val = target.tool_input.get(key) if isinstance(target.tool_input, dict) else None
            if isinstance(val, str) and val.strip():
                snippet = val.split("\n", 1)[0][:120]
                break
        prefix = f"  {sym} {target.name}"
        pattern = re.escape(prefix) + (r"[^\n]*?" + re.escape(snippet) if snippet else "")
        m = re.search(pattern, content)
        if m is None and snippet:
            m = re.search(re.escape(prefix), content)
        if m is None:
            return
        line_region = self.view.line(m.start())
        end = min(line_region.end() + 1, self.view.size())  # include trailing newline
        self._replace(line_region.begin(), end, "")

    def tool_done(self, name: str, result: str = None, tool_id: str = None) -> None:
        """Mark tool as done. Prefer tool_id match, fall back to name+PENDING.

        If duplicate open rows share the same id (legacy re-emits), close all of them.
        """
        targets = []
        if tool_id and self.current:
            for event in self.current.events:
                if (isinstance(event, ToolCall) and event.id == tool_id
                        and event.status in (PENDING, BACKGROUND)):
                    targets.append(event)
        if not targets:
            target = self._find_pending_or_background_by_id(tool_id)
            if target is None and self.current:
                for event in reversed(self.current.events):
                    if (isinstance(event, ToolCall) and event.name == name
                            and event.status == PENDING):
                        target = event
                        break
            if target is not None:
                targets = [target]
        if not targets:
            if self.current:
                self.current.events.append(ToolCall(
                    name=name, tool_input={}, status=DONE, result=result, id=tool_id))
                self._render_current()
            return
        primary = targets[0]
        for target in targets:
            old_status = target.status
            target.status = DONE
            target.result = result
            # Media tools: stash absolute path for formatters + inline phantoms.
            if target.name in MEDIA_TOOLS and result:
                path = extract_media_path(result, target.tool_input)
                if path:
                    if not isinstance(target.tool_input, dict):
                        target.tool_input = {}
                    target.tool_input["_media_path"] = path
                    target.tool_input.setdefault("path", path)
            if not self._is_in_current(target):
                self._patch_tool_symbol(target, old_status)
        # Pull task state from Task* results
        if name in ("TaskList", "TaskCreate", "TaskGet") and result and self.current is not None:
            self._sync_todos_from_task_result(name, primary, result)
        if any(self._is_in_current(t) for t in targets):
            self._render_current()

    def _sync_todos_from_task_result(self, name: str, tool: "ToolCall", result: str) -> None:
        """Parse Task* result text and merge into current.todos.

        Strategy: try JSON first (most robust); fall back to regex sniffing
        of `id:` / `subject:` / `status:` field markers Claude Code emits.
        """
        import re, json
        parsed_items = []
        # Try JSON anywhere in the result
        for match in re.finditer(r'\{[^{}]*"(?:id|subject|status)"[^{}]*\}', result):
            try:
                obj = json.loads(match.group(0))
                if isinstance(obj, dict) and ("id" in obj or "subject" in obj):
                    parsed_items.append(obj)
            except Exception:
                pass
        if not parsed_items:
            for line in result.splitlines():
                # The plugin's own Task* tools print "#<id> [<status>] <subject>"
                # (e.g. "#19 [completed] Fix …") — match that first.
                m = re.match(r'\s*#(\d+)\s+\[(\w+)\]\s+(.+)', line)
                if m:
                    parsed_items.append({
                        "id": m.group(1), "status": m.group(2),
                        "subject": m.group(3).strip(),
                    })
                    continue
                # Create/update results: "Task #4 created successfully: <subject>",
                # "Updated task #4 status". Capture the id (+ subject after a colon)
                # so the new todo gets an id and later TaskUpdate(status) can match.
                m_t = re.search(r'[Tt]ask\s+#(\d+)', line)
                if m_t:
                    sub_m = re.search(r':\s*(.+?)\s*$', line)
                    parsed_items.append({
                        "id": m_t.group(1),
                        "subject": sub_m.group(1).strip() if sub_m else "",
                        "status": "pending",
                    })
                    continue
                # Fallback: "id: 1, subject: ..., status: pending" field markers.
                m_id = re.search(r'\b(?:id|taskId)["\s:=]+([^\s,"\}]+)', line)
                m_sub = re.search(r'\bsubject["\s:=]+([^,"\}]+)', line)
                m_st = re.search(r'\bstatus["\s:=]+([^\s,"\}]+)', line)
                if m_id or m_sub:
                    parsed_items.append({
                        "id": m_id.group(1).strip() if m_id else "",
                        "subject": (m_sub.group(1).strip() if m_sub else ""),
                        "status": (m_st.group(1).strip() if m_st else "pending"),
                    })
        if not parsed_items:
            return

        if name == "TaskList":
            # Authoritative snapshot — open work only.
            self.current.todos = _open_todos([
                TodoItem(
                    content=it.get("subject") or it.get("content") or "",
                    status=_todo_status_norm(it.get("status") or "pending"),
                    id=str(it.get("id") or ""),
                )
                # Require real text — an id-only parse ("+" etc.) is garbage and
                # would render as a blank ○.
                for it in parsed_items if (it.get("subject") or it.get("content"))
            ])
            self.current.todos_all_done = not self.current.todos
        elif name == "TaskCreate":
            # Backfill id on the todo we just appended (the result is for it), so a
            # later TaskUpdate(taskId=…) can match and flip its rendered status.
            new = parsed_items[-1]
            new_id = str(new.get("id") or "")
            new_subject = (new.get("subject") or tool.tool_input.get("subject") or "").strip()
            if new_id:
                idless = [t for t in self.current.todos if not t.id]
                target = next((t for t in idless
                               if new_subject and t.content.strip() == new_subject), None)
                if target is None and idless:
                    target = idless[-1]  # most-recently appended
                if target is not None:
                    target.id = new_id
                    if new_subject:
                        target.content = new_subject
        elif name == "TaskGet":
            new = parsed_items[-1]
            tid = str(new.get("id") or "")
            if tid:
                for todo in self.current.todos:
                    if todo.id == tid:
                        if new.get("subject"):
                            todo.content = new["subject"]
                        if new.get("status"):
                            todo.status = new["status"]
                        break

    def tool_error(self, name: str, result: str = None, tool_id: str = None) -> None:
        """Mark tool as error. Prefer tool_id match, fall back to name+PENDING."""
        target = self._find_pending_or_background_by_id(tool_id)
        if target is None and self.current:
            for event in reversed(self.current.events):
                if isinstance(event, ToolCall) and event.name == name and event.status == PENDING:
                    target = event
                    break
        if target is None:
            if self.current:
                self.current.events.append(ToolCall(name=name, tool_input={}, status=ERROR, result=result, id=tool_id))
                self._render_current()
            return
        old_status = target.status
        target.status = ERROR
        target.result = result
        if self._is_in_current(target):
            self._render_current()
        else:
            self._patch_tool_symbol(target, old_status)

    def text(self, content: str) -> None:
        """Add response text."""
        if not self.current:
            return

        # Merge with previous text event to avoid per-token line breaks
        if self.current.events and isinstance(self.current.events[-1], str):
            self.current.events[-1] += content
        else:
            self.current.events.append(content)
        self._render_current()

    def meta(self, duration: float, cost: float = None, usage: dict = None) -> None:
        """Set completion meta - marks conversation as done."""
        if not self.current:
            return

        self.current.duration = duration
        self.current.usage = usage
        self.current.working = False
        self._render_current()

    def interrupted(self) -> None:
        """Show interrupted indicator."""
        if not self.current:
            return
        self.current.working = False
        # Mark any pending/background tools as error
        for event in self.current.events:
            if isinstance(event, ToolCall) and event.status in (PENDING, BACKGROUND):
                event.status = ERROR
        # Clear any pending permission prompt
        if self.pending_permission:
            self._remove_permission_block()
            self.pending_permission = None
        # Clear any pending plan approval
        if self.pending_plan:
            self._clear_plan_approval()
            self.pending_plan = None
        # Clear any pending question
        if self.pending_question:
            callback = self.pending_question.callback
            self._clear_question()
            self.pending_question = None
            if callback:
                callback(None)
        # Append interrupted text
        self.current.events.append("\n\n*[interrupted]*\n")
        self._render_current()

    def clear(self) -> None:
        """Clear all output (can undo with Cmd+Z).

        Preserves and re-displays "supportive" UI elements that reflect current
        state rather than chat history: active background tools and the open
        todo list. Pending permission/plan/question UIs are turn-modal and
        intentionally cleared.
        """
        # Remember if we were in input mode
        was_input_mode = self._input_mode
        # Check if agent is currently working (before we clear state)
        was_working = self.current and self.current.working

        # Snapshot supportive state BEFORE wiping conversations/current.
        # Background tools we want to keep visible (their bash subprocesses are
        # still running in the bridge); todos we carry forward like prompt() does.
        carry_bg_tools = list(self.active_background_tools())
        carry_todos = []
        carry_goal = None
        if self.current and self.current.todos and not self.current.todos_all_done:
            carry_todos = _open_todos(self.current.todos)
        else:
            # No open todos on current; look at the most recent conversation
            for conv in reversed(self.conversations):
                if conv.todos and not conv.todos_all_done:
                    carry_todos = _open_todos(conv.todos)
                    break
        if self.current and _goal_is_open(self.current.goal):
            carry_goal = self.current.goal
        else:
            for conv in reversed(self.conversations):
                if _goal_is_open(conv.goal):
                    carry_goal = conv.goal
                    break

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
        self.pending_plan = None
        self.pending_question = None
        self.auto_allow_tools.clear()
        self._pending_context_region = (0, 0)
        self._input_mode = False
        self._input_start = 0
        self._input_area_start = 0
        # Clean up any tracked permission region
        if self.view:
            self.view.erase_regions("claude_permission_block")

        # If agent was working, create a stub conversation to receive further output
        # This prevents output from being silently discarded after clear.
        # Attach the carried bg tools + todos so they remain visible while the
        # turn keeps going.
        if was_working:
            self.current = Conversation(prompt="(continued)", working=True)
            self.current.region = (0, 0)  # Will be set on first render
            self.current.events.extend(carry_bg_tools)
            self.current.todos = carry_todos
            self.current.goal = carry_goal
            self._update_title()  # Show working indicator
            return  # Don't enter input mode while working

        # Idle case: build a zero-prompt carry-forward conversation so the
        # supportive UI (bg-tool entries, todos, goal) stays visible. _do_render
        # skips the prompt header when prompt is empty.
        if carry_bg_tools or carry_todos or carry_goal:
            carry = Conversation(prompt="", working=False)
            carry.events = list(carry_bg_tools)
            carry.todos = carry_todos
            carry.todos_all_done = False
            carry.goal = carry_goal
            carry.region = (0, 0)
            self.current = carry
            self._render_current()

        # Re-enter input mode if we were in it (this also adds bg-tool hint
        # lines and the pending-context line via session lookup)
        if was_input_mode:
            self.enter_input_mode()

    def undo_clear(self) -> None:
        """Restore content from last clear.

        Note: this recovers view text only — internal state (conversations,
        current) is NOT restored. If clear() created a carry-forward
        conversation (idle path) for supportive UI, we drop it here so the
        restored view text isn't shadowed by a stale carry record.
        """
        if self._cleared_content and self.view and self.view.is_valid():
            # If current is a zero-prompt carry, drop it — its view region
            # would now collide with the restored text
            if self.current is not None and not self.current.prompt and not self.current.working:
                self.current = None
                if self.view:
                    self.view.erase_regions("claude_conversation")
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
                if isinstance(event, ToolCall) and event.status in (PENDING, BACKGROUND):
                    event.status = ERROR
                    had_pending = True
            if had_pending:
                self.current.events.append("\n\n*[session reconnected]*\n")
                self._render_current()

        # Patch stale background-tool symbols in the view text. After Sublime
        # restart, ToolCall objects for old turns are gone, but the view text
        # still shows ⚙ for bg tasks whose subprocesses died with the previous
        # bridge. Convert them to ✘ so they don't look in-progress forever.
        if self.view:
            bg_sym = self.SYMBOLS["background"]
            err_sym = self.SYMBOLS["error"]
            content = self.view.substr(sublime.Region(0, self.view.size()))
            marker = f"  {bg_sym} "
            replacement = f"  {err_sym} "
            idx = 0
            edits: list = []
            while True:
                pos = content.find(marker, idx)
                if pos < 0:
                    break
                # Only patch the symbol char itself
                sym_start = pos + 2
                sym_end = sym_start + len(bg_sym)
                edits.append((sym_start, sym_end))
                idx = pos + len(marker)
            # Apply right-to-left so earlier offsets stay valid
            if edits:
                self.view.set_read_only(False)
                for sym_start, sym_end in reversed(edits):
                    self.view.run_command("claude_replace", {"start": sym_start, "end": sym_end, "text": err_sym})
                self.view.set_read_only(True)

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
        if regions and regions[0].size() > 0:
            region = regions[0]
            self._replace(region.begin(), region.end(), "")
        else:
            # Tracked region lost (zero-width or missing) — fallback:
            # permission block is everything after conversation region
            if self.current:
                conv_end = self.current.region[1]
                view_size = self.view.size()
                if view_size > conv_end:
                    self._replace(conv_end, view_size, "")
        self.view.erase_regions("claude_permission_block")

    # --- Permission UI ---

    def clear_stale_permission(self, current_pid: int) -> None:
        """Clear permission UI if it's for an older request (bridge moved on)."""
        if not self.pending_permission:
            return

        # If pending permission is for an older pid, it's stale
        if self.pending_permission.id < current_pid:
            self._clear_permission()
            self.pending_permission = None
            self._permission_queue.clear()  # Also clear queue - they're all stale

    def clear_all_permissions(self) -> None:
        """Clear all pending permissions (called when query finishes)."""
        if self.pending_permission:
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
                callback(PERM_ALLOW)
                return

        # Check if user chose "allow for 30s" recently
        now = time.time()
        if self._last_allowed_tool == tool and (now - self._last_allowed_time) < 30:
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

        # Format tool details and display name
        detail = ""
        display_tool = tool
        if tool == "Bash" and "command" in tool_input:
            cmd = tool_input["command"]
            if len(cmd) > 80:
                cmd = cmd[:80] + "..."
            detail = cmd
        elif tool in ("Read", "Edit", "Write"):
            detail = tool_input.get("file_path") or tool_input.get("description") or ""
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
            # Preview the pattern that _make_auto_allow_pattern would create
            pattern = self._make_auto_allow_pattern(tool, tool_input)
            if pattern != tool and "(" in pattern:
                # Extract the specifier part: "Bash(git:*)" → "git:*"
                always_hint = f" `{pattern[pattern.index('(')+1:-1]}`"
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
        clear_pending_block(
            self.view,
            block_region_key="claude_permission_block",
            button_prefix="claude_btn_",
            button_keys=self.pending_permission.button_regions,
            fallback_region_end=self.current.region[1] if self.current else None,
        )
        # Update conversation region end to account for removed permission block
        if self.current:
            self.current.region = (self.current.region[0], self.view.size())
        # Don't clear pending_permission - keep it to detect rapid same-tool requests
        # It will be overwritten when a different tool request comes in

    @staticmethod
    def _extract_bash_subcommands(command: str) -> list:
        """Extract subcommands from a compound bash command.

        Splits on &&, ||, ;, |, |&, &, and newlines.
        Strips process wrappers (timeout, time, nice, nohup, stdbuf).
        Strips bare xargs. Skips env var assignments.
        Returns list of (executable_name, full_subcommand) tuples.
        """
        import re
        parts = re.split(r'\s*(?:&&|\|\||\|&|[;&|\n])\s*', command)
        wrappers = {"timeout", "time", "nice", "nohup", "stdbuf"}
        result = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            words = part.split()
            idx = 0
            # Skip env var assignments
            while idx < len(words) and '=' in words[idx] and not words[idx].startswith('-'):
                idx += 1
            if idx >= len(words):
                continue
            # Strip process wrappers
            while idx < len(words) and words[idx] in wrappers:
                idx += 1
                # Skip wrapper's numeric/flag args
                while idx < len(words) and (words[idx].startswith('-') or words[idx].replace('.', '').isdigit()):
                    idx += 1
            if idx >= len(words):
                continue
            # Strip bare xargs (no flags)
            if words[idx] == "xargs" and (idx + 1 >= len(words) or not words[idx + 1].startswith('-')):
                idx += 1
            if idx >= len(words):
                continue
            word = words[idx]
            if '/' in word:
                word = word.split('/')[-1]
            result.append((word, part))
        return result

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
                trivial = {"cd", "pushd", "popd", "export", "set", "unset", "source", ".", "true", "false"}
                subcmds = self._extract_bash_subcommands(command)
                best = None
                for word, _ in subcmds:
                    if word not in trivial:
                        best = word
                        break
                if not best and subcmds:
                    best = subcmds[0][0]
                if best:
                    return f"Bash({best}:*)"
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

        # Bash command matching — each subcommand must match independently
        if tool == "Bash":
            command = tool_input.get("command", "")
            if not command:
                return False
            if specifier.endswith(":*"):
                prefix = specifier[:-2]
                subcmds = self._extract_bash_subcommands(command)
                return any(word.startswith(prefix) for word, _ in subcmds)
            # Exact match
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

        # Handle "allow all" - save to project settings and remember for this session.
        # Keep PERM_ALLOW_ALL on the callback so ACP bridges can map to allow_always.
        if response == PERM_ALLOW_ALL:
            pattern = self._make_auto_allow_pattern(tool, tool_input)
            self.auto_allow_tools.add(pattern)
            self._save_auto_allowed_tool(pattern)

        # Handle "allow 30s" - set timed auto-allow (still reports as a plain allow)
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

    def _apply_output_settings(self) -> None:
        if not self.view:
            return
        s = sublime.load_settings("ClaudeOutput.sublime-settings")
        for key in ("font_size", "line_numbers", "gutter", "word_wrap", "margin",
                    "draw_indent_guides", "highlight_line", "fold_buttons", "fade_fold_buttons"):
            val = s.get(key)
            if val is not None:
                self.view.settings().set(key, val)

    def _load_persisted_auto_allow(self) -> set:
        """Load autoAllowedMcpTools from project settings at session start."""
        try:
            from .settings import load_project_settings
            folders = self.window.folders()
            project_dir = folders[0] if folders else None
            settings = load_project_settings(project_dir)
            return set(settings.get("autoAllowedMcpTools", []))
        except Exception:
            return set()

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
                    perm.callback(PERM_ALLOW)
                    auto_allowed = True
                    break
            if auto_allowed:
                continue

            now = time.time()
            if self._last_allowed_tool == perm.tool and (now - self._last_allowed_time) < 30:
                perm.callback(PERM_ALLOW)
                continue

            # Show this one
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

    # --- Plan Approval UI ---

    def plan_approval_request(self, plan_id: int, plan_file: str,
                               allowed_prompts: list, callback: Callable[[str], None]) -> None:
        """Show an inline plan approval block."""
        self.show(focus=False)

        if self.view:
            self.view.settings().set("claude_input_mode", False)

        self.pending_plan = PlanApproval(
            id=plan_id,
            plan_file=plan_file,
            allowed_prompts=allowed_prompts,
            callback=callback,
        )
        self._render_plan_approval()
        self._scroll_to_end()

    def _render_plan_approval(self) -> None:
        """Render the plan approval block in the view."""
        if not self.pending_plan or not self.view:
            return

        plan = self.pending_plan
        lines = ["\n"]

        # Header
        lines.append("  ⚙ Plan complete — approve to start implementation\n")

        # Plan file
        if plan.plan_file:
            import os
            basename = os.path.basename(plan.plan_file)
            lines.append(f"    plan: {basename}\n")

        # Allowed prompts summary
        if plan.allowed_prompts:
            lines.append(f"    permissions: {len(plan.allowed_prompts)}\n")
            for p in plan.allowed_prompts[:3]:
                tool = p.get("tool", "?")
                prompt = p.get("prompt", "")
                lines.append(f"      • {tool}: {prompt}\n")
            if len(plan.allowed_prompts) > 3:
                lines.append(f"      ... and {len(plan.allowed_prompts) - 3} more\n")

        # Buttons
        lines.append("    ")
        text_before_buttons = "".join(lines)

        btn_y = "[Y] Approve"
        btn_n = "[N] Reject"
        btn_v = "[V] View Plan"

        lines.append(btn_y)
        lines.append("  ")
        lines.append(btn_n)
        lines.append("  ")
        lines.append(btn_v)
        lines.append("\n")

        text = "".join(lines)

        # Write to view
        start = self.view.size()
        end = self._write(text)
        plan.region = (start, end)

        # Track region
        self.view.add_regions(
            "claude_plan_block",
            [sublime.Region(start, end)],
            "", "", sublime.HIDDEN,
        )

        # Button regions
        btn_start = start + len(text_before_buttons)
        plan.button_regions[PLAN_APPROVE] = (btn_start, btn_start + len(btn_y))
        btn_start += len(btn_y) + 2
        plan.button_regions[PLAN_REJECT] = (btn_start, btn_start + len(btn_n))
        btn_start += len(btn_n) + 2
        plan.button_regions[PLAN_VIEW] = (btn_start, btn_start + len(btn_v))

        # Highlight buttons
        scope_map = {
            PLAN_APPROVE: "claude.permission.button.allow",
            PLAN_REJECT: "claude.permission.button.deny",
            PLAN_VIEW: "claude.permission.button.allow_session",
        }
        for btn_type, (bs, be) in plan.button_regions.items():
            self.view.add_regions(
                f"claude_plan_btn_{btn_type}",
                [sublime.Region(bs, be)],
                scope_map.get(btn_type, ""),
                "", sublime.DRAW_NO_OUTLINE,
            )

    def _clear_plan_approval(self) -> None:
        """Remove plan approval block from view."""
        if not self.pending_plan or not self.view:
            return
        clear_pending_block(
            self.view,
            block_region_key="claude_plan_block",
            button_prefix="claude_plan_btn_",
            button_keys=self.pending_plan.button_regions,
            fallback_region_end=self.current.region[1] if self.current else None,
        )

    def handle_plan_key(self, key: str) -> bool:
        """Handle Y/N key for plan approval. Returns True if handled."""
        if not self.pending_plan:
            return False
        if self.pending_plan.callback is None:
            return False

        plan = self.pending_plan
        key = key.lower()

        if key == "v":
            if plan.plan_file:
                view = sublime.active_window().open_file(plan.plan_file)
                def enable_wrap(v=view):
                    if v.is_loading():
                        sublime.set_timeout(lambda: enable_wrap(v), 100)
                        return
                    v.settings().set("word_wrap", True)
                enable_wrap()
            return True

        if key == "y":
            response = PLAN_APPROVE
        elif key == "n":
            response = PLAN_REJECT
        else:
            return False

        callback = plan.callback
        plan.callback = None
        self._clear_plan_approval()
        self.pending_plan = None
        self._move_cursor_to_end()
        callback(response)
        return True

    # --- Question UI ---

    def question_request(self, qid: int, questions: list, callback: Callable) -> None:
        """Show an inline question block."""
        self.show(focus=False)

        if self.view:
            self.view.settings().set("claude_input_mode", False)

        self.pending_question = QuestionRequest(
            qid=qid,
            questions=questions,
            callback=callback,
        )
        self._render_question()
        self._scroll_to_end(force=True)

    def _render_question(self) -> None:
        """Render the current question inline."""
        if not self.pending_question or not self.view:
            return

        q_req = self.pending_question
        if q_req.current_idx >= len(q_req.questions):
            return

        q = q_req.questions[q_req.current_idx]
        question_text = q.get("question", "")
        options = q.get("options", [])
        multi = q.get("multiSelect", False)

        lines = ["\n"]
        if multi:
            lines.append(f"  ❓ {question_text} (Enter to confirm)\n")
        else:
            lines.append(f"  ❓ {question_text}\n")

        # Numbered options
        for i, opt in enumerate(options):
            label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
            desc = opt.get("description", "") if isinstance(opt, dict) else ""
            num = i + 1
            if multi:
                check = "✓" if i in q_req.selected else " "
                line = f"    [{num}] {check} {label}"
            else:
                line = f"    [{num}] {label}"
            if desc:
                line += f" — {desc}"
            lines.append(line + "\n")

        # Other + confirm buttons
        if multi:
            lines.append(f"    [O] Other...  [⏎] Confirm\n")
        else:
            lines.append(f"    [O] Other...\n")

        text = "".join(lines)

        # Write to view
        start = self.view.size()
        end = self._write(text)
        q_req.region = (start, end)

        # Track region
        self.view.add_regions(
            "claude_question_block",
            [sublime.Region(start, end)],
            "", "", sublime.HIDDEN,
        )

        # Highlight option keys [1], [2], ..., [O], [⏎]
        import re
        key_regions = []
        for m in re.finditer(r'\[\d+\]|\[O\]|\[⏎\]', text):
            key_regions.append(sublime.Region(start + m.start(), start + m.end()))
        if key_regions:
            self.view.add_regions(
                "claude_question_keys",
                key_regions,
                "claude.permission.button.allow",
                "", sublime.DRAW_NO_OUTLINE,
            )

    def _clear_question(self, summary: str = "") -> None:
        """Remove question block. If summary, record the answer as a persistent
        ☑ decision line in the conversation (a real event) so it survives later
        re-renders — writing it as trailing UI got eaten by the next _do_render's
        end-extension when the AskUserQuestion tool completed."""
        if not self.pending_question or not self.view:
            return
        if summary and self.current is not None:
            # ☑ (not ❓) so an answered question reads as a recorded decision.
            # Appended to events → rendered inline (scoped claude.question.answered)
            # and preserved across renders, instead of living as eatable trailing UI.
            self.current.events.append(f"  ☑ {summary}\n")
        clear_pending_block(
            self.view,
            block_region_key="claude_question_block",
            button_prefix="claude_question_btn_",  # questions don't actually use this prefix
            button_keys={},  # questions don't have per-button hit-box regions
            fallback_region_end=(self.current.region[1] if self.current else None),
            replacement="",
            extra_region_keys=("claude_question_keys",),
        )
        if summary:
            self._render_current()

    def _advance_question(self) -> None:
        """Advance to next question or fire callback."""
        q_req = self.pending_question
        if not q_req:
            return

        q_req.current_idx += 1
        q_req.selected = set()  # Reset for next question

        if q_req.current_idx >= len(q_req.questions):
            # All done
            callback = q_req.callback
            answers = q_req.answers
            self.pending_question = None
            self._move_cursor_to_end()
            if callback:
                callback(answers)
        else:
            # Render next question
            self._render_question()
            self._scroll_to_end()

    def handle_question_key(self, key: str) -> bool:
        """Handle key press for question UI. Returns True if consumed."""
        if not self.pending_question:
            return False
        if self.pending_question.callback is None:
            return False

        q_req = self.pending_question
        q = q_req.questions[q_req.current_idx]
        options = q.get("options", [])
        multi = q.get("multiSelect", False)
        header = q.get("header", f"Q{q_req.current_idx + 1}")
        question_text = q.get("question", str(q_req.current_idx))
        key = key.lower()

        # Number keys 1-4
        if key in ("1", "2", "3", "4"):
            idx = int(key) - 1
            if idx >= len(options):
                return True  # Consumed but invalid

            opt = options[idx]
            label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)

            if multi:
                # Toggle selection
                if idx in q_req.selected:
                    q_req.selected.discard(idx)
                else:
                    q_req.selected.add(idx)
                # Re-render (erase + redraw)
                self._clear_question()
                self._render_question()
                self._scroll_to_end()
            else:
                # Single select - record and advance
                q_req.answers[question_text] = label
                self._clear_question(f"{header} → {label}")
                self._advance_question()
            return True

        # O key - other (custom input via inline input mode)
        if key == "o":
            self._question_enter_input_mode()
            return True

        # Enter - confirm multi-select
        if key == "enter":
            if multi:
                selected_labels = []
                for idx in sorted(q_req.selected):
                    opt = options[idx]
                    label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
                    selected_labels.append(label)
                # Keep a list so ACP (Grok) can send answers as [label, ...].
                q_req.answers[question_text] = selected_labels
                summary = ", ".join(selected_labels) if selected_labels else "(none)"
                self._clear_question(f"{header} → {summary}")
                self._advance_question()
                return True
            return False

        # Escape handled by interrupt flow
        return False

    def _question_enter_input_mode(self) -> None:
        """Enter inline input mode for free-text question answer."""
        if not self.view or not self.pending_question:
            return

        # Append input prompt after question block, prefixed with newline so it
        # sits on its own line and can be cleanly removed via named region.
        self.view.set_read_only(False)
        marker_text = "\n    ▸ "
        marker_start = self.view.size()
        self.view.run_command("append", {"characters": marker_text})
        marker_end = self.view.size()
        self._question_input_start = marker_end
        self._question_input_mode = True

        # Track the entire input line as a named region so we can erase it
        # robustly later regardless of how much text the user types.
        self.view.add_regions(
            "claude_question_input_marker",
            [sublime.Region(marker_start, marker_end)],
            "", "", sublime.HIDDEN,
        )

        # Set standard input mode so keyboard/selection handling works
        self._input_start = self._question_input_start
        self._input_mode = True
        self.view.settings().set("claude_input_mode", True)

        # Move cursor to input position
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(self._question_input_start, self._question_input_start))
        self.view.show(self._question_input_start)

    def submit_question_input(self) -> bool:
        """Submit free-text input for question. Returns True if handled."""
        if not getattr(self, '_question_input_mode', False):
            return False
        if not self.pending_question or not self.view:
            self._question_input_mode = False
            return False

        # Get typed text (region from marker_end to current view end)
        text = self.view.substr(sublime.Region(self._question_input_start, self.view.size())).strip()
        self._question_input_mode = False
        self._input_mode = False
        self.view.settings().set("claude_input_mode", False)

        # Remove the entire input line: marker region + any typed text after it.
        # Use the tracked region for robustness; fall back to position math.
        regions = self.view.get_regions("claude_question_input_marker")
        if regions:
            erase_start = regions[0].begin()
        else:
            erase_start = max(0, self._question_input_start - len("\n    ▸ "))
        self.view.set_read_only(False)
        self.view.run_command("claude_replace", {
            "start": erase_start,
            "end": self.view.size(),
            "text": "",
        })
        self.view.set_read_only(True)
        self.view.erase_regions("claude_question_input_marker")

        if text:
            q_req = self.pending_question
            q = q_req.questions[q_req.current_idx]
            header = q.get("header", f"Q{q_req.current_idx + 1}")
            q_req.answers[q.get("question", str(q_req.current_idx))] = text
            self._clear_question(f"{header} → {text}")
            self._advance_question()

        return True

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
        if not self.current or not self.current.working or not self.view:
            return
        self._spinner_frame += 1
        self._render_current(auto_scroll=False)
        # Periodically clear undo history to prevent memory bloat
        if self._spinner_frame % 50 == 0:
            try:
                self.view.clear_undo_stack()
            except AttributeError:
                pass  # Not available in older Sublime builds

    def _do_render(self) -> None:
        """Actually perform the render."""
        self._render_pending = False
        if not self.current or not self.view:
            return

        # Don't render while in input mode - it would corrupt the input region
        if self._input_mode:
            return

        # Read region from Sublime's tracked region (auto-adjusts when view shifts)
        view_size = self.view.size()
        tracked = self.view.get_regions("claude_conversation")
        if tracked and tracked[0].size() > 0:
            start, end = tracked[0].begin(), tracked[0].end()
        else:
            # Fallback to tuple
            start, end = self.current.region
        if start > view_size or end > view_size:
            # Region is invalid - recalculate from view content (skip recovery
            # for empty-prompt carry conversations — there's no prompt marker
            # to anchor on; just give up and let the next render reset region)
            if not self.current.prompt:
                return
            content = self.view.substr(sublime.Region(0, view_size))
            prompt_marker = f"◎ {self.current.prompt[:20]}"
            last_pos = content.rfind(prompt_marker)
            if last_pos >= 0:
                start = last_pos
                end = view_size
            else:
                return

        # Build the full text for this conversation
        lines = []

        # Prompt (newline before only if not at start)
        prefix = "\n" if self.current.region[0] > 0 else ""
        # Skip prompt header for carry-forward conversations (empty prompt)
        # used by clear() to keep todos / bg tools visible without a fake "◎ ▶".
        if self.current.prompt:
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
                lines.append(f"  {CONTEXT_PREFIX}{context_str}\n")
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
            # While the provider is retrying a 429/5xx, surface the hint as its
            # own line — scoped claude.retry (muted) via the syntax, and dropped
            # by _clear_api_retry_hint as soon as content resumes.
            try:
                from . import claude_code
                _sess = claude_code.get_session_for_view(self.view)
                _hint = getattr(_sess, "_api_retry_hint", None)
            except Exception:
                _hint = None
            if _hint:
                lines.append(f"  {_hint}\n")

        # Todo list — open work only (hide completed/cancelled/deleted).
        # Folded by default: running always shown, pending capped at 3.
        open_todos = _open_todos(self.current.todos)
        # Settled list → clear carry flag so next prompt starts empty.
        if not open_todos:
            self.current.todos_all_done = True
        if open_todos:
            active = [t for t in open_todos if _todo_is_active(t)]
            pending = [t for t in open_todos if not _todo_is_active(t)]
            expanded = bool(self.view.settings().get("claude_tasks_expanded", False)) \
                if self.view else False
            if expanded:
                show = open_todos
            else:
                cap = max(0, 3 - len(active))
                show = active + pending[:cap]
            if show:
                counts = "{} active · {} pending".format(len(active), len(pending))
                lines.append("\n  ───── Tasks  ·  {}  ─────\n".format(counts))
                for todo in show:
                    icon = "▸" if _todo_is_active(todo) else "○"
                    lines.append("  {} {}\n".format(icon, todo.content))
                hidden = len(open_todos) - len(show)
                if hidden > 0:
                    lines.append("  … +{} more  (super+click to expand)\n".format(hidden))
                elif expanded:
                    lines.append("  (super+click to collapse)\n")

        # Single sticky goal (Grok /goal + update_goal) — not a multi-item list.
        goal = self.current.goal
        if goal and goal.status in ("active", "blocked", "completed"):
            if goal.status == "blocked":
                label = "blocked"
                body = goal.blocked_reason or goal.message or ""
            elif goal.status == "completed":
                label = "done"
                body = goal.message or ""
            else:
                label = "active"
                body = goal.message or ""
            lines.append("\n  ───── Goal  ·  {}  ─────\n".format(label))
            if body:
                lines.append("  ▸ {}\n".format(body))

        # Meta
        if self.current.duration > 0:
            meta_parts = [f"{self.current.duration:.1f}s"]
            if self.current.usage:
                u = self.current.usage
                input_t = (u.get("input_tokens", 0)
                         + u.get("cache_read_input_tokens", 0)
                         + u.get("cache_creation_input_tokens", 0))
                if input_t:
                    if input_t >= 1000:
                        meta_parts.append(f"{input_t // 1000}k ctx")
                    else:
                        meta_parts.append(f"{input_t} ctx")
            # Append current provider + resolved model so it's visible per-turn
            # (read from view settings set at session start in Session.start).
            _vs = self.view.settings() if self.view else None
            if _vs is not None:
                _label = _vs.get("claude_provider_label")
                _model = _vs.get("claude_model")
                if _model:
                    # "Claude/opus" is verbose; show just the model for the
                    # default backend, full label/model otherwise.
                    if _label and _label != "Claude":
                        meta_parts.append(f"{_label}/{_model}")
                    else:
                        meta_parts.append(_model)
                elif _label and _label != "Claude":
                    meta_parts.append(_label)
            lines.append(f"\n  @done({', '.join(meta_parts)})\n")

        text = "".join(lines)

        # Re-read view size (may have changed during text building)
        view_size = self.view.size()
        # If there's content after our region, extend end to clean it up
        # This handles race conditions where content was orphaned from previous renders
        # BUT: Don't extend if there's a permission block - that's intentional content after the region
        rerender_ui = False
        has_trailing_ui = (
            (self.pending_permission and self.pending_permission.callback) or
            (self.pending_plan and self.pending_plan.callback) or
            (self.pending_question and self.pending_question.callback)
        )
        if has_trailing_ui:
            # Clamp end to not eat the trailing UI block
            if self.pending_permission and self.pending_permission.callback:
                ui_key = "claude_permission_block"
            elif self.pending_plan and self.pending_plan.callback:
                ui_key = "claude_plan_block"
            else:
                ui_key = "claude_question_block"
            ui_region = self.view.get_regions(ui_key)
            if ui_region and ui_region[0].size() > 0:
                end = min(end, ui_region[0].begin())
            else:
                # UI block tracked region lost — extend to clean up, then re-render UI
                end = view_size
                rerender_ui = True
        elif end < view_size:
            end = view_size
        new_end = self._replace(start, end, text)
        self.current.region = (start, new_end)
        self.view.add_regions(
            "claude_conversation",
            [sublime.Region(start, new_end)],
            "", "", sublime.HIDDEN,
        )

        # Update title to reflect working state
        self._update_title()

        # Re-render UI blocks only if their tracked regions were lost
        if rerender_ui:
            if self.pending_permission and self.pending_permission.callback:
                self._render_permission()
            if self.pending_question and self.pending_question.callback:
                self._render_question()

        # Scroll after render completes (only if auto_scroll is enabled)
        if getattr(self, '_auto_scroll', True):
            self._scroll_to_end()

        # Inline image phantoms (minihtml data: + explicit size — ST docs).
        # Defer one tick so region positions match the final buffer.
        sublime.set_timeout(self._refresh_media_phantoms, 10)

    def _format_tool_detail(self, tool: ToolCall) -> str:
        """Format tool detail string. Dispatches via TOOL_FORMATTERS registry."""
        return format_tool_detail(self, tool)

    def _format_x_search_result(self, result: str) -> str:
        """Compact summary for X/Twitter tool results."""
        if not result or not result.strip():
            return " → 0"
        text = result.strip()
        # JSON list of posts?
        try:
            import json
            data = json.loads(text)
            if isinstance(data, list):
                return f" → {len(data)} posts"
            if isinstance(data, dict):
                posts = data.get("posts") or data.get("results") or data.get("data")
                if isinstance(posts, list):
                    return f" → {len(posts)} posts"
                if "username" in data or "name" in data:
                    return f" → @{data.get('username') or data.get('name')}"
        except Exception:
            pass
        lines = [l for l in text.splitlines() if l.strip()]
        # Count http links as rough post count
        import re
        urls = re.findall(r'https?://(?:x|twitter)\.com/\S+', text)
        if urls:
            return f" → {len(set(urls))} links"
        if len(lines) <= 1 and len(text) < 80:
            return f" → {text[:60]}"
        return f" → {len(lines)} lines"

    def _media_path_for_tool(self, tool: "ToolCall") -> Optional[str]:
        if not isinstance(tool.tool_input, dict):
            return extract_media_path(tool.result, None)
        return (
            tool.tool_input.get("_media_path")
            or extract_media_path(tool.result, tool.tool_input)
        )

    # minihtml only documents PNG/JPG/GIF. Phantoms use real downscaled
    # thumbnails (small base64) — not full-res with CSS max-width.
    _MEDIA_SOURCE_MAX_BYTES = 8_000_000  # refuse to read enormous originals
    _MEDIA_PHANTOM_MAX_W = 96            # inline thumbnail edge
    _MEDIA_POPUP_MAX_W = 360            # enlarge popup edge
    _MINIHTML_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif")

    def _minihtml_image_ok(self, path: str) -> bool:
        return bool(path) and path.lower().endswith(self._MINIHTML_IMAGE_EXTS)

    def _image_dimensions_from_bytes(self, data: bytes) -> tuple:
        """Pixel size from PNG/JPEG/GIF header bytes. (0,0) if unknown."""
        try:
            import struct
            if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
                w, h = struct.unpack(">II", data[16:24])
                return int(w), int(h)
            if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
                w, h = struct.unpack("<HH", data[6:10])
                return int(w), int(h)
            if data[:2] == b"\xff\xd8":
                i = 2
                n = len(data)
                while i + 9 < n:
                    if data[i] != 0xFF:
                        break
                    marker = data[i + 1]
                    seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
                    if marker in (
                        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
                    ):
                        h, w = struct.unpack(">HH", data[i + 5:i + 9])
                        return int(w), int(h)
                    i += 2 + seglen
        except Exception:
            pass
        return 0, 0

    def _image_dimensions(self, path: str) -> tuple:
        try:
            with open(path, "rb") as f:
                return self._image_dimensions_from_bytes(f.read(65536))
        except Exception:
            return 0, 0

    def _scaled_display_size(self, w: int, h: int, max_w: int) -> tuple:
        """Scale (w,h) into max_w box, keep aspect."""
        if w <= 0 or h <= 0:
            return max_w, max_w
        if w <= max_w and h <= max_w:
            return w, h
        scale = min(max_w / float(w), max_w / float(h))
        return max(1, int(round(w * scale))), max(1, int(round(h * scale)))

    def _make_thumbnail_bytes(self, path: str, max_edge: int) -> Optional[tuple]:
        """Downscale image → (jpeg_bytes, width, height). Prefer small JPEG.

        Tries Pillow, then macOS sips. Returns None if resize unavailable.
        """
        ow, oh = self._image_dimensions(path)
        tw, th = self._scaled_display_size(ow, oh, max_edge)
        # Already tiny — still re-encode as JPEG for consistent size, but
        # only if we have a resizer; else caller may fall back carefully.
        # 1) Pillow
        try:
            from PIL import Image  # type: ignore
            import io
            with Image.open(path) as im:
                im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
                if im.mode == "L":
                    im = im.convert("RGB")
                im.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS
                             if hasattr(Image, "Resampling")
                             else Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=72, optimize=True)
                data = buf.getvalue()
                return data, im.size[0], im.size[1]
        except Exception:
            pass
        # 2) macOS sips → temp jpeg (absolute path — ST's PATH may omit /usr/bin)
        if sublime.platform() == "osx":
            import subprocess
            import tempfile
            sips = "/usr/bin/sips"
            if not os.path.isfile(sips):
                sips = "sips"
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(suffix=".jpg")
                os.close(fd)
                # -Z: max dimension (fit inside max_edge box)
                r = subprocess.run(
                    [sips, "-Z", str(max_edge), "-s", "format", "jpeg",
                     path, "--out", tmp],
                    capture_output=True, timeout=15)
                if r.returncode == 0 and os.path.isfile(tmp):
                    with open(tmp, "rb") as f:
                        data = f.read()
                    if data:
                        w, h = self._image_dimensions_from_bytes(data)
                        if w <= 0:
                            w, h = tw, th
                        return data, w, h
            except Exception as e:
                print(f"[Claude] sips thumbnail: {e}")
            finally:
                if tmp and os.path.isfile(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        return None

    def _media_embed(self, path: str, max_edge: int = None) -> Optional[tuple]:
        """Return (data_uri, width, height) — downscaled thumbnail for embeds.

        Phantoms pass a small max_edge so base64 stays tiny (~few KB).
        """
        if max_edge is None:
            max_edge = self._MEDIA_PHANTOM_MAX_W
        if not self._minihtml_image_ok(path) or not os.path.isfile(path):
            return None
        try:
            mtime = os.path.getmtime(path)
            size = os.path.getsize(path)
            if size <= 0 or size > self._MEDIA_SOURCE_MAX_BYTES:
                return None
            cache_key = f"{path}|{max_edge}"
            cached = self._media_uri_cache.get(cache_key)
            if cached and cached[0] == mtime:
                return cached[1], cached[2], cached[3]

            import base64
            thumb = self._make_thumbnail_bytes(path, max_edge)
            if thumb:
                data, w, h = thumb
                uri = "data:image/jpeg;base64," + base64.b64encode(data).decode(
                    "ascii")
                self._media_uri_cache[cache_key] = (mtime, uri, w, h)
                return uri, w, h

            # No resizer: only embed if original is already tiny.
            if size > 40_000:
                print(f"[Claude] media: no thumbnailer, skip large embed "
                      f"({size} B)")
                return None
            with open(path, "rb") as f:
                raw = f.read()
            ext = os.path.splitext(path)[1].lower()
            mime = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".gif": "image/gif",
            }.get(ext, "image/jpeg")
            uri = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
            w, h = self._image_dimensions_from_bytes(raw)
            w, h = self._scaled_display_size(w, h, max_edge)
            self._media_uri_cache[cache_key] = (mtime, uri, w, h)
            return uri, w, h
        except Exception as e:
            print(f"[Claude] media embed: {e}")
            return None

    def _refresh_media_phantoms(self) -> None:
        """Inline minihtml phantoms under image tool lines (not video).

        Per ST docs: <img> supports PNG/JPG/GIF via data:/file:/res:.
        Forum: data: + explicit width/height → correct phantom box size.
        """
        if not self.view or not self.view.is_valid():
            return
        if self._media_phantom_set is None:
            try:
                self._media_phantom_set = sublime.PhantomSet(
                    self.view, "claude_media")
            except Exception as e:
                print(f"[Claude] media PhantomSet: {e}")
                return

        media_tools = []
        for conv in list(self.conversations) + (
                [self.current] if self.current is not None else []):
            for event in conv.events:
                if (isinstance(event, ToolCall)
                        and event.name in MEDIA_TOOLS
                        and event.status == DONE):
                    media_tools.append(event)

        if not media_tools:
            self._media_anchor.clear()
            try:
                self._media_phantom_set.update([])
            except Exception:
                pass
            return

        content = self.view.substr(sublime.Region(0, self.view.size()))
        phantoms = []
        used_pts = set()
        anchors: Dict[str, int] = {}
        sym = self.SYMBOLS.get(DONE, "✔")

        for tool in media_tools:
            path = self._media_path_for_tool(tool)
            if not path or not is_image_path(path):
                continue  # video: no inline preview
            if not os.path.isfile(path):
                continue
            disp = media_display_path(path)
            # Locate tool line (last match for this name+path wins).
            candidates = []
            pat = (r"(?m)^  " + _re.escape(sym) + r" "
                   + _re.escape(tool.name) + r".*")
            for m in _re.finditer(pat, content):
                line = m.group(0)
                if disp and disp in line:
                    candidates.append(m.start())
                elif path in line:
                    candidates.append(m.start())
                elif not candidates:
                    candidates.append(m.start())
            if not candidates:
                continue
            pt = candidates[-1]
            if pt in used_pts:
                continue
            used_pts.add(pt)
            line_end = content.find("\n", pt)
            if line_end < 0:
                line_end = len(content)
            # Anchor at end of tool line → LAYOUT_BLOCK draws below it.
            region = sublime.Region(line_end, line_end)
            # Popup should open here (next to thumb), not at EOF.
            anchors[path] = line_end
            html = self._media_phantom_html(path, disp)
            if not html:
                continue

            def _nav(href, _path=path, _loc=line_end):
                self._handle_media_href(href, _path, location=_loc)

            try:
                phantoms.append(sublime.Phantom(
                    region, html, sublime.LAYOUT_BLOCK, _nav))
            except TypeError:
                phantoms.append(sublime.Phantom(
                    region, html, sublime.LAYOUT_BLOCK))

        self._media_anchor = anchors
        try:
            self._media_phantom_set.update(phantoms)
        except Exception as e:
            print(f"[Claude] media phantoms update: {e}")

    def _media_phantom_html(self, path: str, disp: str) -> str:
        """Inline phantom body — data: URI + width/height (docs/forum recipe)."""
        import html as _html
        safe_disp = _html.escape(disp or os.path.basename(path) or path)
        reveal_href = "reveal:" + path
        popup_href = "popup:" + path
        # No "open in Sublime" — ST image views are useless for generated media.
        links = (
            f'<a href="{_html.escape(reveal_href)}">reveal</a>'
            f' · <a href="{_html.escape(popup_href)}">enlarge</a>'
            f' · <span style="color:color(var(--foreground) alpha(0.5))">'
            f'{safe_disp}</span>'
        )
        embed = self._media_embed(path, self._MEDIA_PHANTOM_MAX_W)
        if embed:
            uri, w, h = embed
            # Explicit width/height required for correct phantom box (forum).
            # Bytes are already a real downscaled JPEG thumbnail.
            # Click thumbnail → enlarge popup (not open in ST).
            img = (
                f'<div style="margin:2px 0 2px 0">'
                f'<a href="{_html.escape(popup_href)}">'
                f'<img src="{uri}" width="{w}" height="{h}" />'
                f'</a></div>'
            )
            return (
                f'<body id="claude-media-phantom" '
                f'style="margin:0;padding:0 0 0 24px;font-size:11px;'
                f'color:color(var(--foreground) alpha(0.7))">'
                f'{img}{links}</body>'
            )
        # Too large / unsupported type — links only under the tool line.
        return (
            f'<body id="claude-media-phantom" '
            f'style="margin:0;padding:2px 0 4px 24px;font-size:11px;'
            f'color:color(var(--foreground) alpha(0.7))">'
            f'🖼 {links}</body>'
        )

    def show_media_popup(self, path: str, location: int = -1) -> None:
        """Larger popup preview (super+click / enlarge link), anchored near image."""
        if not self.view or not self.view.is_valid() or not path:
            return
        path = os.path.expanduser(path)
        html = self._media_popup_html(path)
        if not html:
            sublime.status_message(f"Media: {path}")
            return
        if location < 0:
            location = self._media_anchor.get(path, -1)
        if location < 0:
            # Fallback: short path text in buffer, else selection — never EOF.
            disp = media_display_path(path)
            content = self.view.substr(sublime.Region(0, self.view.size()))
            if disp:
                idx = content.rfind(disp)
                if idx >= 0:
                    location = idx
            if location < 0:
                sel = self.view.sel()
                location = sel[0].begin() if sel else 0
        # Keep anchor visible so popup doesn't appear off-screen / at bottom.
        try:
            self.view.show(location)
        except Exception:
            pass
        try:
            self.view.show_popup(
                html,
                flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY,
                location=location,
                max_width=560,
                max_height=480,
                on_navigate=lambda href, _p=path, _l=location: self._handle_media_href(
                    href, _p, location=_l),
            )
        except Exception as e:
            print(f"[Claude] media popup: {e}")
            sublime.status_message(f"Media: {media_display_path(path) or path}")

    def _media_popup_html(self, path: str) -> str:
        """minihtml for enlarge popup — same data: recipe, bigger max size."""
        import html as _html
        disp = media_display_path(path) or os.path.basename(path) or path
        safe_disp = _html.escape(disp)
        reveal_href = "reveal:" + path
        style = (
            "margin:0;padding:8px 10px;"
            "background-color:var(--background);"
            "color:var(--foreground);font-size:12px;"
        )
        links = (
            f'<a href="{_html.escape(reveal_href)}">reveal</a>'
            f' · <a href="dismiss:">dismiss</a>'
            f'<br><span style="color:color(var(--foreground) alpha(0.55))">'
            f'{safe_disp}</span>'
        )
        if is_video_path(path):
            return (
                f'<body id="claude-media-popup" style="{style}">'
                f'<div style="margin-bottom:6px">🎬 video '
                f'(no inline preview)</div>{links}</body>'
            )
        if not is_image_path(path):
            return f'<body id="claude-media-popup" style="{style}">{links}</body>'
        embed = self._media_embed(path, self._MEDIA_POPUP_MAX_W)
        if embed:
            uri, w, h = embed
            img = (
                f'<div style="margin:0 0 6px 0">'
                f'<img src="{uri}" width="{w}" height="{h}" />'
                f'</div>'
            )
            return f'<body id="claude-media-popup" style="{style}">{img}{links}</body>'
        return (
            f'<body id="claude-media-popup" style="{style}">'
            f'<div style="margin-bottom:6px">🖼 image '
            f'(preview too large or unsupported)</div>{links}</body>'
        )

    def _handle_media_href(self, href: str, fallback_path: str = "",
                           location: int = -1) -> None:
        """Navigate handler for media phantom/popup links (reveal / enlarge)."""
        import subprocess
        if href == "dismiss:" or href.startswith("dismiss"):
            try:
                self.view.hide_popup()
            except Exception:
                pass
            return
        path = fallback_path or ""
        if href.startswith("popup:"):
            path = href[6:] or fallback_path
            # Anchor popup at the tool/phantom line, not the bottom of the view.
            loc = location if location >= 0 else self._media_anchor.get(
                os.path.expanduser(path), -1)
            self.show_media_popup(path, location=loc)
            return
        if href.startswith("reveal:"):
            path = href[7:]
        path = os.path.expanduser(path)
        if not path:
            return
        # reveal in Finder / Explorer / file manager — never open in ST
        try:
            if sublime.platform() == "osx":
                subprocess.Popen(["open", "-R", path])
            elif sublime.platform() == "windows":
                subprocess.Popen(["explorer", "/select,", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path) or "."])
            sublime.status_message(f"Revealed {os.path.basename(path)}")
        except Exception as e:
            sublime.status_message(f"Reveal failed: {e}")

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
        """Format ask_user Q&A result. The answer is already recorded as a ☑
        decision line at submit time (see _clear_question), so we don't echo it
        again under the tool line — only surface a cancellation."""
        try:
            if '"cancelled": true' in result or '"cancelled":true' in result:
                return "\n    → (cancelled)"
            return ""
        except Exception:
            return ""

    def _find_line_number(self, file_path: str, old: str, new: str) -> int:
        """Find the line number where old_string (or new_string for new content) occurs in file."""
        import os
        if not file_path or not os.path.exists(file_path):
            return None
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            # After edit, look for new_string; before edit would have old_string
            search = new if new else old
            if not search:
                return None
            pos = content.find(search)
            if pos == -1 and old:
                # Try old_string if new not found (edit might have failed)
                pos = content.find(old)
            if pos == -1:
                return None
            # Count newlines before position to get line number
            return content[:pos].count('\n') + 1
        except Exception:
            return None

    def _format_edit_diff(self, old: str, new: str) -> str:
        """Format Edit diff.

        Returns:
            diff_string for display
        """
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

        # Process diff lines, skip headers
        diff_lines = []
        for line in diff:
            if line.startswith('---') or line.startswith('+++') or line.startswith('@@'):
                continue
            diff_lines.append(line.rstrip('\n'))

        if not diff_lines:
            return ""

        return "\n```diff\n" + "\n".join(diff_lines) + "\n```"

    def _extract_diff_line_num(self, unified: str) -> int:
        """Extract the starting line number from the first hunk header of a unified diff."""
        import re
        m = re.search(r'^@@\s+-(\d+)', unified, re.MULTILINE)
        if m:
            return int(m.group(1))
        return 0

    def _format_unified_diff(self, unified: str) -> str:
        """Render a pre-computed unified diff, stripping headers."""
        if not unified:
            return ""
        lines = []
        for line in unified.splitlines():
            if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
                continue
            lines.append(line)
        if not lines:
            return ""
        return "\n```diff\n" + "\n".join(lines) + "\n```"


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
