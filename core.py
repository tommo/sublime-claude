"""Claude Code core - session management and plugin lifecycle."""
import time

import sublime
import sublime_plugin
from typing import Dict, Optional

from .session import Session
from . import backends


_auto_sleep_timer = None


def plugin_loaded() -> None:
    """Called when plugin is loaded. Start MCP server and notalone client."""
    # Initialize session registry on sublime module (singleton)
    if not hasattr(sublime, '_claude_sessions'):
        sublime._claude_sessions = {}

    # Start MCP server
    from . import mcp_server
    mcp_server.start()

    # Start global notalone client (receives all injects for sublime.* sessions)
    from . import notalone
    notalone.start()

    # Orphaned claude_output views reconnect lazily on first activation via
    # ClaudeOutputEventListener._reconnect_orphaned_view (listeners.py).
    # Order bookmarks attach lazily on view load via ClaudeCodeEventListener.on_load.
    schedule_auto_sleep()


def plugin_unloaded() -> None:
    """Called when plugin is unloaded. Stop MCP server and notalone client."""
    from . import mcp_server
    mcp_server.stop()

    from . import notalone
    notalone.stop()


def get_session_for_view(view: sublime.View) -> Optional[Session]:
    """Get session for a specific output view."""
    return sublime._claude_sessions.get(view.id())


def get_active_session(window: sublime.Window) -> Optional[Session]:
    """Get session for active view if it's a Claude output, or last active Claude session in window."""
    view = window.active_view()
    if view and view.settings().get("claude_output"):
        return sublime._claude_sessions.get(view.id())
    # Check for last active Claude view in this window
    active_view_id = window.settings().get("claude_active_view")
    if active_view_id and active_view_id in sublime._claude_sessions:
        session = sublime._claude_sessions[active_view_id]
        if session.window == window:
            return session
    # Fallback: return any session in this window
    for view_id, session in sublime._claude_sessions.items():
        if session.window == window:
            return session
    return None


def create_session(window: sublime.Window, resume_id: Optional[str] = None, fork: bool = False, profile: Optional[dict] = None, initial_context: Optional[dict] = None, backend: str = "claude") -> Session:
    """Create a new session (always creates new, doesn't reuse)."""
    # Clear active marker from previous active session
    old_active = window.settings().get("claude_active_view")
    if old_active and old_active in sublime._claude_sessions:
        old_session = sublime._claude_sessions[old_active]
        old_session.output.set_name(old_session.name or "Claude")

    s = Session(window, resume_id=resume_id, fork=fork, profile=profile, initial_context=initial_context, backend=backend)
    s.output.show()  # Create view first
    if s.output.view and backend != "claude":
        spec = backends.get(backend)
        s.output.view.settings().set("claude_backend", backend)
        s.output.set_name(spec.label)
        if spec.theme:
            s.output.view.settings().set("color_scheme", spec.theme)
    s.start()
    # Register by view id and mark as active
    if s.output.view:
        view_id = s.output.view.id()
        sublime._claude_sessions[view_id] = s
        window.settings().set("claude_active_view", view_id)
        print(f"[Claude] create_session: view_id={view_id}")
    else:
        print(f"[Claude] create_session: ERROR - no output view!")
    schedule_auto_sleep()
    return s


def _check_auto_sleep():
    global _auto_sleep_timer
    _auto_sleep_timer = None

    settings = sublime.load_settings("ClaudeCode.sublime-settings")
    timeout_min = settings.get("auto_sleep_minutes", 60)
    if not timeout_min or timeout_min <= 0:
        return

    threshold = time.time() - (timeout_min * 60)
    force_threshold = time.time() - (timeout_min * 60 * 2)

    for view_id, session in list(sublime._claude_sessions.items()):
        if getattr(session, 'sleep_disabled', False):
            continue
        if (session.initialized
                and not session.working
                and not session.is_sleeping
                and session.last_idle_at > 0
                and session.last_idle_at < threshold):
            force = session.last_idle_at < force_threshold
            print(f"[Claude] auto-sleep: {session.name} idle for >{timeout_min}m (force={force})")
            session.sleep(force=force)

    schedule_auto_sleep()


def schedule_auto_sleep():
    global _auto_sleep_timer
    if _auto_sleep_timer is None and hasattr(sublime, '_claude_sessions') and sublime._claude_sessions:
        _auto_sleep_timer = sublime.set_timeout(_check_auto_sleep, 60000)
