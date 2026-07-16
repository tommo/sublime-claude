"""Plugin-owned goal harness state machine (backend-agnostic).

Reference: Grok Build goal mode contracts — model claims via update_goal;
host owns verification and continuation. Pure Python, no Sublime imports.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple


# Terminal / non-open for sticky strip
TERMINAL = frozenset({"complete", "cleared"})
PAUSED = frozenset({
    "user_paused", "blocked", "budget_limited", "infra_paused",
})
OPEN_STATUSES = frozenset({
    "active", "user_paused", "blocked", "budget_limited", "infra_paused",
})

DEFAULT_VERIFY_MAX = 5
DEFAULT_CONTINUE_MAX = 24  # host continues without verified complete
BLOCKED_STREAK_TO_PAUSE = 3
HISTORY_MAX = 64
GOAL_RESERVED = frozenset({"status", "pause", "resume", "clear", "edit"})


@dataclass
class GoalEvent:
    kind: str
    detail: str = ""
    ts: float = field(default_factory=time.time)


@dataclass
class GoalTracker:
    """Single active goal per session."""
    goal_id: str = ""
    objective: str = ""
    status: str = "cleared"  # no goal until create
    phase: str = "idle"  # idle | planning | executing | verifying
    token_budget: Optional[int] = None
    tokens_baseline: int = 0
    tokens_used: int = 0
    message: str = ""
    blocked_reason: str = ""
    blocked_streak: int = 0
    pause_message: str = ""
    gaps: List[str] = field(default_factory=list)
    verify_runs: int = 0
    verify_max: int = DEFAULT_VERIFY_MAX
    pending_completed_message: Optional[str] = None
    continue_count: int = 0
    continue_max: int = DEFAULT_CONTINUE_MAX
    plan_path: str = ""
    history: List[GoalEvent] = field(default_factory=list)
    created_at: float = 0.0

    # ── queries ──────────────────────────────────────────────────────────

    def is_open(self) -> bool:
        return bool(self.goal_id) and self.status in OPEN_STATUSES

    def is_active(self) -> bool:
        return self.status == "active" and bool(self.goal_id)

    def is_paused(self) -> bool:
        return self.status in PAUSED

    def should_continue(self) -> bool:
        """Host may inject a continuation turn after successful idle."""
        if not self.is_active() or self.phase == "verifying":
            return False
        if self.continue_count >= self.continue_max:
            return False
        return True

    def has_pending_complete(self) -> bool:
        return self.pending_completed_message is not None

    # ── mutations ────────────────────────────────────────────────────────

    def _push(self, kind: str, detail: str = "") -> None:
        self.history.append(GoalEvent(kind=kind, detail=detail[:500]))
        if len(self.history) > HISTORY_MAX:
            self.history = self.history[-HISTORY_MAX:]

    def create(
        self,
        objective: str,
        token_budget: Optional[int] = None,
        tokens_baseline: int = 0,
        plan_path: str = "",
    ) -> None:
        obj = (objective or "").strip()
        if not obj:
            raise ValueError("objective required")
        self.goal_id = uuid.uuid4().hex[:12]
        self.objective = obj
        self.status = "active"
        self.phase = "executing"
        self.token_budget = token_budget
        self.tokens_baseline = max(0, int(tokens_baseline or 0))
        self.tokens_used = 0
        self.message = ""
        self.blocked_reason = ""
        self.blocked_streak = 0
        self.pause_message = ""
        self.gaps = []
        self.verify_runs = 0
        self.verify_max = DEFAULT_VERIFY_MAX
        self.pending_completed_message = None
        self.continue_count = 0
        self.plan_path = plan_path or ""
        self.created_at = time.time()
        self.history = []
        self._push("goal_created", obj[:200])

    def clear(self) -> None:
        self._push("goal_cleared", self.goal_id)
        self.goal_id = ""
        self.objective = ""
        self.status = "cleared"
        self.phase = "idle"
        self.token_budget = None
        self.tokens_baseline = 0
        self.tokens_used = 0
        self.message = ""
        self.blocked_reason = ""
        self.blocked_streak = 0
        self.pause_message = ""
        self.gaps = []
        self.verify_runs = 0
        self.pending_completed_message = None
        self.continue_count = 0
        self.plan_path = ""

    def pause(self, reason: str = "user", message: str = "") -> None:
        if not self.is_open():
            return
        status_map = {
            "user": "user_paused",
            "blocked": "blocked",
            "budget": "budget_limited",
            "infra": "infra_paused",
        }
        self.status = status_map.get(reason, "user_paused")
        self.phase = "idle"
        self.pause_message = (message or "").strip()
        self.pending_completed_message = None
        self._push("goal_paused", f"{self.status}: {self.pause_message}"[:200])

    def resume(self) -> bool:
        if not self.is_open() or self.status == "active":
            return self.is_active()
        if self.status == "blocked":
            self.blocked_streak = 0
            self.blocked_reason = ""
        self.status = "active"
        self.phase = "executing"
        self.pause_message = ""
        self._push("goal_resumed")
        return True

    def complete(self, message: str = "") -> None:
        self.status = "complete"
        self.phase = "idle"
        if message:
            self.message = message.strip()
        self.pending_completed_message = None
        self._push("goal_completed", self.message[:200])

    def set_tokens_used(self, absolute_tokens: int) -> None:
        """Set spent tokens relative to baseline (session total tokens)."""
        try:
            cur = int(absolute_tokens or 0)
        except (TypeError, ValueError):
            return
        self.tokens_used = max(0, cur - self.tokens_baseline)

    def budget_exceeded(self) -> bool:
        if not self.token_budget or self.token_budget <= 0:
            return False
        return self.tokens_used >= self.token_budget

    def enforce_budget(self) -> bool:
        """If over budget → budget_limited. Returns True if limited."""
        if self.is_active() and self.budget_exceeded():
            self.pause("budget", f"Token budget {self.token_budget} reached "
                        f"(used ~{self.tokens_used})")
            return True
        return False

    # ── update_goal drain ────────────────────────────────────────────────

    def apply_update(
        self,
        *,
        message: str = "",
        completed: bool = False,
        blocked_reason: str = "",
        mid_turn: bool = True,
    ) -> Dict[str, Any]:
        """Apply model claim. Returns ack dict for the tool result."""
        msg = (message or "").strip()
        blocked = (blocked_reason or "").strip()

        if not self.is_open():
            if completed or blocked:
                return {
                    "ok": False,
                    "error": "No active goal. User must /goal <objective> first.",
                    "rejected": True,
                }
            # Progress with no goal: soft reject (do not invent sticky goal)
            return {
                "ok": False,
                "error": "No active goal; progress ignored.",
                "rejected": True,
            }

        if not self.is_active() and (completed or blocked):
            return {
                "ok": False,
                "error": f"Goal is {self.status}; resume with /goal resume first.",
                "rejected": True,
                "status": self.status,
            }

        if blocked:
            self.blocked_streak += 1
            self.blocked_reason = blocked
            self.message = msg or self.message
            self.pending_completed_message = None
            n = self.blocked_streak
            if n >= BLOCKED_STREAK_TO_PAUSE:
                self.pause("blocked", blocked)
                return {
                    "ok": True,
                    "status": "blocked",
                    "blocked_streak": n,
                    "message": f"Blocked after {n} reports. Goal paused.",
                    "summary": f"blocked: {blocked}",
                }
            return {
                "ok": True,
                "status": "active",
                "blocked_streak": n,
                "message": f"Blocked reason recorded ({n}/{BLOCKED_STREAK_TO_PAUSE}).",
                "summary": f"blocked {n}/{BLOCKED_STREAK_TO_PAUSE}: {blocked}",
            }

        if completed:
            claim = msg or self.message or "claimed complete"
            if mid_turn:
                self.pending_completed_message = claim
                self.message = claim
                self.blocked_streak = 0
                self.blocked_reason = ""
                self._push("complete_deferred", claim[:200])
                return {
                    "ok": True,
                    "status": "active",
                    "deferred": True,
                    "message": "Completion deferred to turn end for host verification.",
                    "summary": f"complete deferred: {claim}",
                }
            # Immediate path (turn-end drain entry) — still needs verifier
            self.pending_completed_message = claim
            self.message = claim
            self.blocked_streak = 0
            self.blocked_reason = ""
            return {
                "ok": True,
                "status": "active",
                "deferred": False,
                "pending_verify": True,
                "message": "Completion claimed; host will verify.",
                "summary": f"complete claimed: {claim}",
            }

        # message-only progress
        if msg:
            self.message = msg
            self.blocked_streak = 0
            self.blocked_reason = ""
            self._push("progress", msg[:200])
        return {
            "ok": True,
            "status": self.status,
            "message": self.message,
            "summary": f"progress: {self.message}" if self.message else "progress recorded",
        }

    def begin_verify(self) -> Optional[str]:
        """Move pending complete into verifying phase. Returns claim msg or None."""
        if not self.has_pending_complete() or not self.is_active():
            return None
        if self.verify_runs >= self.verify_max:
            self.pause(
                "user",
                f"Verification cap ({self.verify_max}) reached without Achieved.",
            )
            self.status = "user_paused"  # back_off-ish
            self.pause_message = (
                f"Verification cap ({self.verify_max}) reached. "
                "/goal resume to try again or /goal clear."
            )
            self.pending_completed_message = None
            self._push("verify_cap")
            return None
        claim = self.pending_completed_message or ""
        self.phase = "verifying"
        self.verify_runs += 1
        self._push("verify_started", f"run {self.verify_runs}")
        return claim

    def apply_verdict(
        self,
        achieved: bool,
        gaps: Optional[List[str]] = None,
        detail: str = "",
    ) -> None:
        """After verifier turn."""
        gaps = gaps or []
        if achieved:
            self.complete(detail or self.message or "verified")
            self.gaps = []
            return
        self.gaps = [g.strip() for g in gaps if (g or "").strip()][:12]
        self.pending_completed_message = None
        self.phase = "executing"
        if self.is_active():
            self._push("verify_not_achieved", detail[:200] if detail else "")
            if self.verify_runs >= self.verify_max:
                self.pause(
                    "user",
                    f"Verification cap ({self.verify_max}) reached.",
                )
                self.pause_message = (
                    f"Not achieved after {self.verify_max} checks. "
                    + ("; ".join(self.gaps[:3]) if self.gaps else "")
                )

    def note_continue(self) -> None:
        self.continue_count += 1
        self.phase = "executing"
        self._push("continue", f"n={self.continue_count}")

    def status_summary(self) -> str:
        if not self.goal_id:
            return "No active goal. Use /goal <objective> [--budget N]"
        parts = [
            f"status={self.status}",
            f"phase={self.phase}",
            f"id={self.goal_id}",
        ]
        if self.token_budget:
            parts.append(f"tokens~{self.tokens_used}/{self.token_budget}")
        if self.verify_runs:
            parts.append(f"verify={self.verify_runs}/{self.verify_max}")
        lines = [
            f"Goal: {self.objective}",
            "  " + " · ".join(parts),
        ]
        if self.message:
            lines.append(f"  message: {self.message}")
        if self.blocked_reason:
            lines.append(f"  blocked: {self.blocked_reason}")
        if self.pause_message:
            lines.append(f"  pause: {self.pause_message}")
        if self.gaps:
            lines.append("  gaps: " + "; ".join(self.gaps[:5]))
        return "\n".join(lines)

    def to_ui_dict(self) -> Dict[str, Any]:
        """Snapshot for GoalState / strip."""
        return {
            "goal_id": self.goal_id,
            "objective": self.objective,
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "blocked_reason": self.blocked_reason,
            "pause_message": self.pause_message,
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "verify_runs": self.verify_runs,
            "verify_max": self.verify_max,
            "gaps": list(self.gaps),
            "verifying": self.phase == "verifying",
            "planning": self.phase == "planning",
        }

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["history"] = [
            {"kind": e.kind, "detail": e.detail, "ts": e.ts}
            for e in self.history
        ]
        return d

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "GoalTracker":
        g = cls()
        if not data:
            return g
        g.goal_id = str(data.get("goal_id") or "")
        g.objective = str(data.get("objective") or "")
        g.status = str(data.get("status") or "cleared")
        g.phase = str(data.get("phase") or "idle")
        tb = data.get("token_budget")
        g.token_budget = int(tb) if tb is not None else None
        g.tokens_baseline = int(data.get("tokens_baseline") or 0)
        g.tokens_used = int(data.get("tokens_used") or 0)
        g.message = str(data.get("message") or "")
        g.blocked_reason = str(data.get("blocked_reason") or "")
        g.blocked_streak = int(data.get("blocked_streak") or 0)
        g.pause_message = str(data.get("pause_message") or "")
        g.gaps = list(data.get("gaps") or [])
        g.verify_runs = int(data.get("verify_runs") or 0)
        g.verify_max = int(data.get("verify_max") or DEFAULT_VERIFY_MAX)
        pcm = data.get("pending_completed_message")
        g.pending_completed_message = str(pcm) if pcm is not None else None
        g.continue_count = int(data.get("continue_count") or 0)
        g.plan_path = str(data.get("plan_path") or "")
        g.created_at = float(data.get("created_at") or 0)
        hist = []
        for h in (data.get("history") or [])[-HISTORY_MAX:]:
            if isinstance(h, dict):
                hist.append(GoalEvent(
                    kind=str(h.get("kind") or ""),
                    detail=str(h.get("detail") or ""),
                    ts=float(h.get("ts") or 0),
                ))
        g.history = hist
        # Restore mid-flight Active → user_paused (subagents don't survive)
        if g.status == "active" and g.goal_id:
            g.status = "user_paused"
            g.phase = "idle"
            g.pending_completed_message = None
            if not g.pause_message:
                g.pause_message = "Restored from disk — /goal resume to continue."
            g._push("restored_paused")
        return g


def parse_goal_slash(args: str) -> Tuple[str, Any]:
    """Parse /goal args after the command name.

    Returns (action, payload):
      ('status', None)
      ('pause', None)
      ('resume', None)
      ('clear', None)
      ('set', (objective, budget|None))
    """
    raw = (args or "").strip()
    if not raw:
        return ("status", None)
    low = raw.lower()
    first = low.split(None, 1)[0]
    if first in GOAL_RESERVED:
        if first == "edit":
            return ("status", None)  # reserved, treat as status for v1
        return (first, None)

    objective, budget = _parse_budget_trailing(raw)
    if not objective.strip():
        return ("status", None)
    return ("set", (objective.strip(), budget))


def _parse_budget_trailing(text: str) -> Tuple[str, Optional[int]]:
    """Strip trailing standalone --budget <positive int>."""
    parts = text.split()
    if len(parts) >= 2 and parts[-2] == "--budget":
        val = parts[-1]
        if val.isdigit() and int(val) > 0:
            return (" ".join(parts[:-2]), int(val))
    return (text, None)
