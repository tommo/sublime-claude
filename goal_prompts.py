"""Prompt templates for plugin-native goal mode."""
from __future__ import annotations

from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .goal_tracker import GoalTracker


def _plan_contract_excerpt(tracker: "GoalTracker", max_chars: int = 3500) -> str:
    body = (getattr(tracker, "plan_body", None) or "").strip()
    if not body:
        return ""
    if len(body) > max_chars:
        body = body[:max_chars] + "\n…[plan truncated for prompt]"
    path = (getattr(tracker, "plan_path", None) or "").strip()
    loc = f"\nPlan path: {path}" if path else ""
    return f"\nHOST PLAN CONTRACT (source of truth):{loc}\n{body}\n"


def goal_rules(objective: str, plan_hint: str = "", plan_body: str = "") -> str:
    plan = plan_hint or (
        "The host owns the plan artifact (Acceptance criteria + Verification plan). "
        "Execute against that contract; do not invent a weaker bar."
    )
    contract = ""
    if (plan_body or "").strip():
        contract = f"\n{plan_body.strip()}\n"
    return f"""<goal-mode>
You are working under a HOST-OWNED goal harness (sublime-claude).

OBJECTIVE:
{objective}
{contract}
Rules:
1. Deliver the objective fully — no leaving manual steps for the user.
2. Track work with todos when the task has multiple steps.
3. Verify as you go with real commands/files — no test theater.
4. Report progress with the MCP tool `update_goal`:
   - update_goal(message="…") for progress notes
   - update_goal(completed=true, message="…") ONLY when fully achieved
     (the host re-checks against the plan; do not assume complete is accepted)
   - update_goal(blocked_reason="…") only after multiple failed attempts
5. Prefer MCP sublime `update_goal` for harness status (not quick_done).
6. {plan}
7. When blocked three times the host will pause the goal.

Parallelism (use it — do not default to solo serial work):
8. When the objective has independent workstreams, fan out with subagents
   instead of doing everything yourself in one long serial chain.
9. Prefer the platform Task / Agent tools (Explore, general-purpose, Plan, …)
   for research, parallel edits, isolated spikes, and verification side-quests.
10. Use MCP sublime `spawn_session` when a separate full session helps
    (different backend/profile, long-running branch of work, wait_for_completion
    for a dedicated worker). You remain the orchestrator: integrate results,
    own the objective, and only you call update_goal(completed=true).
11. Do not spawn for trivial one-file tweaks. Do spawn when parallel work would
    clearly cut wall time or keep context focused.

Work until the objective is done or truly blocked.
</goal-mode>
"""


def implementer_kickoff(tracker: "GoalTracker") -> str:
    """First implementer turn after host accepts the plan."""
    excerpt = _plan_contract_excerpt(tracker)
    return (
        goal_rules(tracker.objective, plan_body=excerpt)
        + "\nThe host has finished planning. Execute the plan contract above.\n"
        + "Flip checklist items only when done; claim complete only with evidence.\n"
    )


def continuation_directive(tracker: "GoalTracker") -> str:
    gaps = ""
    if tracker.gaps:
        gaps = "\nVerification gaps (must address):\n" + "\n".join(
            f"- {g}" for g in tracker.gaps[:8]
        )
    budget = ""
    if tracker.token_budget:
        budget = f"\nTokens used ~{tracker.tokens_used}/{tracker.token_budget}."
    msg = f" Last progress: {tracker.message}" if tracker.message else ""
    excerpt = _plan_contract_excerpt(tracker)
    criteria = ""
    try:
        from .goal_plan import extract_acceptance_criteria
    except ImportError:
        from goal_plan import extract_acceptance_criteria  # type: ignore
    ac = extract_acceptance_criteria(getattr(tracker, "plan_body", "") or "")
    if ac:
        criteria = "\nAcceptance criteria (must all pass for complete):\n" + "\n".join(
            f"- {c}" for c in ac[:12]
        )
    return f"""Goal NOT complete — continue working. Next step:

OBJECTIVE: {tracker.objective}
{msg}{budget}{gaps}
{criteria}
{excerpt}
Continue implementing and verifying against the HOST PLAN CONTRACT.
Call update_goal(message=…) for progress.
When fully done: update_goal(completed=true, message=summary of evidence).
If stuck after real attempts: update_goal(blocked_reason=…).

If remaining work is parallelizable, use Task/Agent subagents or
spawn_session — do not grind independent strands serially by default.
"""


def verifier_prompt(
    objective: str,
    claim: str,
    gaps_prior: Optional[List[str]] = None,
    plan_body: str = "",
) -> str:
    prior = ""
    if gaps_prior:
        prior = "\nPrior gaps:\n" + "\n".join(f"- {g}" for g in gaps_prior[:6])
    plan_block = ""
    if (plan_body or "").strip():
        try:
            from .goal_plan import (
                extract_acceptance_criteria,
                extract_verification_steps,
            )
        except ImportError:
            from goal_plan import (  # type: ignore
                extract_acceptance_criteria,
                extract_verification_steps,
            )
        ac = extract_acceptance_criteria(plan_body)
        vs = extract_verification_steps(plan_body)
        plan_block = "\nPLAN ACCEPTANCE CRITERIA (judge against these):\n"
        plan_block += "\n".join(f"- {c}" for c in ac[:12]) if ac else "- (missing)"
        plan_block += "\n\nPLAN VERIFICATION STEPS:\n"
        plan_block += "\n".join(f"- {s}" for s in vs[:12]) if vs else "- (missing)"
        plan_block += (
            "\n\nA prose claim alone is insufficient. Require concrete evidence "
            "matching the plan's verification bar."
        )
    return f"""<goal-verifier>
You are a SKEPTIC verifier for a host-owned goal. Do NOT implement new features.
Do NOT call update_goal(completed=true). You only check evidence.

OBJECTIVE:
{objective}

IMPLEMENTER CLAIM:
{claim}
{prior}
{plan_block}

Steps:
1. Inspect the workspace (read files, run targeted checks) for real evidence.
2. Decide if the objective is fully achieved against the plan criteria (not only
   the claim wording).

End your reply with EXACTLY one of these lines (and optional gaps after):
VERDICT: achieved
VERDICT: not_achieved
GAPS:
- short gap 1
- short gap 2
</goal-verifier>
"""


def resume_recap(tracker: "GoalTracker") -> str:
    excerpt = _plan_contract_excerpt(tracker)
    if tracker.phase == "planning" or not tracker.has_plan():
        return (
            f"Resuming goal {tracker.goal_id} still in planning.\n"
            + tracker.status_summary()
            + "\nHost will re-materialize the plan contract.\n"
        )
    return (
        goal_rules(tracker.objective, plan_body=excerpt)
        + f"\nResuming goal {tracker.goal_id}. Status was paused.\n"
        + tracker.status_summary()
        + "\nContinue from here against the plan contract.\n"
    )


def parse_verifier_verdict(text: str) -> tuple:
    """Return (achieved: bool, gaps: list[str]). Default not_achieved."""
    if not text:
        return (False, ["Verifier produced no output"])
    achieved = False
    gaps: List[str] = []
    in_gaps = False
    for line in text.strip().splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("verdict:"):
            in_gaps = False
            body = low.split(":", 1)[1].strip()
            compact = body.replace(" ", "").replace("-", "_")
            if "not_achieved" in compact or body.startswith("not "):
                achieved = False
            elif "achieved" in compact:
                achieved = True
            continue
        if low.startswith("gaps:"):
            in_gaps = True
            rest = s.split(":", 1)[-1].strip()
            if rest:
                gaps.append(rest.lstrip("-* ").strip())
            continue
        if in_gaps and (s.startswith("-") or s.startswith("*")):
            g = s.lstrip("-* ").strip()
            if g:
                gaps.append(g)
    if achieved:
        return (True, [])
    if not gaps:
        gaps = ["Verifier did not confirm achievement"]
    return (False, gaps)
