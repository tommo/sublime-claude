"""Claude Code core - session management and plugin lifecycle."""
import time

import sublime
import sublime_plugin
from typing import Dict, Optional

from .session import Session
from . import backends


_auto_sleep_timer = None
# Wall-clock when plugin_loaded ran. Used to suppress focus/UI churn while ST
# restores sheets and re-attaches ViewEventListeners (each matching view can
# get a spurious on_activated during that window).
_PLUGIN_LOADED_AT: float = 0.0
_STARTUP_QUIET_S = 3.0


def in_startup_quiet() -> bool:
    """True for a few seconds after plugin load / package reload."""
    if _PLUGIN_LOADED_AT <= 0:
        return False
    return (time.time() - _PLUGIN_LOADED_AT) < _STARTUP_QUIET_S


def plugin_loaded() -> None:
    """Called when plugin is loaded. Start MCP server and notalone client."""
    global _PLUGIN_LOADED_AT
    _PLUGIN_LOADED_AT = time.time()

    # Initialize session registry on sublime module (singleton).
    # Package reload leaves *stale* Session objects here (old module instance).
    # Touching them in on_activated (set_name / sleep UI / input) cycles focus
    # across every Claude sheet. Drop them; reattach is lazy on real focus.
    prev = getattr(sublime, "_claude_sessions", None) or {}
    if prev:
        print(f"[Claude] plugin_loaded: dropping {len(prev)} stale session(s)")
        for _vid, s in list(prev.items()):
            try:
                if getattr(s, "client", None):
                    s.client = None  # don't block load on shutdown
            except Exception:
                pass
        prev.clear()
    sublime._claude_sessions = prev if isinstance(prev, dict) else {}

    # Start MCP server
    from . import mcp_server
    mcp_server.start()

    # Start global notalone client (receives all injects for sublime.* sessions)
    from . import notalone
    notalone.start()

    # 1) Immediately strip leftover ◎ from restored buffers so the composer
    #    never flashes before sleep restore (ST session restore leaves ◎ in text).
    # 2) After quiet: register Session objects as sleeping + paint chrome.
    schedule_auto_sleep()
    sublime.set_timeout(_startup_strip_composers, 0)
    sublime.set_timeout(_startup_strip_composers, 100)  # ST may still be restoring
    sublime.set_timeout(_startup_settle_views, int(_STARTUP_QUIET_S * 1000) + 50)


def _startup_strip_composers() -> None:
    """First paint: remove sticky ◎ from every Claude sheet before user sees it."""
    try:
        from .output_view import OutputView
        n = 0
        for w in sublime.windows():
            for v in w.views():
                if not v.settings().get("claude_output"):
                    continue
                if v.settings().get("claude_quick"):
                    continue
                v.settings().set("claude_input_mode", False)
                # Prefer sleeping chrome once we restore; mark early for keymaps
                if v.settings().get("claude_backend") or v.settings().get("claude_sleeping"):
                    # Don't force sleeping on brand-new empty sheets
                    pass
                if OutputView.strip_composer_tail(v):
                    n += 1
        if n:
            print(f"[Claude] startup: stripped composer from {n} sheet(s)")
    except Exception as e:
        print(f"[Claude] startup strip: {e}")


def _startup_settle_views() -> None:
    """Restore all Claude sheets into sleeping Session objects (no dual phase)."""
    try:
        # One more strip in case ST finished restoring buffer after first pass
        _startup_strip_composers()
        from .listeners import settle_startup_claude_views
        settle_startup_claude_views()
    except Exception as e:
        print(f"[Claude] startup settle: {e}")


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
        s = sublime._claude_sessions.get(view.id())
        if s:
            return s
    # Prefer a working session in this window (incl. Quick Agent mid-turn)
    working = None
    for view_id, session in sublime._claude_sessions.items():
        if session.window == window and session.working:
            working = session
            if not getattr(session, "quick_mode", False):
                return session
    if working:
        return working
    # Check for last active Claude view in this window
    active_view_id = window.settings().get("claude_active_view")
    if active_view_id and active_view_id in sublime._claude_sessions:
        session = sublime._claude_sessions[active_view_id]
        if session.window == window and not getattr(session, "quick_mode", False):
            return session
    # Prefer a non-quick session in this window
    for view_id, session in sublime._claude_sessions.items():
        if session.window == window and not getattr(session, "quick_mode", False):
            return session
    # Quick Agent (panel) when nothing else is active
    try:
        from . import quick_agent
        qs = quick_agent.get_quick_session(window)
        if qs:
            return qs
    except Exception:
        pass
    return None


def create_session(window: sublime.Window, resume_id: Optional[str] = None, fork: bool = False, profile: Optional[dict] = None, initial_context: Optional[dict] = None, backend: Optional[str] = None, focus: bool = True) -> Session:
    """Create a new session (always creates new, doesn't reuse).

    focus=True (default): intentional New Session UX — sheet is focused.
    focus=False: background create (e.g. MCP spawn) — do not steal focus.
    Startup multi-tab restore does not use this path; orphans reconnect via
    listeners with a quiet window so they never raise every sheet.
    """
    if backend is None:
        backend = sublime.load_settings("ClaudeCode.sublime-settings").get("default_backend", "claude")

    # Clear active marker from previous active session
    old_active = window.settings().get("claude_active_view")
    if old_active and old_active in sublime._claude_sessions:
        old_session = sublime._claude_sessions[old_active]
        old_session.output.set_name(old_session.name or "Claude")

    s = Session(window, resume_id=resume_id, fork=fork, profile=profile, initial_context=initial_context, backend=backend)
    # New session: composer allowed after init (start sets False until then)
    s._composer_allowed = True
    # new_file() fires on_activated before we can register — suppress orphan
    # reconnect so we don't attach a sleeping session to this brand-new sheet.
    window.settings().set("claude_creating_session", True)
    try:
        s.output.show(focus=focus)
        if s.output.view and backend != "claude":
            spec = backends.get(backend)
            s.output.view.settings().set("claude_backend", backend)
            s.output.set_name(spec.label)
            if spec.theme:
                s.output.view.settings().set("color_scheme", spec.theme)
        # Register before start so any later activation finds this session.
        if s.output.view:
            view_id = s.output.view.id()
            sublime._claude_sessions[view_id] = s
            # Track as last-active session for commands; view focus is separate.
            window.settings().set("claude_active_view", view_id)
            print(f"[Claude] create_session: view_id={view_id} focus={focus}")
        else:
            print(f"[Claude] create_session: ERROR - no output view!")
        s.start()
    finally:
        window.settings().erase("claude_creating_session")
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
        if getattr(session, 'quick_mode', False):
            continue
        # Host goal harness: don't auto-sleep while a goal is open/active.
        try:
            gt = getattr(session, "goal_tracker", None)
            if gt is not None and gt.is_open() and gt.status in (
                    "active", "infra_paused"):
                continue
        except Exception:
            pass
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
