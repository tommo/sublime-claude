"""Plugin-owned goal harness state machine (backend-agnostic).

Reference: Grok Build goal mode contracts — model claims via update_goal;
host owns verification and continuation. Pure Python, no Sublime imports.
"""
from __future__ import annotations

import os
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


def ui_phase_label(
    *,
    status: str = "",
    phase: str = "",
    gaps: Optional[List[str]] = None,
) -> str:
    """Human phase for sticky goal strip + status chip (pure; no Sublime)."""
    st = (status or "").strip()
    ph = (phase or "").strip()
    if ph == "verifying":
        return "verifying"
    if ph == "planning":
        return "planning"
    if ph == "executing":
        return "executing"
    if st == "blocked":
        return "blocked"
    if st == "budget_limited":
        return "budget"
    if st in ("user_paused", "infra_paused"):
        return "paused"
    if st == "complete":
        return "complete"
    if st == "active":
        return "active"
    return st or "idle"


def ui_phase_body(
    *,
    status: str = "",
    phase: str = "",
    message: str = "",
    objective: str = "",
    pause_message: str = "",
    blocked_reason: str = "",
    gaps: Optional[List[str]] = None,
) -> str:
    """Progress / gap line — not a second copy of the objective."""
    ph = (phase or "").strip()
    msg = (message or "").strip()
    obj = (objective or "").strip()
    if (pause_message or "").strip():
        return pause_message.strip()
    if (blocked_reason or "").strip():
        return blocked_reason.strip()
    gs = [g.strip() for g in (gaps or []) if (g or "").strip()]
    if gs and ph != "verifying" and st_is_work(status):
        if len(gs) == 1:
            return gs[0]
        return gs[0] + f" (+{len(gs) - 1} more)"
    # Prefer progress message; skip if it only restates the objective
    if msg and msg != obj and not _is_noise_progress(msg):
        return msg
    if ph == "verifying":
        return msg or ""
    return ""


def st_is_work(status: str) -> bool:
    return (status or "").strip() in (
        "active", "user_paused", "blocked", "budget_limited", "infra_paused",
    )


def _is_noise_progress(msg: str) -> bool:
    m = (msg or "").strip().lower()
    return m in (
        "plan accepted — executing",
        "plan accepted",
        "executing",
    )


def _clip(text: str, n: int) -> str:
    t = (text or "").replace("\n", " ").strip()
    if len(t) <= n:
        return t
    if n <= 1:
        return "…"
    # Constraints-first objectives ("don't X, just Y") — keep the tail intent
    low = t.lower()
    if low.startswith(("don't ", "do not ", "never ", "without ")):
        return "…" + t[-(n - 1) :]
    return t[: max(0, n - 1)] + "…"


def format_goal_strip_line(
    *,
    status: str = "",
    phase: str = "",
    message: str = "",
    objective: str = "",
    pause_message: str = "",
    blocked_reason: str = "",
    gaps: Optional[List[str]] = None,
    token_budget: Optional[int] = None,
    tokens_used: Optional[int] = None,
    verify_runs: int = 0,
    verify_max: int = 0,
    compact: bool = False,
) -> str:
    """One compact sticky work-strip line (no composer ◎ confusion).

    Shape: ``  ◆ goal · {phase} · {short obj}  — {progress}  (extras)``
    Uses ◆ (not ◎) so it never collides with the sticky composer marker.
    """
    label = ui_phase_label(status=status, phase=phase, gaps=gaps)
    obj = _clip(objective, 36 if compact else 44)
    body = ui_phase_body(
        status=status,
        phase=phase,
        message=message,
        objective=objective,
        pause_message=pause_message,
        blocked_reason=blocked_reason,
        gaps=gaps,
    )
    body = _clip(body, 56 if compact else 72)
    # Avoid "obj — obj"
    if body and obj and body.lower() == obj.lower():
        body = ""
    line = f"  ◆ goal · {label}"
    if obj:
        line += f" · {obj}"
    if body:
        line += f"  — {body}"
    extra = []
    if token_budget:
        extra.append(f"~{tokens_used or 0}/{token_budget}")
    if verify_runs or (phase or "") == "verifying":
        extra.append(f"verify {verify_runs or 0}/{verify_max or '?'}")
    gap_n = len([g for g in (gaps or []) if (g or "").strip()])
    if gap_n and (phase or "") != "verifying":
        extra.append(f"{gap_n} gap" + ("s" if gap_n != 1 else ""))
    if extra:
        line += f"  ({' · '.join(extra)})"
    return line


@dataclass
class GoalEvent:
    kind: str
    detail: str = ""
    ts: float = field(default_factory=time.time)


@dataclass
class GoalTracker:
    """Single active goal per session.

    Lifecycle: create → planning (no implementer continue) → accept_plan →
    executing (continue loop) → verifying (on complete claim) → complete/clear.
    """
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
    # Structured skeptic result from MCP goal_verdict (preferred over prose parse)
    pending_tool_verdict: Optional[Dict[str, Any]] = None
    continue_count: int = 0
    continue_max: int = DEFAULT_CONTINUE_MAX
    plan_path: str = ""
    plan_body: str = ""  # host-owned plan markdown (live checklist marks)
    plan_baseline: str = ""  # immutable AC/VS/items text at accept
    history: List[GoalEvent] = field(default_factory=list)
    created_at: float = 0.0

    # ── queries ──────────────────────────────────────────────────────────

    def is_open(self) -> bool:
        return bool(self.goal_id) and self.status in OPEN_STATUSES

    def is_active(self) -> bool:
        return self.status == "active" and bool(self.goal_id)

    def is_paused(self) -> bool:
        return self.status in PAUSED

    def has_plan(self) -> bool:
        """True when a structured plan is ready for execute/verify."""
        body = (self.plan_body or "").strip()
        if not body:
            return False
        try:
            from .goal_plan import plan_has_required_sections
        except ImportError:
            from goal_plan import plan_has_required_sections  # type: ignore
        return plan_has_required_sections(body)

    def should_continue(self) -> bool:
        """Host may inject implementer continuation only after plan is ready."""
        if not self.is_active():
            return False
        if self.phase in ("planning", "verifying"):
            return False
        if self.phase != "executing":
            return False
        if not self.has_plan():
            return False
        if self.continue_count >= self.continue_max:
            return False
        return True

    def has_pending_complete(self) -> bool:
        # While verifying, claim is in-flight — not a new complete to re-fire
        if self.phase == "verifying":
            return False
        return self.pending_completed_message is not None

    def ui_phase_label(self) -> str:
        """Sticky strip / status chip phase word (verifying beats bare active)."""
        return ui_phase_label(
            status=self.status,
            phase=self.phase,
            gaps=self.gaps,
        )

    def ui_phase_body(self) -> str:
        """Secondary strip text: claim while verifying, else gaps / message."""
        return ui_phase_body(
            status=self.status,
            phase=self.phase,
            message=self.message,
            objective=self.objective,
            pause_message=self.pause_message,
            blocked_reason=self.blocked_reason,
            gaps=self.gaps,
        )

    def sync_plan_body_from_disk(self) -> bool:
        """Merge disk checklist ``[x]`` marks onto the frozen accepted plan.

        Contract sections (Acceptance criteria, Verification plan, checklist
        item *text*) stay as accepted. Disk may only flip checkbox state for
        matching items. Deleting the checklist or thinning AC on disk is
        ignored — open frozen items stay open so complete stays blocked.

        Returns True if plan_body checklist marks changed.
        """
        path = (self.plan_path or "").strip()
        if not path or not self.has_plan() or not (self.plan_body or "").strip():
            return False
        try:
            import os
            if not os.path.isfile(path):
                return False
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                disk = f.read()
        except Exception:
            return False
        try:
            from .goal_plan import merge_checklist_marks
        except ImportError:
            from goal_plan import merge_checklist_marks  # type: ignore
        merged, changed = merge_checklist_marks(self.plan_body, disk)
        if not changed:
            return False
        self.plan_body = merged
        self._push("plan_checklist_synced", path[:200])
        return True

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
        # Host plan expansion first — implementer continue blocked until accept_plan
        self.phase = "planning"
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
        self.pending_tool_verdict = None
        self.continue_count = 0
        self.plan_path = plan_path or ""
        self.plan_body = ""
        self.plan_baseline = ""
        self.created_at = time.time()
        self.history = []
        self._push("goal_created", obj[:200])
        self._push("planning_started", self.goal_id)

    def accept_plan(self, plan_md: str, plan_path: str = "") -> Dict[str, Any]:
        """Host freezes plan contract → executing. Returns ok/error dict.

        ``plan_baseline`` is an immutable copy of AC/verification/checklist text.
        Disk plan.md may only flip checklist ``[x]`` marks thereafter.

        If the goal is merely user/infra-paused mid-planning (Esc during plan
        write), auto-resume so a valid plan.md is not stuck in a re-plan loop.
        """
        body = (plan_md or "").strip()
        if not self.is_open():
            return {
                "ok": False,
                "error": "No open goal to accept plan for (cleared or never /goal).",
                "host_state": True,
            }
        if not self.is_active():
            # Esc pauses to user_paused + phase idle while planner still wrote plan.md
            if self.status in ("user_paused", "infra_paused") and not self.has_plan():
                self.resume()
            elif self.status in ("user_paused", "infra_paused") and self.phase in (
                    "planning", "idle"):
                # Mid-plan pause: resume into planning then accept elevates to executing
                self.resume()
            if not self.is_active():
                return {
                    "ok": False,
                    "error": (
                        f"Goal is {self.status}, not active — "
                        f"/goal resume before accept (not a plan schema error)."
                    ),
                    "host_state": True,
                    "issues": [
                        f"host state: goal {self.status}; /goal resume",
                    ],
                }
        try:
            from .goal_plan import plan_has_required_sections
        except ImportError:
            from goal_plan import plan_has_required_sections  # type: ignore
        if not plan_has_required_sections(body):
            return {
                "ok": False,
                "error": "Plan missing required sections "
                "(## Acceptance criteria, ## Verification plan).",
            }
        try:
            from .goal_plan import plan_quality_issues, write_plan_baseline
        except ImportError:
            from goal_plan import plan_quality_issues, write_plan_baseline  # type: ignore
        quality = plan_quality_issues(body, self.objective)
        if quality:
            return {
                "ok": False,
                "error": "Plan rejected: " + "; ".join(quality[:4]),
                "issues": quality,
            }
        frozen = body if body.endswith("\n") else body + "\n"
        self.plan_body = frozen
        self.plan_baseline = frozen
        if plan_path:
            self.plan_path = plan_path
            try:
                write_plan_baseline(plan_path, frozen)
            except Exception:
                pass
        self.phase = "executing"
        self.message = "Plan accepted — contract frozen"
        self._push("plan_accepted", (self.plan_path or "in-memory")[:200])
        return {
            "ok": True,
            "phase": self.phase,
            "plan_path": self.plan_path,
            "baseline": True,
        }

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
        self.pending_tool_verdict = None
        self.continue_count = 0
        self.plan_path = ""
        self.plan_body = ""
        self.plan_baseline = ""

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
        # Remember plan vs no-plan so resume re-enters the right phase.
        # Do NOT wipe planning → idle in a way that loses "still need plan".
        if self.has_plan():
            self.phase = "idle"
        else:
            self.phase = "planning"
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
        # Resume into planning if plan never accepted; else executing
        self.phase = "executing" if self.has_plan() else "planning"
        self.pause_message = ""
        self._push("goal_resumed", self.phase)
        return True

    def complete(self, message: str = "") -> None:
        self.status = "complete"
        self.phase = "idle"
        if message:
            self.message = message.strip()
        self.pending_completed_message = None
        # Drop plan contract so stale plans are not treated as still active
        self.plan_body = ""
        self.plan_path = ""
        self.plan_baseline = ""
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
            if self.phase == "planning" or not self.has_plan():
                return {
                    "ok": False,
                    "error": "No plan ready; host must finish planning before complete.",
                    "rejected": True,
                    "phase": self.phase,
                }
            claim = msg or self.message or "claimed complete"
            # Re-read plan.md so checklist [x] flips on disk count (not freeze-only)
            self.sync_plan_body_from_disk()
            # Host preflight: open checklist / partial language / diluted north-star
            try:
                from .goal_plan import complete_claim_preflight
            except ImportError:
                from goal_plan import complete_claim_preflight  # type: ignore
            pre = complete_claim_preflight(
                claim,
                self.plan_body,
                self.objective,
                plan_baseline=self.plan_baseline or self.plan_body,
            )
            if pre:
                self._push("complete_rejected_preflight", pre[0][:200])
                return {
                    "ok": False,
                    "error": "Complete rejected: " + "; ".join(pre[:4]),
                    "rejected": True,
                    "issues": pre,
                    "status": self.status,
                    "phase": self.phase,
                }
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
        if not self.has_plan():
            self.pending_completed_message = None
            self._push("verify_rejected_no_plan")
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
        self.pending_completed_message = None  # claim consumed; avoid re-fire
        # Fresh checklist state for verifier-side preflight too
        self.sync_plan_body_from_disk()
        self.phase = "verifying"
        self.verify_runs += 1
        self.pending_tool_verdict = None  # fresh skeptic turn
        self._push("verify_started", f"run {self.verify_runs}")
        return claim

    def record_tool_verdict(
        self,
        *,
        achieved: bool,
        evidence: Optional[List[str]] = None,
        gaps: Optional[List[str]] = None,
        message: str = "",
    ) -> Dict[str, Any]:
        """MCP goal_verdict during verifying phase — structured, not prose parse."""
        if self.phase != "verifying":
            return {
                "ok": False,
                "error": "goal_verdict only valid during host verifying phase.",
                "rejected": True,
                "phase": self.phase,
            }
        if not self.is_active():
            return {
                "ok": False,
                "error": f"Goal is {self.status}; cannot record verdict.",
                "rejected": True,
            }
        ev = [str(x).strip() for x in (evidence or []) if str(x).strip()][:20]
        gp = [str(x).strip() for x in (gaps or []) if str(x).strip()][:12]
        # Structured fail-closed: achieved requires non-empty evidence, no gaps
        if achieved:
            if gp:
                achieved = False
                if "gaps present" not in " ".join(gp).lower():
                    gp = gp + ["Host: gaps present — cannot mark achieved"]
            if not ev:
                achieved = False
                gp = gp + ["Host: achieved requires non-empty evidence[]"]
            # Re-read plan.md then preflight (checklist may have been flipped)
            self.sync_plan_body_from_disk()
            try:
                from .goal_plan import complete_claim_preflight
            except ImportError:
                from goal_plan import complete_claim_preflight  # type: ignore
            claim = (message or self.message or "").strip()
            pre = complete_claim_preflight(
                claim,
                self.plan_body,
                self.objective,
                plan_baseline=self.plan_baseline or self.plan_body,
            )
            if pre:
                achieved = False
                gp = gp + [f"Host: {p}" for p in pre[:4]]
        else:
            if not gp:
                gp = ["not achieved (tool)"]
        # One verify run, one decisive verdict. Do not let a later soft
        # not_achieved (or empty re-call) overwrite a stronger achieved.
        prev = self.pending_tool_verdict
        if prev and prev.get("achieved") and not achieved:
            self._push(
                "tool_verdict_ignored",
                f"keep achieved; ignore later not_achieved gaps={len(gp)}",
            )
            return {
                "ok": True,
                "recorded": False,
                "deduped": True,
                "achieved": True,
                "evidence_count": len(prev.get("evidence") or []),
                "gaps": list(prev.get("gaps") or []),
                "message": (
                    "Prior achieved verdict already recorded this verify run; "
                    "later not_achieved ignored."
                ),
            }
        if prev and prev.get("achieved") and achieved:
            # Keep the richer evidence set
            if len(ev) < len(prev.get("evidence") or []):
                self._push("tool_verdict_ignored", "keep earlier achieved with more evidence")
                return {
                    "ok": True,
                    "recorded": False,
                    "deduped": True,
                    "achieved": True,
                    "evidence_count": len(prev.get("evidence") or []),
                    "gaps": [],
                    "message": "Earlier achieved verdict with more evidence kept.",
                }
        self.pending_tool_verdict = {
            "achieved": bool(achieved),
            "evidence": ev,
            "gaps": gp,
            "message": (message or "").strip()[:500],
            "source": "tool",
        }
        self._push(
            "tool_verdict",
            ("achieved" if achieved else "not_achieved") + f" ev={len(ev)} gaps={len(gp)}",
        )
        return {
            "ok": True,
            "recorded": True,
            "achieved": bool(achieved),
            "evidence_count": len(ev),
            "gaps": gp,
            "message": "Verdict recorded; host applies at end of verify turn.",
        }

    def take_tool_verdict(self) -> Optional[Dict[str, Any]]:
        """Consume structured verdict if the verifier called goal_verdict."""
        v = self.pending_tool_verdict
        self.pending_tool_verdict = None
        return v

    def evidence_dir(self) -> str:
        """Directory for host/agent evidence files next to plan.md."""
        path = (self.plan_path or "").strip()
        if path:
            return os.path.join(os.path.dirname(os.path.abspath(path)), "evidence")
        return ""

    def try_load_verdict_file(self) -> Optional[Dict[str, Any]]:
        """Load evidence/VERDICT.json written by Task skeptic when MCP failed.

        Same fail-closed rules as MCP goal_verdict (via record_tool_verdict).
        Returns the record_tool_verdict result dict, or None if no file.
        """
        import json
        edir = self.evidence_dir()
        if not edir:
            return None
        for name in ("VERDICT.json", "verdict.json", "SKEPTIC_VERDICT.json"):
            fpath = os.path.join(edir, name)
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                self._push("verdict_file_error", f"{name}: {e}"[:200])
                continue
            if not isinstance(data, dict):
                continue
            # Normalize lists
            def _as_list(v):
                if v is None:
                    return []
                if isinstance(v, str):
                    return [ln.strip().lstrip("-* ") for ln in v.splitlines() if ln.strip()]
                if isinstance(v, (list, tuple)):
                    return [str(x).strip() for x in v if str(x).strip()]
                return [str(v).strip()] if str(v).strip() else []

            achieved = bool(data.get("achieved"))
            # Accept common aliases
            if "achieved" not in data and "verdict" in data:
                v = str(data.get("verdict") or "").lower()
                achieved = v in ("achieved", "complete", "pass", "true", "ok")
            rec = self.record_tool_verdict(
                achieved=achieved,
                evidence=_as_list(data.get("evidence")),
                gaps=_as_list(data.get("gaps")),
                message=str(data.get("message") or data.get("summary") or "")[:500],
            )
            if rec.get("ok"):
                rec = dict(rec)
                rec["source"] = "verdict_file"
                rec["path"] = fpath
                self._push("verdict_file", f"{name} achieved={rec.get('achieved')}")
            return rec
        return None

    def apply_verdict(
        self,
        achieved: bool,
        gaps: Optional[List[str]] = None,
        detail: str = "",
    ) -> None:
        """After verifier turn. Terminal complete/cleared cannot be demoted."""
        if self.status in ("complete", "cleared"):
            return
        gaps = gaps or []
        self.pending_tool_verdict = None
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
        if self.plan_path:
            lines.append(f"  plan: {self.plan_path}")
        elif self.has_plan():
            lines.append("  plan: (in-memory)")
        elif self.phase == "planning":
            lines.append("  plan: pending host expansion")
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
            "has_plan": self.has_plan(),
            "plan_path": self.plan_path,
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
        g.plan_body = str(data.get("plan_body") or "")
        g.plan_baseline = str(data.get("plan_baseline") or "")
        g.created_at = float(data.get("created_at") or 0)
        # Prefer immutable baseline file over plan.md (plan.md is agent-writable)
        if g.plan_path and not g.plan_baseline:
            try:
                from .goal_plan import plan_baseline_path
            except ImportError:
                from goal_plan import plan_baseline_path  # type: ignore
            bp = plan_baseline_path(g.plan_path)
            if bp and os.path.isfile(bp):
                try:
                    with open(bp, "r", encoding="utf-8") as f:
                        g.plan_baseline = f.read()
                except Exception:
                    pass
        if not g.plan_body and g.plan_baseline:
            g.plan_body = g.plan_baseline
        # Never rehydrate contract solely from plan.md (manipulated AC disaster).
        # If only plan.md exists without baseline, load it but also freeze it.
        if g.plan_path and not g.plan_body and os.path.isfile(g.plan_path):
            try:
                with open(g.plan_path, "r", encoding="utf-8") as f:
                    g.plan_body = f.read()
                if not g.plan_baseline:
                    g.plan_baseline = g.plan_body
            except Exception:
                pass
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
