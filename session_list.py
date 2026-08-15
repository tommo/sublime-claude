"""Scratch view: live + saved sessions, jump to focus or resume."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import sublime
import sublime_plugin

from .session import load_saved_sessions, load_bookmarks, remove_saved_session


SETTING = "claude_session_list"
ROWS_KEY = "claude_session_list_rows"
HISTORY_CAP = 40
# Full row needs ~backend(7) + title(16+) + status/time. Below this, abbrev.
COMPACT_COLS = 56


def one_line_title(name: str, limit: int = 200) -> str:
    """Keep a list row on one line; show ↵ where the name had a newline."""
    text = (name or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", "↵")
    text = " ".join(text.split())
    return text[:limit].strip()


def _name_from_prompt(prompt: str, limit: int = 200) -> str:
    return one_line_title(prompt, limit)


def session_title(session) -> str:
    """Full stored name, or first prompt if the name was the old 30-char cut."""
    name = (getattr(session, "name", None) or "").strip()
    if name.endswith("...") or name.endswith("…"):
        try:
            out = getattr(session, "output", None)
            convs = list(getattr(out, "conversations", None) or [])
            cur = getattr(out, "current", None)
            if cur is not None:
                convs.append(cur)
            for c in convs:
                p = getattr(c, "prompt", None) or ""
                one = _name_from_prompt(p)
                if one and len(one) > len(name.rstrip(".… ")):
                    return one
        except Exception:
            pass
    return one_line_title(name) or "(unnamed)"


def awaiting_input(session) -> bool:
    """True while a permission, question, or plan UI is waiting on the user."""
    out = getattr(session, "output", None)
    if not out:
        return False
    for attr in ("pending_permission", "pending_question", "pending_plan"):
        req = getattr(out, attr, None)
        if req is not None and getattr(req, "callback", None):
            return True
    return False


def _status_of(session) -> str:
    if getattr(session, "is_sleeping", False):
        return "sleeping"
    if awaiting_input(session):
        return "input"
    # Kimi /compact: session/prompt returns end_turn immediately while
    # compaction continues. working can drop; _compacting is the live flag.
    if getattr(session, "working", False) or getattr(session, "_compacting", False):
        return "working"
    if getattr(session, "unread", False):
        return "unread"
    return "ready"


def access_ts(obj) -> float:
    """Most recent focus or activity. `obj` is a live session or a row/saved dict."""
    if obj is None:
        return 0.0
    getter = obj.get if isinstance(obj, dict) else lambda k, d=0: getattr(obj, k, d)
    try:
        acc = float(getter("last_access", 0) or 0)
    except (TypeError, ValueError):
        acc = 0.0
    if acc > 0:
        return acc
    try:
        return float(getter("last_activity", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _mark(status: str) -> str:
    return {
        "input": "?",
        "unread": "*",
        "working": "●",
        "sleeping": "⏸",
        "ready": "○",
    }.get(status, "·")


def backend_abbrev(backend: str) -> str:
    from .backends import abbrev_for
    return abbrev_for(backend)


def view_cols(view, fallback: int = 80) -> int:
    """Visible columns for the Sessions scratch (viewport / em, minus slack)."""
    if not view:
        return fallback
    try:
        vw = float(view.viewport_extent()[0])
        em = float(view.em_width() or 0)
        if em <= 0 or vw < 8:
            return fallback
        try:
            margin = float(view.settings().get("margin") or 0)
        except Exception:
            margin = 0
        usable = max(em, vw - 2 * margin)
        return max(24, int(usable / em) - 1)
    except Exception:
        return fallback


def use_compact(cols: int) -> bool:
    return 0 < cols < COMPACT_COLS


def format_header(cols: int = 0) -> str:
    left = "SESSIONS"
    right = "enter open · v reveal · r refresh · del close"
    if not cols:
        return f"{left}                  {right}"
    for cand in (right, "↵ open · v · r · del", "v · r · del", "r · del", "r refresh"):
        if cols >= len(left) + 1 + len(cand):
            right = cand
            break
    else:
        right = ""
    if not right:
        return left[:cols]
    gap = max(1, cols - len(left) - len(right))
    line = f"{left}{' ' * gap}{right}"
    return line[:cols] if len(line) > cols else line


def _name_budget(prefix: str, extra: str, cols: int, compact: bool) -> int:
    if cols <= 0:
        return 48 if compact else 40
    return max(8, cols - len(prefix) - len(extra))


def fit_title(name: str, width: int) -> str:
    """Pad or ellipsize a session title to exactly `width` columns."""
    name = name or ""
    if width <= 0:
        return ""
    if len(name) <= width:
        return f"{name:<{width}}"
    if width == 1:
        return "…"
    return name[: width - 1] + "…"


def format_when(ts, now=None) -> str:
    """Elapsed since last access: now / 12s / 5m / 3h / 2d / 4w."""
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    try:
        now_t = time.time() if now is None else float(now)
    except (TypeError, ValueError):
        now_t = time.time()
    sec = max(0, int(now_t - t))
    if sec < 5:
        return "now"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    if sec < 86400 * 14:
        return f"{sec // 86400}d"
    return f"{sec // (86400 * 7)}w"


def window_project(window) -> str:
    """This window's project root (first folder). Empty if none."""
    try:
        folders = window.folders() if window else None
    except Exception:
        folders = None
    if folders:
        return (folders[0] or "").rstrip("/")
    return ""


def session_project(session) -> str:
    return window_project(getattr(session, "window", None))


def belongs_to_window(session, window) -> bool:
    """True when the session is this window's project (or this window if none)."""
    want = window_project(window)
    got = session_project(session)
    if want:
        if got:
            return got == want
        return getattr(session, "window", None) == window
    return getattr(session, "window", None) == window


def collect_live(window) -> List[dict]:
    out = []
    try:
        from .session_registry import iter_sessions
        live_sessions = iter_sessions()
    except Exception:
        live_sessions = list((getattr(sublime, "_claude_sessions", None) or {}).values())
    for s in live_sessions:
        if getattr(s, "quick_mode", False):
            continue
        if not belongs_to_window(s, window):
            continue
        try:
            view = s.output.view if s.output else None
            view_ok = bool(view and view.is_valid())
            view_id = view.id() if view_ok else id(s)
        except Exception:
            view_ok = False
            view_id = id(s)
        out.append({
            "kind": "live",
            "session_id": getattr(s, "session_id", None),
            "view_id": view_id,
            "name": session_title(s),
            "backend": getattr(s, "backend", None) or "claude",
            "status": _status_of(s),
            "query_count": int(getattr(s, "query_count", 0) or 0),
            "same_window": True,
            "last_access": access_ts(s),
            "last_activity": float(getattr(s, "last_activity", 0) or 0),
        })
    # Input wait first, then awake, then sleeping; access time within each band.
    _band = {"input": 0, "unread": 0, "working": 1, "ready": 1, "sleeping": 2}
    out.sort(key=lambda r: (
        _band.get(r.get("status"), 1),
        -access_ts(r),
    ))
    return out


def collect_history(live_ids: set, cwd: str) -> Tuple[List[dict], List[dict]]:
    here, other = [], []
    cwd = (cwd or "").rstrip("/")
    for s in load_saved_sessions():
        sid = s.get("session_id")
        if not sid or sid in live_ids:
            continue
        row = {
            "kind": "saved",
            "session_id": sid,
            "view_id": None,
            "name": one_line_title(s.get("name") or "") or "(unnamed)",
            "backend": s.get("backend") or "claude",
            "status": s.get("state") or "closed",
            "query_count": int(s.get("query_count") or 0),
            "project": s.get("project") or "",
            "last_activity": s.get("last_activity"),
            "last_access": access_ts(s),
        }
        proj = (row["project"] or "").rstrip("/")
        if cwd and proj == cwd:
            here.append(row)
        else:
            other.append(row)
    here.sort(key=access_ts, reverse=True)
    other.sort(key=access_ts, reverse=True)
    return here[:HISTORY_CAP], other[:HISTORY_CAP]


def _right_meta(r: dict) -> str:
    """One stamp column: ready/working if awake, elapsed if sleeping/history."""
    live = r.get("kind") == "live"
    status = r.get("status") or ""
    if live and status not in ("", "sleeping"):
        stamp = status
    else:
        stamp = format_when(r.get("last_access") or r.get("last_activity"))
    q = f"{r['query_count']}q" if r.get("query_count") else ""
    extra = f" {q:>4} {stamp:>7}"
    return extra


def _fmt_row(r: dict, starred: set, compact: bool = False, cols: int = 0) -> str:
    live = r.get("kind") == "live"
    star = "★ " if r.get("session_id") in starred else ""
    name = one_line_title(r.get("name") or "")
    mark = _mark(r["status"]) if live else "·"
    if compact:
        pre = f"{mark} {star}{backend_abbrev(r.get('backend'))} "
        return pre + fit_title(name, _name_budget(pre, "", cols, True))
    extra = _right_meta(r)
    pre = f"{mark} {star}{r['backend']:<7} "
    return pre + fit_title(name, _name_budget(pre, extra, cols, False)) + extra


def render_list(live: List[dict], here: List[dict], other: List[dict],
                starred: Optional[set] = None,
                cols: int = 0) -> Tuple[str, List[dict]]:
    starred = starred or set()
    compact = use_compact(cols)
    lines = [
        format_header(cols),
        "",
    ]
    index: List[dict] = []

    def add_section(title: str, rows: List[dict], fmt):
        lines.append(f"{title} ({len(rows)})")
        if not rows:
            lines.append("  (none)")
            lines.append("")
            return
        for r in rows:
            lines.append(fmt(r, starred, compact, cols))
            rec = dict(r)
            rec["line"] = len(lines)  # 1-based
            index.append(rec)
        lines.append("")

    add_section("RUNNING", live, _fmt_row)
    add_section("HISTORY", here, _fmt_row)
    return "\n".join(lines).rstrip() + "\n", index


def build_for_window(window, cols: int = 0) -> Tuple[str, List[dict]]:
    cwd = ""
    if window and window.folders():
        cwd = window.folders()[0]
    live = collect_live(window)
    live_ids = {r["session_id"] for r in live if r.get("session_id")}
    here, _other = collect_history(live_ids, cwd)
    starred = load_bookmarks(cwd or None)
    return render_list(live, here, [], starred, cols=cols)


def row_at_line(index: List[dict], line: int) -> Optional[dict]:
    for r in index:
        if r.get("line") == line:
            return r
    return None


def focus_live(window, row: dict) -> bool:
    vid = row.get("view_id")
    sid = row.get("session_id")
    sessions = getattr(sublime, "_claude_sessions", None) or {}
    session = None
    if vid is not None and vid in sessions:
        session = sessions[vid]
    if session is None and sid:
        try:
            from .session_registry import find_live_by_session_id
            session = find_live_by_session_id(sid)
        except Exception:
            session = None
        if session is None:
            for s in sessions.values():
                if getattr(s, "session_id", None) == sid:
                    session = s
                    break
    if session is None:
        return False
    view = session.output.view if session.output else None
    if not view or not view.is_valid():
        return reveal_live_session(window, session)
    win = view.window() or window
    win.focus_view(view)
    try:
        from .session_split import remember_active_session
        remember_active_session(win, view)
    except Exception:
        win.settings().set("claude_active_view", view.id())
    reveal_session_bottom(session)
    return True


def reveal_live_session(window, session, focus: bool = True) -> bool:
    """Show a live session, reattaching a sheet if it was backgrounded."""
    if not session or not window:
        return False
    view = session.output.view if session.output else None
    if view and view.is_valid():
        win = view.window() or window
        prev = None if focus else win.active_view()
        win.focus_view(view)
        if focus:
            try:
                from .session_split import remember_active_session
                remember_active_session(win, view)
            except Exception:
                pass
        elif prev and prev.is_valid() and prev.id() != view.id():
            win.focus_view(prev)
        reveal_session_bottom(session)
        return True
    session.window = window
    if session.output:
        session.output.window = window
        session.output.view = None
    try:
        session.reset_phantoms_for_new_view()
    except Exception:
        pass
    if not session.output:
        return False
    session.output.show(focus=focus)
    view = session.output.view
    if not view or not view.is_valid():
        return False
    try:
        from .session_split import place_in_last_session_split, remember_active_session
        place_in_last_session_split(window, view)
        if focus:
            window.focus_view(view)
            remember_active_session(window, view)
    except Exception:
        pass
    try:
        from .session_registry import register_session
        register_session(session)
    except Exception:
        pass
    try:
        if session.backend and session.backend != "claude":
            from . import backends
            spec = backends.get(session.backend)
            view.settings().set("claude_backend", session.backend)
            if getattr(spec, "theme", None):
                view.settings().set("color_scheme", spec.theme)
    except Exception:
        pass
    try:
        session.output.set_name(session.display_name)
    except Exception:
        pass
    reveal_session_bottom(session)
    if focus and not session.working:
        try:
            session._enter_input_with_draft()
        except Exception:
            pass
    return True


def reveal_session_bottom(session) -> None:
    """Scroll to ◎ / EOF. Never wake a sleeping session."""
    out = getattr(session, "output", None)
    view = out.view if out else None
    if not view or not view.is_valid():
        return
    try:
        if out.is_input_mode():
            out.scroll_composer_chrome(force=True)
            return
    except Exception:
        pass
    try:
        end = view.size()
        view.show(sublime.Region(end, end), False)
        _x, y = view.text_to_layout(end)
        vh = float(view.viewport_extent()[1])
        vx, _vy = view.viewport_position()
        view.set_viewport_position((float(vx), max(0.0, float(y) - vh + 24.0)), False)
    except Exception:
        try:
            view.show(view.size())
        except Exception:
            pass


def resume_saved(window, row: dict, focus: bool = True) -> bool:
    from .core import create_session
    sid = row.get("session_id")
    if not sid:
        return False
    try:
        from .session_registry import find_live_by_session_id
        live = find_live_by_session_id(sid)
        if live and reveal_live_session(window, live, focus=focus):
            return True
    except Exception:
        pass
    backend = row.get("backend") or "claude"
    s = create_session(window, resume_id=sid, backend=backend, focus=focus)
    name = row.get("name")
    if s and name and name != "(unnamed)":
        s.name = name
        if s.output:
            s.output.set_name(name)
    return bool(s)


_last_open = (0.0, None)


def open_row(window, row: dict) -> bool:
    if not row:
        return False
    global _last_open
    key = (row.get("session_id"), row.get("view_id"), row.get("kind"))
    now = time.time()
    if key == _last_open[1] and (now - _last_open[0]) < 0.4:
        return True
    _last_open = (now, key)
    if row.get("kind") == "live":
        if focus_live(window, row):
            return True
    return resume_saved(window, row)


def reveal_row(window, row: dict) -> bool:
    """Show the session sheet but keep keyboard focus on the list."""
    if not row or not window:
        return False
    keep = window.active_view()
    ok = False
    if row.get("kind") == "live":
        session = _live_session_for_row(row)
        if session is not None:
            ok = reveal_live_session(window, session, focus=False)
    if not ok:
        ok = resume_saved(window, row, focus=False)
    if keep and keep.is_valid():
        try:
            window.focus_view(keep)
        except Exception:
            pass
    return ok


def _live_session_for_row(row: dict):
    if not row:
        return None
    sessions = getattr(sublime, "_claude_sessions", None) or {}
    vid = row.get("view_id")
    if vid is not None and vid in sessions:
        return sessions.get(vid)
    sid = row.get("session_id")
    if sid:
        try:
            from .session_registry import find_live_by_session_id
            return find_live_by_session_id(sid)
        except Exception:
            pass
        for s in sessions.values():
            if getattr(s, "session_id", None) == sid:
                return s
    return None


def close_row(window, row: dict) -> bool:
    """Stop a live session or drop a history entry. Removes it from the list."""
    if not row:
        return False
    sid = row.get("session_id")
    if row.get("kind") == "live":
        session = _live_session_for_row(row)
        if session:
            view = None
            try:
                view = session.output.view if session.output else None
            except Exception:
                view = None
            try:
                session.stop()
            except Exception:
                pass
            try:
                from .session_registry import unregister_view, ensure_registries
                ensure_registries()
                if view:
                    unregister_view(view.id())
                aid = getattr(session, "agent_id", None)
                bg = getattr(sublime, "_claude_background", None)
                if aid and isinstance(bg, dict):
                    bg.pop(aid, None)
            except Exception:
                pass
            if view:
                try:
                    if view.is_valid():
                        view.settings().set("claude_soft_close", True)
                        view.close()
                except Exception:
                    pass
        if sid:
            try:
                remove_saved_session(sid)
            except Exception:
                pass
        return True
    if sid:
        return bool(remove_saved_session(sid))
    return False


class SessionListView:
    def __init__(self, window):
        self.window = window
        self.view = None
        self._create()

    def _apply_chrome(self):
        st = self.view.settings()
        st.set(SETTING, True)
        st.set("command_mode", False)
        st.set("word_wrap", False)
        st.set("gutter", False)
        st.set("line_numbers", False)
        st.set("margin", 12)
        st.set("scroll_past_end", False)
        st.set("highlight_line", True)
        st.set("font_size", 10)
        st.set("color_scheme",
               "Packages/ClaudeCode/SessionList.hidden-color-scheme")
        self.view.assign_syntax(
            "Packages/ClaudeCode/SessionList.sublime-syntax")

    def _create(self):
        for v in self.window.views():
            if v.settings().get(SETTING):
                self.view = v
                break
        if not self.view:
            self.view = self.window.new_file()
            self.view.set_name("Sessions")
            self.view.set_scratch(True)
            self.view.set_read_only(True)
        self._apply_chrome()
        self.refresh()
        self.window.focus_view(self.view)

    def refresh(self):
        if not self.view or not self.view.is_valid():
            return
        cols = view_cols(self.view)
        text, index = build_for_window(self.window, cols=cols)
        cur = self.view.substr(sublime.Region(0, self.view.size()))
        keep_sid = None
        keep_kind = None
        if self.view.sel():
            line = self.view.rowcol(self.view.sel()[0].begin())[0] + 1
            try:
                old = json.loads(self.view.settings().get(ROWS_KEY) or "[]")
            except Exception:
                old = []
            hit = row_at_line(old, line)
            if hit:
                keep_sid = hit.get("session_id")
                keep_kind = hit.get("kind")
        self.view.settings().set(ROWS_KEY, json.dumps(index))
        if text == cur:
            return
        self.view.set_read_only(False)
        self.view.run_command("select_all")
        self.view.run_command("left_delete")
        self.view.run_command("append", {"characters": text})
        self.view.set_read_only(True)
        target = None
        if keep_sid:
            for rec in index:
                if rec.get("session_id") == keep_sid and rec.get("kind") == keep_kind:
                    target = rec
                    break
        if target is None and self.view.sel() and self.view.rowcol(
                self.view.sel()[0].begin())[0] < 3 and index:
            target = index[0]
        if target:
            pt = self.view.text_point(max(0, int(target["line"]) - 1), 0)
            self.view.sel().clear()
            self.view.sel().add(sublime.Region(pt))
            self.view.show(pt)


def show_session_list(window) -> Optional[SessionListView]:
    if not window:
        return None
    for v in window.views():
        if v.settings().get(SETTING) and v.is_valid():
            sl = SessionListView.__new__(SessionListView)
            sl.window = window
            sl.view = v
            sl._apply_chrome()
            sl.refresh()
            window.focus_view(v)
            _arm_session_list_poll()
            return sl
    sl = SessionListView(window)
    _arm_session_list_poll()
    return sl


class SessionListClickListener(sublime_plugin.EventListener):
    """Dclick is Default `drag_select` by=words; letter keys often fall through to insert."""

    def on_query_context(self, view, key, operator, operand, match_all):
        if key != "claude_session_list":
            return None
        val = bool(view and view.settings().get(SETTING))
        if operator == sublime.OP_EQUAL:
            return val == bool(operand)
        if operator == sublime.OP_NOT_EQUAL:
            return val != bool(operand)
        return val

    def on_text_command(self, view, name, args):
        if not view or not view.settings().get(SETTING):
            return None
        if name in ("left_delete", "right_delete"):
            return ("claude_session_list_close", {})
        if name != "insert":
            return None
        ch = (args or {}).get("characters") or ""
        if ch == "r":
            return ("claude_session_list_refresh", {})
        if ch == "v":
            return ("claude_session_list_reveal", {})
        if ch in ("\n", "\r"):
            return ("claude_session_list_open", {})
        return None

    def on_post_text_command(self, view, name, args):
        if not view or not view.settings().get(SETTING):
            return
        if name != "drag_select":
            return
        args = args or {}
        if args.get("by") != "words":
            return
        line = 0
        if view.sel():
            line = view.rowcol(view.sel()[0].begin())[0] + 1
        if line <= 1:
            return
        view.run_command("claude_session_list_open")


def refresh_session_list(window) -> None:
    if not window:
        return
    for v in window.views():
        if v.settings().get(SETTING) and v.is_valid():
            sl = SessionListView.__new__(SessionListView)
            sl.window = window
            sl.view = v
            sl.refresh()
            return


def _session_list_open() -> bool:
    try:
        for w in sublime.windows():
            for v in w.views():
                if v.settings().get(SETTING) and v.is_valid():
                    return True
    except Exception:
        pass
    return False


def refresh_all_session_lists() -> None:
    try:
        for w in sublime.windows():
            refresh_session_list(w)
    except Exception:
        pass


_refresh_pending = False
_poll_armed = False


def schedule_session_list_refresh() -> None:
    """Debounced refresh of every open Sessions scratch view."""
    global _refresh_pending
    if _refresh_pending:
        return
    if not _session_list_open():
        return
    _refresh_pending = True

    def _go():
        global _refresh_pending
        _refresh_pending = False
        refresh_all_session_lists()

    sublime.set_timeout(_go, 250)
    _arm_session_list_poll()


def _arm_session_list_poll() -> None:
    global _poll_armed
    if _poll_armed:
        return
    _poll_armed = True
    sublime.set_timeout(_session_list_poll, 900)


def _session_list_poll() -> None:
    global _poll_armed
    if not _session_list_open():
        _poll_armed = False
        return
    refresh_all_session_lists()
    sublime.set_timeout(_session_list_poll, 900)


class ClaudeSessionListCloseCommand(sublime_plugin.TextCommand):
    """Delete/close the session under the caret in the Sessions list."""

    def run(self, edit):
        import json
        if not self.view.settings().get(SETTING):
            return
        raw = self.view.settings().get(ROWS_KEY) or "[]"
        try:
            index = json.loads(raw)
        except Exception:
            index = []
        sel = self.view.sel()
        if not sel:
            return
        line = self.view.rowcol(sel[0].begin())[0] + 1
        row = row_at_line(index, line)
        win = self.view.window()
        if not win or not row:
            return
        name = (row.get("name") or "").strip() or "session"
        if close_row(win, row):
            refresh_session_list(win)
            try:
                pt = self.view.text_point(max(0, line - 1), 0)
                self.view.sel().clear()
                self.view.sel().add(pt)
            except Exception:
                pass
            sublime.status_message("Claude: closed {}".format(name))

    def is_enabled(self):
        return bool(self.view.settings().get(SETTING))


class ClaudeSessionListRevealCommand(sublime_plugin.TextCommand):
    """Show the session under the caret; keep focus on the Sessions list."""

    def run(self, edit):
        import json
        if not self.view.settings().get(SETTING):
            return
        raw = self.view.settings().get(ROWS_KEY) or "[]"
        try:
            index = json.loads(raw)
        except Exception:
            index = []
        sel = self.view.sel()
        if not sel:
            return
        line = self.view.rowcol(sel[0].begin())[0] + 1
        row = row_at_line(index, line)
        win = self.view.window()
        if not win or not row:
            return
        if reveal_row(win, row):
            try:
                win.focus_view(self.view)
            except Exception:
                pass

    def is_enabled(self):
        return bool(self.view.settings().get(SETTING))

