"""Claude Code core - session management and plugin lifecycle."""
import sublime
import sublime_plugin
from typing import Dict, Optional

from .session import Session
from . import mcp_server


# Sessions keyed by output view id - allows multiple sessions per window
_sessions: Dict[int, Session] = {}


def plugin_loaded() -> None:
    """Called when plugin is loaded. Reconnect orphaned output views and start MCP server."""
    sublime.set_timeout(_reconnect_orphaned_views, 1000)
    mcp_server.start()


def plugin_unloaded() -> None:
    """Called when plugin is unloaded. Stop MCP server."""
    mcp_server.stop()


def _reconnect_orphaned_views() -> None:
    """Find Claude output views without sessions and reconnect them."""
    for window in sublime.windows():
        for view in window.views():
            if view.settings().get("claude_output") and view.id() not in _sessions:
                # Found orphaned output view - try to get session info from view name
                name = view.name()
                session_name = None
                # Strip status prefix if present (◉ active+working, ◇ active+idle, • inactive+working)
                if name.startswith(("◉ ", "◇ ", "• ")):
                    name = name[2:]
                if name.startswith("Claude: "):
                    session_name = name[8:]
                    # Strip any " - tool" suffix from stale working state
                    if " - " in session_name:
                        session_name = session_name.split(" - ")[0]
                # Handle spinner prefix (e.g., "⠋ name - thinking")
                for c in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏":
                    if name.startswith(c + " "):
                        name = name[2:]
                        if " - " in name:
                            session_name = name.split(" - ")[0]
                        else:
                            session_name = name
                        break

                # Try to find session_id from saved sessions to resume with context
                resume_id = None
                if session_name:
                    from .session import load_saved_sessions
                    for saved in load_saved_sessions():
                        if saved.get("name") == session_name:
                            resume_id = saved.get("session_id")
                            break

                # Create session with resume_id if found
                session = Session(window, resume_id=resume_id)
                session.name = session_name
                session.output.view = view  # Reuse existing view
                session.draft_prompt = ""  # Clear any stale draft
                _sessions[view.id()] = session

                # Reset active states (pending tools, permissions, stale title)
                session.output.reset_active_states()
                # Reset view title to clean state
                if session_name:
                    view.set_name(f"Claude: {session_name}")
                else:
                    view.set_name("Claude")

                session.start()

                if session_name:
                    view.set_status("claude_reconnect", f"Reconnected: {session_name}")
                    sublime.set_timeout(lambda v=view: v.erase_status("claude_reconnect"), 3000)


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


def create_session(window: sublime.Window, resume_id: Optional[str] = None, fork: bool = False) -> Session:
    """Create a new session (always creates new, doesn't reuse)."""
    # Clear active marker from previous active session
    old_active = window.settings().get("claude_active_view")
    if old_active and old_active in _sessions:
        old_session = _sessions[old_active]
        old_session.output.set_name(old_session.name or "Claude")

    s = Session(window, resume_id=resume_id, fork=fork)
    s.output.show()  # Create view first
    s.start()
    # Register by view id and mark as active
    if s.output.view:
        _sessions[s.output.view.id()] = s
        window.settings().set("claude_active_view", s.output.view.id())
    return s
