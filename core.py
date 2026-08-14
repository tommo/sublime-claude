"""Claude Code core - session management and plugin lifecycle."""
import time

import sublime
import sublime_plugin
from typing import Dict, Optional

from .session import Session
from . import backends
from .session_split import (
    remember_active_session,
    place_in_last_session_split,
)


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
    from . import session_registry
    prev = getattr(sublime, "_claude_sessions", None) or {}
    if prev:
        print(f"[Claude] plugin_loaded: dropping {len(prev)} stale session(s)")
        for _vid, s in list(prev.items()):
            try:
                if getattr(s, "client", None):
                    # Drop the Python ref only. terminate() on reload SIGTERM'd
                    # every live bridge (exit -15) and killed all sessions.
                    s.client = None
            except Exception:
                pass
            # Clear sleep/connect phantoms while we still have a view ref —
            # PhantomSet GC does not always remove HTML from the buffer.
            try:
                v = s.output.view if getattr(s, "output", None) else None
                if v and v.is_valid():
                    from .session import clear_claude_view_phantoms
                    clear_claude_view_phantoms(v)
            except Exception:
                pass
        prev.clear()
    sublime._claude_sessions = prev if isinstance(prev, dict) else {}
    sublime._claude_agents = {}  # agent_id → view_id (rebuilt on restore)
    bg = getattr(sublime, "_claude_background", None)
    if isinstance(bg, dict) and bg:
        print(f"[Claude] plugin_loaded: dropping {len(bg)} background session(s)")
        for s in list(bg.values()):
            try:
                if getattr(s, "client", None):
                    s.client = None
            except Exception:
                pass
        bg.clear()
    sublime._claude_background = bg if isinstance(bg, dict) else {}
    session_registry.ensure_registries()

    # Also sweep every Claude sheet (covers orphans not in the registry)
    try:
        from .session import clear_all_claude_phantoms
        n = clear_all_claude_phantoms()
        if n:
            print(f"[Claude] plugin_loaded: cleared phantoms on Claude views ({n} keys)")
    except Exception as e:
        print(f"[Claude] plugin_loaded: phantom clear: {e}")

    # Start MCP server
    from . import mcp_server
    mcp_server.start()

    # Plugin self-debug surface (socket op=debug + ring log)
    try:
        from . import devtools
        devtools.start()
    except Exception as e:
        print(f"[Claude] devtools start failed: {e}")

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
    """First paint: remove sticky ◎ + orphan phantoms before user sees them."""
    try:
        from .output_view import OutputView
        from .session import clear_claude_view_phantoms
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
                # Drop pre-reload sleep/queue banners (re-painted by settle)
                clear_claude_view_phantoms(v)
                if OutputView.strip_composer_tail(v):
                    n += 1
    except Exception:
        pass


def _startup_settle_views() -> None:
    """Restore all Claude sheets into sleeping Session objects (no dual phase)."""
    try:
        # One more strip in case ST finished restoring buffer after first pass
        _startup_strip_composers()
        from .listeners import settle_startup_claude_views
        settle_startup_claude_views()
        # Re-bind parent_view_id from stable parent_agent_id after all sheets load
        from . import session_registry
        n = session_registry.relink_all_parents()
        if n:
            print(f"[Claude] startup settle: relinked {n} parent view_id(s)")
    except Exception as e:
        print(f"[Claude] startup settle: {e}")


def plugin_unloaded() -> None:
    """Called when plugin is unloaded. Stop MCP server and notalone client."""
    # Clear phantoms while views are still valid — prevents sticky sleep banner
    # after soft package reload (Session refs die; HTML can remain).
    try:
        from .session import clear_all_claude_phantoms
        clear_all_claude_phantoms()
    except Exception:
        pass

    try:
        from . import devtools
        devtools.stop()
    except Exception:
        pass

    from . import mcp_server
    mcp_server.stop()

    from . import notalone
    notalone.stop()


def get_session_for_view(view: sublime.View) -> Optional[Session]:
    """Get session for a specific output view."""
    from . import session_registry
    session_registry.ensure_registries()
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
    if resume_id and not fork:
        try:
            from .session_registry import find_live_by_session_id
            existing = find_live_by_session_id(resume_id)
            if existing:
                from .session_list import reveal_live_session
                if reveal_live_session(window, existing, focus=focus):
                    return existing
        except Exception:
            pass
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
        if resume_id and s.output and s.output.view:
            if place_in_last_session_split(window, s.output.view) and focus:
                window.focus_view(s.output.view)
        if s.output.view and backend != "claude":
            spec = backends.get(backend)
            s.output.view.settings().set("claude_backend", backend)
            s.output.set_name(spec.label)
            if spec.theme:
                s.output.view.settings().set("color_scheme", spec.theme)
        # Register before start so any later activation finds this session.
        if s.output.view:
            view_id = s.output.view.id()
            from . import session_registry
            try:
                s._persist_view_identity()
            except Exception:
                pass
            session_registry.register_session(s)
            # Track as last-active session for commands; view focus is separate.
            remember_active_session(window, s.output.view)
            print(
                f"[Claude] create_session: agent_id={getattr(s, 'agent_id', None)} "
                f"view_id={view_id} focus={focus}"
            )
        else:
            print(f"[Claude] create_session: ERROR - no output view!")
        s.start()
    finally:
        window.settings().erase("claude_creating_session")
    schedule_auto_sleep()
    try:
        from .session_list import schedule_session_list_refresh
        schedule_session_list_refresh()
    except Exception:
        pass
    return s


def _check_auto_sleep():
    global _auto_sleep_timer
    _auto_sleep_timer = None

    settings = sublime.load_settings("ClaudeCode.sublime-settings")
    timeout_min = settings.get("auto_sleep_minutes", 60)
    if not timeout_min or timeout_min <= 0:
        return

    now = time.time()
    threshold = now - (timeout_min * 60)
    force_threshold = now - (timeout_min * 60 * 2)

    try:
        from .session_registry import iter_sessions
        live = iter_sessions()
    except Exception:
        live = list((getattr(sublime, "_claude_sessions", None) or {}).values())
    for session in live:
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
        if not (session.initialized
                and not session.working
                and not session.is_sleeping):
            continue
        # Prefer last_idle_at (true idle). Fall back to last_activity so a
        # session that never stamped idle (or stamped it mid-turn long ago)
        # still needs a full timeout of *quiet* time after the last work.
        idle_at = float(getattr(session, "last_idle_at", 0) or 0)
        last_act = float(getattr(session, "last_activity", 0) or 0)
        # Idle clock cannot predate last work — long ACP turns used to leave
        # last_idle_at at pre-run sticky-◎ open time → instant sleep on done.
        effective_idle = max(idle_at, last_act)
        if effective_idle <= 0 or effective_idle >= threshold:
            continue
        force = effective_idle < force_threshold
        idle_m = int((now - effective_idle) / 60)
        print(
            f"[Claude] auto-sleep: {session.name} idle ~{idle_m}m "
            f"(timeout={timeout_min}m force={force})")
        session.sleep(force=force)

    schedule_auto_sleep()


def schedule_auto_sleep():
    global _auto_sleep_timer
    if _auto_sleep_timer is not None:
        return
    try:
        from .session_registry import iter_sessions
        if not iter_sessions():
            return
    except Exception:
        if not (hasattr(sublime, '_claude_sessions') and sublime._claude_sessions):
            return
    _auto_sleep_timer = sublime.set_timeout(_check_auto_sleep, 60000)
