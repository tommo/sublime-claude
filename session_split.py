"""Remember last active session split; land resumed sheets there."""
from typing import Optional


def remember_active_session(window, view) -> None:
    """Record last focused session + its split so resume can land there."""
    if not window or not view:
        return
    try:
        if not view.is_valid() or not view.settings().get("claude_output"):
            return
    except Exception:
        return
    window.settings().set("claude_active_view", view.id())
    try:
        group, _index = window.get_view_index(view)
    except Exception:
        return
    if group is None or group < 0:
        return
    window.settings().set("claude_active_group", int(group))


def last_session_group(window) -> Optional[int]:
    """Group index of the last active session sheet, if still valid."""
    if not window:
        return None
    vid = window.settings().get("claude_active_view")
    if vid is not None:
        try:
            for v in window.views():
                if v.id() == vid and v.is_valid():
                    group, _ = window.get_view_index(v)
                    if group is not None and group >= 0:
                        return int(group)
        except Exception:
            pass
    group = window.settings().get("claude_active_group")
    try:
        group = int(group)
    except (TypeError, ValueError):
        return None
    try:
        n = window.num_groups()
    except Exception:
        return None
    if n <= 0 or group < 0 or group >= n:
        return None
    return group


def place_in_last_session_split(window, view) -> bool:
    """Move a new sheet into the last active session's split."""
    if not window or not view:
        return False
    group = last_session_group(window)
    if group is None:
        return False
    try:
        if not view.is_valid():
            return False
        cur, _ = window.get_view_index(view)
        if cur == group:
            return False
        n_in = len(window.views_in_group(group))
        window.set_view_index(view, group, n_in)
        return True
    except Exception:
        return False
