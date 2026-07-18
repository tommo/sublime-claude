"""Host goal skeptic helpers.

POC architecture (default):
  Main session = goal flow executor (single sheet).
  Plan / worker / reviewer = Task (or spawn_subagent) *under* that session.

Legacy (opt-in settings ``goal_skeptic_mode: session``):
  Separate ST session sheet via create_session — discouraged.

Grok Build analogy: harness-internal general-purpose subagents, not peer
product sessions.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

GOAL_ROLE_SKEPTIC = "skeptic"
# Default POC path
MODE_TASK = "task"
MODE_SESSION = "session"  # legacy sheet


def resolve_skeptic_mode(settings: Any = None) -> str:
    """Return ``task`` (default) or ``session`` (legacy sheets)."""
    mode = MODE_TASK
    try:
        if settings is None:
            import sublime  # type: ignore
            settings = sublime.load_settings("ClaudeCode.sublime-settings")
        raw = (settings.get("goal_skeptic_mode") or MODE_TASK) if settings else MODE_TASK
        mode = str(raw).strip().lower() or MODE_TASK
    except Exception:
        mode = MODE_TASK
    if mode not in (MODE_TASK, MODE_SESSION):
        return MODE_TASK
    return mode


def make_skeptic_context(parent_view_id: int, verify_run: int = 1) -> Dict[str, Any]:
    """initial_context for legacy sheet skeptic only."""
    sid = f"goal-skeptic-{uuid.uuid4().hex[:8]}"
    return {
        "subsession_id": sid,
        "parent_view_id": parent_view_id,
        "goal_parent_view_id": parent_view_id,
        "goal_role": GOAL_ROLE_SKEPTIC,
        "goal_verify_run": int(verify_run or 1),
    }


def is_goal_skeptic(session_like: Any) -> bool:
    role = getattr(session_like, "goal_role", None)
    if role is None:
        ctx = getattr(session_like, "initial_context", None) or {}
        if isinstance(ctx, dict):
            role = ctx.get("goal_role")
    return (role or "").strip().lower() == GOAL_ROLE_SKEPTIC


def parent_view_id_for_skeptic(session_like: Any) -> Optional[int]:
    pvid = getattr(session_like, "parent_view_id", None)
    if pvid is None:
        ctx = getattr(session_like, "initial_context", None) or {}
        if isinstance(ctx, dict):
            pvid = ctx.get("goal_parent_view_id") or ctx.get("parent_view_id")
    if pvid is None:
        return None
    try:
        return int(pvid)
    except (TypeError, ValueError):
        return None


def resolve_verdict_session(
    caller: Any,
    sessions: Dict[Any, Any],
) -> Any:
    """Map MCP caller → session that owns the goal (parent if sheet-skeptic)."""
    if caller is None:
        return None
    if is_goal_skeptic(caller):
        pvid = parent_view_id_for_skeptic(caller)
        if pvid is not None and pvid in sessions:
            return sessions[pvid]
        return None
    return caller


def skeptic_display_name(verify_run: int) -> str:
    return f"goal-skeptic-{int(verify_run or 1)}"


def prepare_task_verify_abort(gt: Any, gap: str, message: str = "") -> bool:
    """Fail-closed prep for Task-mode verify on error/interrupt.

    If ``gt.phase == verifying`` and no tool verdict yet, records
    not-achieved. Returns True when the host should run finish cycle.
    """
    if gt is None:
        return False
    if getattr(gt, "phase", None) != "verifying":
        return False
    if not getattr(gt, "pending_tool_verdict", None):
        gt.record_tool_verdict(
            achieved=False,
            evidence=[],
            gaps=[(gap or "Host: verify aborted")[:200]],
            message=(message or gap or "aborted")[:200],
        )
    return True


def preserve_working_after_verify_finish(finish_started_next: bool) -> bool:
    """True → completion handler must return early (do not force working=False).

    When ``_goal_finish_verify_cycle`` returns True it already started the
    next turn (continuation query). The non-success gate must not clobber
    that busy state.
    """
    return bool(finish_started_next)
