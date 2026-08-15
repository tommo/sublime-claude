"""Stable agent identity + runtime view_id mapping.

view_id is a Sublime runtime handle — it changes on restart and must not be
the public identity for MCP tools. agent_id is host-stable (persisted on the
view and in sessions.json). Registry maps:

  _claude_sessions: view_id (int) -> Session   # ST lookup
  _claude_agents:   agent_id (str) -> view_id  # stable → runtime
  _claude_background: agent_id (str) -> Session  # live, no sheet
"""
from __future__ import annotations

import re
import uuid
from typing import Optional, Any, List

import sublime


def new_agent_id() -> str:
    return f"agent-{uuid.uuid4().hex[:12]}"


def ensure_registries() -> None:
    if not hasattr(sublime, "_claude_sessions") or sublime._claude_sessions is None:
        sublime._claude_sessions = {}
    if not hasattr(sublime, "_claude_agents") or sublime._claude_agents is None:
        sublime._claude_agents = {}
    if not hasattr(sublime, "_claude_background") or sublime._claude_background is None:
        sublime._claude_background = {}


def clear_registries() -> None:
    """Drop all mappings (plugin reload / hard reset)."""
    ensure_registries()
    sublime._claude_sessions.clear()
    sublime._claude_agents.clear()
    sublime._claude_background.clear()


def register_session(session: Any) -> None:
    """Bind session into view_id and agent_id maps."""
    ensure_registries()
    view = getattr(getattr(session, "output", None), "view", None)
    if not view or not view.is_valid():
        return
    vid = view.id()
    aid = getattr(session, "agent_id", None)
    if not aid:
        aid = new_agent_id()
        session.agent_id = aid

    # Drop stale agent→view if this agent_id moved
    old_vid = sublime._claude_agents.get(aid)
    if old_vid is not None and old_vid != vid:
        old = sublime._claude_sessions.get(old_vid)
        if old is session or old is None:
            sublime._claude_sessions.pop(old_vid, None)

    # Drop previous agent_id pointing at this view
    for a, v in list(sublime._claude_agents.items()):
        if v == vid and a != aid:
            sublime._claude_agents.pop(a, None)

    sublime._claude_sessions[vid] = session
    sublime._claude_agents[aid] = vid
    sublime._claude_background.pop(aid, None)
    session.backgrounded = False

    try:
        relink_parent_view(session)
    except Exception:
        pass


def unregister_view(view_id: int) -> None:
    """Remove session for a closed/replaced view."""
    ensure_registries()
    session = sublime._claude_sessions.pop(view_id, None)
    if session is None:
        for a, v in list(sublime._claude_agents.items()):
            if v == view_id:
                sublime._claude_agents.pop(a, None)
        return
    aid = getattr(session, "agent_id", None)
    if aid and sublime._claude_agents.get(aid) == view_id:
        sublime._claude_agents.pop(aid, None)


def get_session_for_view_id(view_id: int) -> Optional[Any]:
    ensure_registries()
    return sublime._claude_sessions.get(view_id)


def get_session_by_agent_id(agent_id: str) -> Optional[Any]:
    """Resolve stable agent_id (also accepts subsession_id alias)."""
    if not agent_id:
        return None
    ensure_registries()
    aid = str(agent_id).strip()
    vid = sublime._claude_agents.get(aid)
    if vid is not None:
        s = sublime._claude_sessions.get(vid)
        if s is not None:
            return s
    bg = sublime._claude_background.get(aid)
    if bg is not None:
        return bg
    for s in sublime._claude_sessions.values():
        if getattr(s, "agent_id", None) == aid:
            register_session(s)
            return s
        if getattr(s, "subsession_id", None) == aid:
            register_session(s)
            return s
    for s in list(sublime._claude_background.values()):
        if getattr(s, "agent_id", None) == aid:
            return s
        if getattr(s, "subsession_id", None) == aid:
            return s
    return None


def iter_sessions() -> List[Any]:
    """All live sessions, including background (no sheet)."""
    ensure_registries()
    out = []
    seen = set()
    for s in list(sublime._claude_sessions.values()) + list(
            sublime._claude_background.values()):
        if s is None or id(s) in seen:
            continue
        seen.add(id(s))
        out.append(s)
    return out


def find_live_by_session_id(session_id: str) -> Optional[Any]:
    if not session_id:
        return None
    for s in iter_sessions():
        if getattr(s, "session_id", None) == session_id:
            return s
    return None


def close_or_detach_session(session: Any, view: Any = None) -> str:
    """Detach a live session, or stop a sleeping/disabled one.

    Returns 'detach' or 'stop'.
    """
    if keep_running_on_close(session):
        if view is not None:
            try:
                view.settings().set("claude_soft_close", True)
            except Exception:
                pass
        detach_session(session)
        return "detach"
    try:
        session.stop()
    except Exception:
        pass
    vid = None
    try:
        if view is not None:
            vid = view.id()
    except Exception:
        vid = None
    if vid is not None:
        unregister_view(vid)
    else:
        try:
            ov = getattr(session, "output", None)
            v = getattr(ov, "view", None) if ov else None
            if v:
                unregister_view(v.id())
        except Exception:
            pass
    return "stop"


def sessions_for_window(window: Any) -> List[Any]:
    if window is None:
        return []
    return [s for s in iter_sessions() if getattr(s, "window", None) == window]


def keep_running_on_close(session: Any) -> bool:
    """True when closing the sheet should detach, not kill the bridge."""
    if not session or getattr(session, "quick_mode", False):
        return False
    if getattr(session, "is_sleeping", False):
        return False
    try:
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        if settings.get("keep_running_on_close", True) is False:
            return False
    except Exception:
        pass
    if getattr(session, "client", None) is not None:
        return True
    if getattr(session, "working", False):
        return True
    return bool(getattr(session, "initialized", False))


def detach_session(session: Any) -> bool:
    """Drop the sheet, keep the live session in the background map."""
    if not session:
        return False
    ensure_registries()
    aid = getattr(session, "agent_id", None)
    if not aid:
        aid = new_agent_id()
        session.agent_id = aid
    view = getattr(getattr(session, "output", None), "view", None)
    vid = None
    try:
        if view and view.is_valid():
            vid = view.id()
    except Exception:
        vid = None
    if vid is not None:
        if sublime._claude_sessions.get(vid) is session:
            sublime._claude_sessions.pop(vid, None)
        if sublime._claude_agents.get(aid) == vid:
            sublime._claude_agents.pop(aid, None)
    try:
        if hasattr(session, "reset_phantoms_for_new_view"):
            session.reset_phantoms_for_new_view()
    except Exception:
        pass
    if session.output:
        session.output.view = None
        try:
            session.output._input_mode = False
        except Exception:
            pass
    session.backgrounded = True
    sublime._claude_background[aid] = session
    try:
        from .session_list import schedule_session_list_refresh
        schedule_session_list_refresh()
    except Exception:
        pass
    return True


def get_session_by_ref(ref) -> Optional[Any]:
    """Resolve agent_id (str) or view_id (int / digit-string).

    Prefer agent_id for all agent-facing tools. view_id kept for legacy.
    """
    if ref is None or ref == "":
        return None
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return get_session_for_view_id(ref)
    s = str(ref).strip()
    if not s:
        return None
    # Pure digits → treat as view_id (legacy)
    if s.isdigit():
        return get_session_for_view_id(int(s))
    return get_session_by_agent_id(s)


def relink_parent_view(session: Any) -> Optional[int]:
    """Refresh session.parent_view_id from parent_agent_id. Returns view_id or None."""
    parent = resolve_parent_session(session)
    if not parent:
        return getattr(session, "parent_view_id", None)
    try:
        pview = parent.output.view if parent.output else None
        if pview and pview.is_valid():
            session.parent_view_id = pview.id()
            return session.parent_view_id
    except Exception:
        pass
    return getattr(session, "parent_view_id", None)


def resolve_parent_session(child: Any) -> Optional[Any]:
    """Find parent session via stable parent_agent_id, else parent_view_id."""
    paid = getattr(child, "parent_agent_id", None)
    if paid:
        p = get_session_by_agent_id(paid)
        if p is not None:
            return p
    pvid = getattr(child, "parent_view_id", None)
    if pvid is not None:
        try:
            return get_session_for_view_id(int(pvid))
        except (TypeError, ValueError):
            pass
    return None


def is_child_of(session: Any, parent_view_id: int = None, parent_agent_id: str = None) -> bool:
    """True if session is a subsession of the given parent."""
    if parent_agent_id:
        if getattr(session, "parent_agent_id", None) == parent_agent_id:
            return True
    if parent_view_id is not None:
        if getattr(session, "parent_view_id", None) == parent_view_id:
            return True
        # parent may have been re-bound to a new view_id
        if parent_agent_id is None:
            parent = get_session_for_view_id(parent_view_id)
            if parent:
                paid = getattr(parent, "agent_id", None)
                if paid and getattr(session, "parent_agent_id", None) == paid:
                    return True
    # Has parent linkage at all when filtering "any child"?
    return False


def list_children_of(parent_view_id: int = None, parent_agent_id: str = None) -> List[Any]:
    ensure_registries()
    if parent_view_id is not None and not parent_agent_id:
        parent = get_session_for_view_id(parent_view_id)
        if parent:
            parent_agent_id = getattr(parent, "agent_id", None)
    out = []
    for vid, s in sublime._claude_sessions.items():
        if parent_view_id is not None and vid == parent_view_id:
            continue
        if is_child_of(s, parent_view_id=parent_view_id, parent_agent_id=parent_agent_id):
            # Keep parent_view_id cache current
            relink_parent_view(s)
            out.append(s)
    return out


def relink_all_parents() -> int:
    """After multi-tab restore, re-resolve parent_view_id for all children."""
    ensure_registries()
    n = 0
    for s in list(sublime._claude_sessions.values()):
        if getattr(s, "parent_agent_id", None) or getattr(s, "parent_view_id", None):
            before = getattr(s, "parent_view_id", None)
            after = relink_parent_view(s)
            if after and after != before:
                n += 1
            register_session(s)
    return n


def runtime_view_id(session: Any) -> Optional[int]:
    try:
        v = session.output.view if session.output else None
        if v and v.is_valid():
            return v.id()
    except Exception:
        pass
    return None


# ── Host-local wait_for_subsession (no daemon required) ──────────────────
# notalone2 can also receive signal_complete, but MCP signal_complete used to
# inject the parent only and never fired the daemon — waiters hung forever.
# Keyed by stable agent_id / subsession_id.

def ensure_waits() -> dict:
    if not hasattr(sublime, "_claude_subsession_waits") or sublime._claude_subsession_waits is None:
        sublime._claude_subsession_waits = {}  # key -> list[dict]
    return sublime._claude_subsession_waits


def register_subsession_wait(
    *,
    child_id: str,
    parent_view_id: int = None,
    parent_agent_id: str = None,
    wake_prompt: str = "",
) -> str:
    """Register a parent waiter for child agent_id/subsession_id. Returns wait_id."""
    import time
    waits = ensure_waits()
    key = str(child_id).strip()
    wid = f"wait-{uuid.uuid4().hex[:10]}"
    entry = {
        "wait_id": wid,
        "child_id": key,
        "parent_view_id": parent_view_id,
        "parent_agent_id": parent_agent_id,
        "wake_prompt": wake_prompt or "",
        "created": time.time(),
    }
    waits.setdefault(key, []).append(entry)
    return wid


def pop_subsession_waits(child_ids) -> list:
    """Remove and return all waiters matching any of the child id aliases."""
    waits = ensure_waits()
    out = []
    seen = set()
    for cid in child_ids:
        if not cid:
            continue
        key = str(cid).strip()
        for entry in waits.pop(key, []) or []:
            wid = entry.get("wait_id")
            if wid in seen:
                continue
            seen.add(wid)
            out.append(entry)
    return out


_AGENT_ID_RE = re.compile(r"agent-[0-9a-f]{8,}", re.I)


def subsession_notify_key(prompt: str) -> str:
    """Stable key for one child-completion notify (dedupe queue entries)."""
    if not prompt:
        return ""
    m = _AGENT_ID_RE.search(prompt)
    return ("subsession:" + m.group(0).lower()) if m else ""


def is_stock_subsession_wake(prompt: str) -> bool:
    s = (prompt or "").lstrip()
    return s.startswith("✅ Subsession ") or s.startswith("Subsession ")


def merge_subsession_queue(queued: list, incoming: str) -> list:
    """At most one completion notify per child. Prefer custom wait text."""
    incoming = (incoming or "").strip()
    if not incoming:
        return list(queued or [])
    key = subsession_notify_key(incoming)
    if not key:
        return list(queued or []) + [incoming]
    out = []
    replaced = False
    for p in queued or []:
        if subsession_notify_key(p) != key:
            out.append(p)
            continue
        if is_stock_subsession_wake(incoming) and not is_stock_subsession_wake(p):
            out.append(p)
        else:
            out.append(incoming)
        replaced = True
    if not replaced:
        out.append(incoming)
    return out


def child_parent_already_notified(child_session: Any) -> bool:
    return bool(getattr(child_session, "_parent_notified", False))


def parent_notify_should_inject(n_waits: int, child_session: Any = None) -> bool:
    """False when this delivery already went through a waiter.

    Only this completion: waiter query/queue XOR default inject — not a
    lifetime lock. Next signal_complete is a new delivery.
    """
    return int(n_waits or 0) <= 0


def mark_child_parent_notified(child_session: Any) -> None:
    try:
        child_session._parent_notified = True
    except Exception:
        pass


def fire_subsession_waits(
    child_session: Any,
    result_summary: str = None,
    default_body: str = None,
) -> int:
    """Deliver host-local wait_for_subsession prompts to parents. Returns count.

    default_body: richer signal_complete wake (budget + summary). Used when the
    registered wake_prompt is empty or the stock default, so parents get one
    complete notification instead of a thin stub + a second inject.
    """
    aliases = []
    for attr in ("agent_id", "subsession_id"):
        v = getattr(child_session, attr, None)
        if v:
            aliases.append(str(v))
    try:
        vid = runtime_view_id(child_session)
        if vid is not None:
            aliases.append(str(vid))
    except Exception:
        pass

    entries = pop_subsession_waits(aliases)
    if not entries:
        return 0

    # One delivery per parent even if multiple wait entries match aliases
    delivered_parents = set()
    n = 0
    for entry in entries:
        parent = None
        paid = entry.get("parent_agent_id")
        if paid:
            parent = get_session_by_agent_id(paid)
        if parent is None and entry.get("parent_view_id") is not None:
            parent = get_session_for_view_id(entry["parent_view_id"])
        if parent is None:
            parent = resolve_parent_session(child_session)
        if parent is None:
            print(
                f"[Claude] wait_for_subsession: no parent for "
                f"{entry.get('child_id')!r} wait={entry.get('wait_id')}"
            )
            continue

        parent_key = (
            getattr(parent, "agent_id", None)
            or runtime_view_id(parent)
            or id(parent)
        )
        if parent_key in delivered_parents:
            print(
                f"[Claude] wait_for_subsession: skip duplicate wait "
                f"{entry.get('wait_id')} for parent {parent_key!r}"
            )
            continue

        reg = (entry.get("wake_prompt") or "").rstrip()
        stock = (
            not reg
            or reg.startswith("✅ Subsession ")
        )
        if stock and default_body:
            body = default_body
        else:
            body = reg
            if result_summary:
                if body:
                    body = f"{body}\n\n{result_summary}"
                else:
                    body = result_summary
        if not body:
            body = (
                f"✅ Subsession {entry.get('child_id')} completed "
                f"(wait_for_subsession)"
            )

        try:
            if getattr(parent, "working", False):
                parent.queue_prompt(body)
            elif getattr(parent, "is_sleeping", False):
                parent._queued_prompts = merge_subsession_queue(
                    getattr(parent, "_queued_prompts", None) or [], body)
                parent.wake()
            else:
                parent.query(body, display_prompt="📬 Subsession complete")
            delivered_parents.add(parent_key)
            n += 1
            mark_child_parent_notified(child_session)
            print(
                f"[Claude] wait_for_subsession: fired {entry.get('wait_id')} "
                f"→ parent agent={getattr(parent, 'agent_id', None)}"
            )
        except Exception as e:
            print(f"[Claude] wait_for_subsession fire failed: {e}")
    return n
