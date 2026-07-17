"""Prompt templates for plugin-native goal mode."""
from __future__ import annotations

from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .goal_tracker import GoalTracker


def goal_rules(objective: str, plan_hint: str = "") -> str:
    plan = plan_hint or (
        "Optionally write a short plan to a file under the project "
        "(e.g. goal/plan.md) before large changes."
    )
    return f"""<goal-mode>
You are working under a HOST-OWNED goal harness (sublime-claude).

OBJECTIVE:
{objective}

Rules:
1. Deliver the objective fully — no leaving manual steps for the user.
2. Track work with todos when the task has multiple steps.
3. Verify as you go with real commands/files — no test theater.
4. Report progress with the MCP tool `update_goal`:
   - update_goal(message="…") for progress notes
   - update_goal(completed=true, message="…") ONLY when fully achieved
     (the host re-checks; do not assume complete is accepted)
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
    return f"""Goal NOT complete — continue working. Next step:

OBJECTIVE: {tracker.objective}
{msg}{budget}{gaps}

Continue implementing and verifying. Call update_goal(message=…) for progress.
When fully done: update_goal(completed=true, message=summary of evidence).
If stuck after real attempts: update_goal(blocked_reason=…).

If remaining work is parallelizable, use Task/Agent subagents or
spawn_session — do not grind independent strands serially by default.
"""


def verifier_prompt(
    objective: str,
    claim: str,
    gaps_prior: Optional[List[str]] = None,
) -> str:
    prior = ""
    if gaps_prior:
        prior = "\nPrior gaps:\n" + "\n".join(f"- {g}" for g in gaps_prior[:6])
    return f"""<goal-verifier>
You are a SKEPTIC verifier for a host-owned goal. Do NOT implement new features.
Do NOT call update_goal(completed=true). You only check evidence.

OBJECTIVE:
{objective}

IMPLEMENTER CLAIM:
{claim}
{prior}

Steps:
1. Inspect the workspace (read files, run targeted checks) for real evidence.
2. Decide if the objective is fully achieved.

End your reply with EXACTLY one of these lines (and optional gaps after):
VERDICT: achieved
VERDICT: not_achieved
GAPS:
- short gap 1
- short gap 2
</goal-verifier>
"""


def resume_recap(tracker: "GoalTracker") -> str:
    return (
        goal_rules(tracker.objective)
        + f"\nResuming goal {tracker.goal_id}. Status was paused.\n"
        + tracker.status_summary()
        + "\nContinue from here.\n"
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
