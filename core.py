"""Claude Code core - session management and plugin lifecycle."""
import sublime
import sublime_plugin
from typing import Dict, Optional

from .session import Session


# Sessions keyed by output view id - allows multiple sessions per window
_sessions: Dict[int, Session] = {}


def plugin_loaded() -> None:
    """Called when plugin is loaded. Start MCP server (orphaned views reconnect on focus)."""
    # Lazy import to avoid circular import with mcp_server
    from . import mcp_server
    mcp_server.start()


def plugin_unloaded() -> None:
    """Called when plugin is unloaded. Stop MCP server."""
    # Lazy import to avoid circular import with mcp_server
    from . import mcp_server
    mcp_server.stop()


def get_session_for_view(view: sublime.View) -> Optional[Session]:
    """Get session for a specific output view."""
    return _sessions.get(view.id())


def get_active_session(window: sublime.Window) -> Optional[Session]:
    """Get session for active view if it's a Claude output, or last active Claude session in window."""
    view = window.active_view()
    if view and view.settings().get("claude_output"):
        return _sessions.get(view.id())
    # Check for last active Claude view in this window
    active_view_id = window.settings().get("claude_active_view")
    if active_view_id and active_view_id in _sessions:
        session = _sessions[active_view_id]
        if session.window == window:
            return session
    # Fallback: return any session in this window
    for view_id, session in _sessions.items():
        if session.window == window:
            return session
    return None


def create_session(window: sublime.Window, resume_id: Optional[str] = None, fork: bool = False, profile: Optional[dict] = None) -> Session:
    """Create a new session (always creates new, doesn't reuse)."""
    # Clear active marker from previous active session
    old_active = window.settings().get("claude_active_view")
    if old_active and old_active in _sessions:
        old_session = _sessions[old_active]
        old_session.output.set_name(old_session.name or "Claude")

    s = Session(window, resume_id=resume_id, fork=fork, profile=profile)
    s.output.show()  # Create view first
    s.start()
    # Register by view id and mark as active
    if s.output.view:
        view_id = s.output.view.id()
        _sessions[view_id] = s
        window.settings().set("claude_active_view", view_id)
        print(f"[Claude] create_session: registered view_id={view_id}, _sessions={id(_sessions)}, count={len(_sessions)}, keys={list(_sessions.keys())}")
    else:
        print(f"[Claude] create_session: ERROR - no output view!")
    return s
