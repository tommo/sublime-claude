"""Plugin self-debug surface for agents and humans.

Starts with the plugin (MCP socket already up). Outside Sublime:

  python3 devtools_cli.py ping
  python3 devtools_cli.py snapshot
  python3 devtools_cli.py sessions
  python3 devtools_cli.py composer [view_id]
  python3 devtools_cli.py log --tail 80
  python3 devtools_cli.py eval 'return list(sublime._claude_sessions)'
  python3 devtools_cli.py reload          # soft in-process package reload
  python3 devtools_cli.py reload --hard   # ignored_packages cycle

Socket protocol (newline JSON on MCP_SOCKET_PATH):

  {"op":"debug","action":"ping"|"snapshot"|"sessions"|"composer"|"log"|"event"|"reload"|"help", ...}
  {"code":"..."}   # existing eval path

Host-only for plugin authors (CLI / socket). Not advertised in agent MCP tools/list.
"""
from __future__ import annotations

import collections
import json
import os
import threading
import time
import traceback
from typing import Any, Deque, Dict, List, Optional

import sublime

from .constants import BRIDGE_LOG_PATH, INPUT_MARKER, MCP_SOCKET_PATH

# ─── ring buffer (process-global on sublime so importlib.reload keeps history) ─

_MAX_EVENTS = 2000
_log_path = os.path.join(
    os.environ.get("TMPDIR")
    or os.environ.get("TEMP")
    or os.environ.get("TMP")
    or "/tmp",
    "claude_devtools.log",
)


def _state() -> dict:
    """Singleton state hung on the sublime module (survives module reload)."""
    st = getattr(sublime, "_claude_devtools", None)
    if not isinstance(st, dict) or "events" not in st:
        st = {
            "events": collections.deque(maxlen=_MAX_EVENTS),
            "lock": threading.Lock(),
            "started": False,
        }
        sublime._claude_devtools = st  # type: ignore[attr-defined]
    return st


def log(message: str, level: str = "info", **fields: Any) -> None:
    """Append a structured event (also mirrored to the log file)."""
    entry = {
        "ts": time.time(),
        "level": level,
        "msg": str(message),
    }
    if fields:
        entry["fields"] = {k: _safe(v) for k, v in fields.items()}
    st = _state()
    with st["lock"]:
        st["events"].append(entry)
    try:
        with open(_log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def start() -> None:
    """Called from plugin_loaded — idempotent across reloads."""
    st = _state()
    if st.get("started"):
        return
    st["started"] = True
    log("devtools started", socket=MCP_SOCKET_PATH, log_path=_log_path)
    print(f"[Claude Devtools] ready  socket={MCP_SOCKET_PATH}  log={_log_path}")


def stop() -> None:
    st = _state()
    if st.get("started"):
        log("devtools stopped")
    st["started"] = False


# ─── public snapshots ─────────────────────────────────────────────────────────

def ping() -> dict:
    st = _state()
    return {
        "ok": True,
        "plugin": "ClaudeCode",
        "socket": MCP_SOCKET_PATH,
        "log_path": _log_path,
        "events": len(st["events"]),
        "sessions": len(getattr(sublime, "_claude_sessions", {}) or {}),
        "time": time.time(),
        "started": bool(st.get("started")),
    }


def help_text() -> dict:
    return {
        "actions": [
            "ping",
            "snapshot [view_id]",
            "sessions",
            "composer [view_id]",
            "log [tail=N] [grep=substr]",
            "event message=...  (write agent note into ring)",
            "reload [mode=soft|hard]  (no ST restart)",
            "goal <objective> | status|pause|resume|clear  [view_id=]",
            "help",
        ],
        "cli": "python3 devtools_cli.py <action> ...",
        "socket": MCP_SOCKET_PATH,
        "log_path": _log_path,
    }


def reload_plugin(mode: str = "soft", **kwargs: Any) -> dict:
    """Schedule a correct package reload (soft or hard). See package_reloader."""
    from . import package_reloader
    log(f"reload scheduled mode={mode}")
    return package_reloader.schedule_reload(mode=mode, **kwargs)


def goal_command(args: str = "status", view_id: Optional[int] = None) -> dict:
    """Run /goal harness on a host session (same as typing /goal in the sheet)."""
    sess, vid = _resolve_session(view_id)
    if not sess:
        return {
            "ok": False,
            "error": "no session",
            "view_id": view_id,
            "available": list((getattr(sublime, "_claude_sessions", {}) or {}).keys()),
        }
    if not hasattr(sess, "handle_goal_command"):
        return {"ok": False, "error": "session has no handle_goal_command (stale class? reload)", "view_id": vid}
    try:
        sess.handle_goal_command(args or "status")
        gt = getattr(sess, "goal_tracker", None)
        return {
            "ok": True,
            "view_id": vid,
            "args": args,
            "goal": _goal_dump(sess),
            "working": bool(getattr(sess, "working", False)),
            "initialized": bool(getattr(sess, "initialized", False)),
            "sleeping": bool(getattr(sess, "is_sleeping", False)),
        }
    except Exception as e:
        log(f"goal_command error: {e}", level="error")
        return {"ok": False, "error": str(e), "traceback": traceback.format_exc(), "view_id": vid}


def sessions_dump() -> dict:
    """All host sessions (not just MCP-spawned subsessions)."""
    reg = getattr(sublime, "_claude_sessions", {}) or {}
    rows = []
    for vid, s in list(reg.items()):
        rows.append(_session_row(vid, s))
    rows.sort(key=lambda r: (not r.get("working"), r.get("view_id") or 0))
    return {"count": len(rows), "sessions": rows, "log_path": _log_path}


def snapshot(view_id: Optional[int] = None) -> dict:
    """Host + optional focused session/composer dump."""
    out: Dict[str, Any] = {
        "ping": ping(),
        "windows": _windows_brief(),
        "sessions": sessions_dump()["sessions"],
    }
    sess, vid = _resolve_session(view_id)
    if sess is not None:
        out["focus"] = {
            "view_id": vid,
            "session": _session_row(vid, sess, deep=True),
            "composer": _composer_dump(sess),
            "goal": _goal_dump(sess),
            "view_settings": _view_settings_dump(sess),
        }
    else:
        out["focus"] = {"error": "no session", "view_id": view_id}
    return out


def composer_dump(view_id: Optional[int] = None) -> dict:
    sess, vid = _resolve_session(view_id)
    if not sess:
        return {"error": "no session", "view_id": view_id,
                "available": list((getattr(sublime, "_claude_sessions", {}) or {}).keys())}
    return {
        "view_id": vid,
        "session": _session_row(vid, sess),
        "composer": _composer_dump(sess),
        "view_settings": _view_settings_dump(sess),
    }


def log_tail(tail: int = 80, grep: Optional[str] = None) -> dict:
    tail = max(1, min(int(tail or 80), _MAX_EVENTS))
    st = _state()
    with st["lock"]:
        items = list(st["events"])[-tail:]
        ring_count = len(st["events"])
    if grep:
        g = grep.lower()
        items = [
            e for e in items
            if g in (e.get("msg") or "").lower()
            or g in json.dumps(e.get("fields") or {}, default=str).lower()
        ]
    bridge_tail = _tail_file(BRIDGE_LOG_PATH, 40)
    file_tail = _tail_file(_log_path, tail)
    return {
        "ring": items,
        "ring_count": ring_count,
        "log_path": _log_path,
        "log_file_tail": file_tail,
        "bridge_log_path": BRIDGE_LOG_PATH,
        "bridge_tail": bridge_tail,
    }


def dispatch(action: str, **kwargs: Any) -> Any:
    """Route a debug action (socket / MCP / ST command)."""
    # Lazy-start if package hasn't re-run plugin_loaded yet
    if not _state().get("started"):
        start()
    action = (action or "help").strip().lower()
    try:
        if action in ("ping", "status"):
            return ping()
        if action in ("help", "?"):
            return help_text()
        if action in ("sessions", "list"):
            return sessions_dump()
        if action == "snapshot":
            return snapshot(kwargs.get("view_id"))
        if action == "composer":
            return composer_dump(kwargs.get("view_id"))
        if action == "log":
            return log_tail(tail=kwargs.get("tail", 80), grep=kwargs.get("grep"))
        if action == "event":
            msg = kwargs.get("message") or kwargs.get("msg") or ""
            log(msg or "(empty)", level="agent", **{
                k: v for k, v in kwargs.items()
                if k not in ("message", "msg", "action")
            })
            return {"ok": True, "logged": msg}
        if action in ("reload", "reload_plugin"):
            mode = kwargs.get("mode") or "soft"
            return reload_plugin(mode=mode, **{
                k: v for k, v in kwargs.items() if k != "mode"
            })
        if action == "goal":
            args = kwargs.get("args") or kwargs.get("message") or kwargs.get("cmd") or "status"
            return goal_command(args=args, view_id=kwargs.get("view_id"))
        return {"error": f"unknown action: {action}", "help": help_text()}
    except Exception as e:
        log(f"dispatch error: {e}", level="error", action=action)
        return {"error": str(e), "traceback": traceback.format_exc()}


# ─── internals ────────────────────────────────────────────────────────────────

def _safe(v: Any) -> Any:
    try:
        json.dumps(v)
        return v
    except Exception:
        return repr(v)[:500]


def _tail_file(path: str, n: int) -> List[str]:
    try:
        if not path or not os.path.isfile(path):
            return []
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
        return [ln.rstrip("\n") for ln in lines[-n:]]
    except Exception as e:
        return [f"<read error: {e}>"]


def _resolve_session(view_id: Optional[int] = None):
    reg = getattr(sublime, "_claude_sessions", {}) or {}
    if view_id is not None:
        try:
            vid = int(view_id)
        except (TypeError, ValueError):
            return None, view_id
        return reg.get(vid), vid

    win = sublime.active_window()
    if win:
        v = win.active_view()
        if v and v.id() in reg:
            return reg[v.id()], v.id()
        aid = win.settings().get("claude_active_view")
        if aid and aid in reg:
            return reg[aid], aid
    # Prefer a working non-quick session, else any
    working = None
    any_s = None
    for vid, s in reg.items():
        any_s = (s, vid)
        if getattr(s, "working", False) and not getattr(s, "quick_mode", False):
            working = (s, vid)
            break
    if working:
        return working
    if any_s:
        return any_s
    return None, None


def _session_row(vid: int, s, deep: bool = False) -> dict:
    view = None
    try:
        view = s.output.view if s.output else None
    except Exception:
        view = None

    row = {
        "view_id": vid,
        "name": getattr(s, "name", None),
        "backend": getattr(s, "backend", None),
        "working": bool(getattr(s, "working", False)),
        "initialized": bool(getattr(s, "initialized", False)),
        "session_id": getattr(s, "session_id", None) or getattr(s, "claude_session_id", None),
        "quick_mode": bool(getattr(s, "quick_mode", False)),
        "sleeping": bool(getattr(s, "sleeping", False)
                         or (view and view.settings().get("claude_sleeping"))),
        "query_count": getattr(s, "query_count", None),
        "parent_view_id": getattr(s, "parent_view_id", None),
        "composer_allowed": getattr(s, "_composer_allowed", None),
        "input_mode_entered": getattr(s, "_input_mode_entered", None),
        "client_alive": None,
        "view_valid": bool(view and view.is_valid()) if view is not None else False,
        "view_size": view.size() if view and view.is_valid() else None,
    }
    try:
        c = getattr(s, "client", None)
        if c is not None:
            row["client_alive"] = bool(getattr(c, "is_alive", lambda: None)())
    except Exception as e:
        row["client_alive"] = f"err:{e}"

    gt = getattr(s, "goal_tracker", None)
    if gt is not None:
        active = False
        try:
            if callable(getattr(gt, "is_active", None)):
                active = bool(gt.is_active())
            else:
                active = bool(getattr(gt, "active", False))
        except Exception:
            active = False
        row["goal"] = {
            "active": active,
            "phase": getattr(gt, "phase", None),
            "goal_id": getattr(gt, "goal_id", None),
            "status": getattr(gt, "status", None),
        }

    if deep:
        row["attrs_sample"] = sorted(
            a for a in dir(s)
            if not a.startswith("__") and not callable(getattr(s, a, None))
        )[:60]
    return row


def _goal_dump(s) -> dict:
    gt = getattr(s, "goal_tracker", None)
    if not gt:
        return {"present": False}
    out = {"present": True}
    for key in (
        "phase", "goal_id", "objective", "active", "paused",
        "plan_path", "status", "completed", "blocked_reason",
    ):
        if hasattr(gt, key):
            try:
                out[key] = _safe(getattr(gt, key))
            except Exception as e:
                out[key] = f"err:{e}"
    for meth in ("has_plan", "is_active", "summary", "to_dict", "state_dict"):
        fn = getattr(gt, meth, None)
        if callable(fn):
            try:
                out[meth] = _safe(fn())
            except Exception as e:
                out[meth] = f"err:{e}"
    return out


def _view_settings_dump(s) -> dict:
    try:
        view = s.output.view if s.output else None
    except Exception:
        view = None
    if not view or not view.is_valid():
        return {"error": "no view"}
    keys = (
        "claude_output", "claude_input_mode", "claude_sleeping",
        "claude_backend", "claude_quick", "claude_queue",
        "claude_session_id", "claude_name", "scroll_past_end",
        "word_wrap", "gutter",
    )
    st = view.settings()
    return {k: st.get(k) for k in keys}


def _composer_dump(s) -> dict:
    out: Dict[str, Any] = {}
    try:
        ov = s.output
    except Exception as e:
        return {"error": f"no output: {e}"}
    if not ov:
        return {"error": "output is None"}

    view = getattr(ov, "view", None)
    out["input_mode"] = bool(getattr(ov, "_input_mode", False))
    out["question_input_mode"] = bool(getattr(ov, "_question_input_mode", False))
    out["input_start"] = getattr(ov, "_input_start", None)
    out["input_area_start"] = getattr(ov, "_input_area_start", None)
    out["input_marker"] = getattr(ov, "_input_marker", INPUT_MARKER)
    out["has_pad_phantom"] = getattr(ov, "_pad_phantom_set", None) is not None
    out["has_media_phantom"] = getattr(ov, "_media_phantom_set", None) is not None
    out["render_pending"] = getattr(ov, "_render_pending", None)

    if not view or not view.is_valid():
        out["view"] = "invalid"
        return out

    size = view.size()
    out["size"] = size
    out["sel"] = [(r.a, r.b) for r in view.sel()]
    try:
        out["viewport_position"] = list(view.viewport_position())
        out["viewport_extent"] = list(view.viewport_extent())
        out["layout_extent"] = list(view.layout_extent())
        out["line_height"] = view.line_height()
    except Exception as e:
        out["geometry_error"] = str(e)

    # Last lines of buffer (composer lives at EOF)
    try:
        tail_start = view.line(max(0, size - 1)).begin()
        # back up a few lines
        for _ in range(8):
            if tail_start <= 0:
                break
            prev = view.line(max(0, tail_start - 1)).begin()
            if prev == tail_start:
                break
            tail_start = prev
        text = view.substr(sublime.Region(tail_start, size))
        out["tail_text"] = text[-800:]
        out["tail_has_marker"] = INPUT_MARKER.rstrip() in text or "◎" in text
        # count trailing blank lines after last non-empty
        lines = text.split("\n")
        trailing = 0
        for ln in reversed(lines):
            if ln.strip() == "":
                trailing += 1
            else:
                break
        out["trailing_empty_lines"] = trailing
        # find last ◎ line geometry
        marker_pt = text.rfind("◎")
        if marker_pt >= 0:
            abs_pt = tail_start + marker_pt
            out["marker_pt"] = abs_pt
            try:
                out["marker_layout"] = list(view.text_to_layout(abs_pt))
                out["eof_layout"] = list(view.text_to_layout(size))
            except Exception:
                pass
    except Exception as e:
        out["tail_error"] = str(e)

    # Regions / phantoms summary
    try:
        out["regions"] = {
            k: [(r.a, r.b) for r in view.get_regions(k)]
            for k in ("claude_queue", "claude_input", "claude_context", "mark")
            if view.get_regions(k)
        }
    except Exception as e:
        out["regions_error"] = str(e)

    return out


def _windows_brief() -> List[dict]:
    rows = []
    for w in sublime.windows():
        av = w.active_view()
        rows.append({
            "id": w.id(),
            "folders": w.folders()[:4],
            "active_view_id": av.id() if av else None,
            "active_name": av.name() if av else None,
            "claude_active_view": w.settings().get("claude_active_view"),
            "view_count": len(w.views()),
            "claude_views": sum(
                1 for v in w.views() if v.settings().get("claude_output")
            ),
        })
    return rows
