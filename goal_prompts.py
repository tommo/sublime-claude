"""Prompt templates for plugin-native goal mode."""
from __future__ import annotations

import re
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
   - update_goal(completed=true, message="…") ONLY when the FULL objective is done
     (host rejects open checklist items, partial/deferral claim language, and
     diluted plans; then a goal_verdict turn must still pass)
   - update_goal(blocked_reason="…") only after multiple failed attempts
   - Never claim complete for "one sprint", "first slice", or deferred north-star work
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


def planner_kickoff(tracker: "GoalTracker", plan_path: str = "") -> str:
    """First turn: write a real plan against the host schema (shown in full)."""
    path = (plan_path or getattr(tracker, "plan_path", "") or "").strip()
    path_line = f"Write the plan to: `{path}`\n" if path else (
        "Write the plan under `.claude/goals/<goal_id>/plan.md`.\n"
    )
    try:
        from .goal_plan import plan_schema_for_prompt
    except ImportError:
        from goal_plan import plan_schema_for_prompt  # type: ignore
    schema = plan_schema_for_prompt()
    return f"""<goal-planning>
You are the PLANNER for a host-owned goal. Do NOT implement the objective yet.
Do NOT call update_goal(completed=true).

OBJECTIVE:
{tracker.objective}

{schema}

Your job this turn:
1. Explore only enough to make paths/commands real for THIS objective
   (docs/demo goals still need concrete evidence paths under the goal dir).
2. Write plan.md matching the schema above (required headings + ≥2 AC + ≥2
   verification steps with paths/commands).
3. {path_line}
4. End the turn after the file is written. Host runs the accept gate.
   On reject you get each issue + this schema again — fix the file, do not
   invent a different structure.

Host FREEZES the accepted plan as an immutable contract (plan.baseline.md).
Rewriting Acceptance criteria / Verification after accept is IGNORED and
cannot unlock complete. Only checklist ``[x]`` marks may change on disk.

Ban: generic templates; single vague criterion; verification that is only
``test -f`` on a marker you just wrote; restating the objective as the only
content; meta "prove the harness works" plans when the objective is real work.
</goal-planning>
"""


def planner_revise(tracker: "GoalTracker", issues: list, plan_path: str = "") -> str:
    """Re-plan after host rejected the plan — always re-show the schema."""
    path = (plan_path or getattr(tracker, "plan_path", "") or "").strip()
    iss = "\n".join(f"- {i}" for i in (issues or [])[:10])
    try:
        from .goal_plan import plan_schema_for_prompt
    except ImportError:
        from goal_plan import plan_schema_for_prompt  # type: ignore
    schema = plan_schema_for_prompt()
    return f"""<goal-planning>
Host REJECTED plan.md — the file did not match the accept-gate schema.
This is a schema/quality failure, not a suggestion. Fix the file.

OBJECTIVE: {tracker.objective}
Plan path: `{path or ".claude/goals/<id>/plan.md"}`

Gate failures (fix every line):
{iss or "- (unspecified — rewrite to schema)"}

{schema}

Rewrite the entire plan file to satisfy the schema. Do not implement product
code yet. Do not call update_goal(completed=true). End after writing plan.md.
</goal-planning>
"""


def implementer_kickoff(tracker: "GoalTracker") -> str:
    """First implementer turn after host accepts the plan."""
    excerpt = _plan_contract_excerpt(tracker)
    return (
        goal_rules(tracker.objective, plan_body=excerpt)
        + "\nThe host has ACCEPTED the plan (real contract, not a template). "
        "Execute it.\n"
        + "As you finish work, flip checklist boxes in the plan file on disk "
        f"(`{getattr(tracker, 'plan_path', '') or 'plan.md'}`): change "
        "`- [ ]` to `- [x]`. Host merges ONLY checkbox marks — rewriting "
        "Acceptance criteria / Verification plan on disk is a no-op and a "
        "manipulated plan cannot unlock complete.\n"
        + "Claim complete only with full-objective evidence against the FROZEN "
        "contract. Host then runs Task-mode verification requiring goal_verdict "
        "(prose alone cannot unlock).\n"
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


def _plan_judge_block(plan_body: str) -> str:
    if not (plan_body or "").strip():
        return ""
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
    return plan_block


def verifier_prompt(
    objective: str,
    claim: str,
    gaps_prior: Optional[List[str]] = None,
    plan_body: str = "",
) -> str:
    """Body for the *reviewer* subagent (Task/spawn_subagent child)."""
    prior = ""
    if gaps_prior:
        prior = "\nPrior gaps:\n" + "\n".join(f"- {g}" for g in gaps_prior[:6])
    plan_block = _plan_judge_block(plan_body)
    return f"""<goal-verifier>
You are a HOST SKEPTIC / REVIEWER subagent under the goal executor session.
You are NOT the implementer. Fresh judgment; default outcome is NOT achieved.
Do not implement features. Do NOT call update_goal.

OBJECTIVE:
{objective}

IMPLEMENTER CLAIM (untrusted):
{claim}
{prior}
{plan_block}

Rules:
1. Inspect with tools (read files, run tests/commands) first. Prose alone fails.
2. Judge ONLY against plan acceptance criteria / verification steps.
3. Partial / almost / LGTM → not achieved, list gaps.
4. Adversarial: missing proof = gap, not benefit of the doubt.

REQUIRED — structured verdict (not prose):

1) Prefer MCP ``goal_verdict`` on the sublime server (Grok: use_tool with
   tool_name=\"sublime__goal_verdict\" or \"goal_verdict\"):

  goal_verdict(
    achieved=false,   # or true only if every criterion is proven
    evidence=["criterion → path/test/command proof", ...],  # if achieved=true
    gaps=["what is still missing", ...],   # if achieved=false
    message="one-line summary"
  )

2) If this agent cannot call MCP (common for Task children), write:
   ``<plan-dir>/evidence/VERDICT.json`` with the same fields
   (achieved, evidence, gaps, message). Host ingests that file at verify end.

Host applies only structured tool or VERDICT.json. Omit both → not_achieved.
</goal-verifier>
"""


def executor_verify_prompt(
    objective: str,
    claim: str,
    gaps_prior: Optional[List[str]] = None,
    plan_body: str = "",
) -> str:
    """Main-session verify turn: flow *executor* fans out one Task reviewer.

    Single sheet: do not open a new session for the skeptic. Host unlocks
    complete only from structured ``goal_verdict``.
    """
    skeptic_body = verifier_prompt(
        objective, claim, gaps_prior=gaps_prior, plan_body=plan_body)
    return f"""<goal-executor-verify>
You are the GOAL FLOW EXECUTOR on this session (the only sheet the user watches).
A complete claim was deferred to host verification. This turn:

1. Spawn **exactly one** reviewer via Task / Agent / spawn_subagent
   (backend-native subagent under this session — never spawn_session / new sheet).
2. Description: ``goal achievement skeptic`` (or equivalent).
3. Give the subagent the full prompt inside <skeptic-prompt>…</skeptic-prompt>.
4. Wait until that subagent finishes.
5. Do not implement features. Do not call update_goal(completed=true).
6. You are not the judge — the reviewer is. Do not rubber-stamp the claim.
7. Prefer the reviewer calling MCP ``goal_verdict`` (or writing
   ``evidence/VERDICT.json`` next to plan.md). If the Task child cannot use
   MCP, either ensure VERDICT.json exists from the child, or call
   ``goal_verdict`` yourself using **only** its evidence (weak/no proof →
   achieved=false with gaps). Do not invent evidence the reviewer did not run.

OBJECTIVE: {objective}
CLAIM (untrusted): {claim}

<skeptic-prompt>
{skeptic_body}
</skeptic-prompt>
</goal-executor-verify>
"""


def resume_recap(tracker: "GoalTracker") -> str:
    excerpt = _plan_contract_excerpt(tracker)
    if tracker.phase == "planning" or not tracker.has_plan():
        return (
            f"Resuming goal {tracker.goal_id} still in planning.\n"
            + tracker.status_summary()
            + "\nHost will re-run planning until a concrete plan.md is accepted.\n"
        )
    return (
        goal_rules(tracker.objective, plan_body=excerpt)
        + f"\nResuming goal {tracker.goal_id}. Status was paused.\n"
        + tracker.status_summary()
        + "\nContinue from here against the plan contract.\n"
    )


def parse_verifier_verdict(text: str) -> tuple:
    """Return (achieved: bool, gaps: list[str]). Fail-closed default.

    Accepts only exact ``VERDICT: achieved`` / ``VERDICT: not_achieved`` (optional
    markdown wrappers). Substrings like "partially achieved" do **not** pass.
    Achieved is rejected if GAPS lists items or EVIDENCE is missing/empty.
    """
    if not text:
        return (False, ["Verifier produced no output"])

    achieved = None  # None = no valid verdict line
    gaps: List[str] = []
    evidence: List[str] = []
    in_gaps = False
    in_evidence = False

    def _strip_md(s: str) -> str:
        s = (s or "").strip()
        # unwrap **bold**, *italic*, `code`, surrounding quotes
        for _ in range(3):
            n = re.sub(r"^\*+|\*+$", "", s)
            n = re.sub(r"^`+|`+$", "", n)
            n = n.strip().strip("\"'")
            if n == s:
                break
            s = n
        return s.strip()

    for line in text.strip().splitlines():
        s = _strip_md(line)
        if not s:
            continue
        low = s.lower()

        if low.startswith("verdict:"):
            in_gaps = False
            in_evidence = False
            body = _strip_md(s.split(":", 1)[1])
            body_l = body.lower().strip()
            compact = re.sub(r"[\s\-]+", "_", body_l)
            compact = re.sub(r"[^a-z_]", "", compact)
            # Exact tokens only (fail closed on anything else)
            if compact in ("not_achieved", "notachieved", "failed", "fail", "no"):
                achieved = False
            elif compact in ("achieved", "pass", "passed", "yes", "ok"):
                achieved = True
            elif compact in (
                "partially_achieved", "almost_achieved", "partial", "almost",
                "mostly_achieved", "likely_achieved",
            ):
                achieved = False
                if "partial" not in " ".join(gaps).lower():
                    gaps.append("Partial/almost is not achieved")
            else:
                achieved = False
                gaps.append(f"Unrecognized verdict token: {body[:40]}")
            continue

        if low.startswith("gaps:"):
            in_gaps = True
            in_evidence = False
            rest = _strip_md(s.split(":", 1)[-1])
            if rest:
                gaps.append(rest.lstrip("-* ").strip())
            continue

        if low.startswith("evidence:"):
            in_evidence = True
            in_gaps = False
            rest = _strip_md(s.split(":", 1)[-1])
            if rest:
                evidence.append(rest.lstrip("-* ").strip())
            continue

        if in_gaps and (s.startswith("-") or s.startswith("*")):
            g = s.lstrip("-* ").strip()
            if g:
                gaps.append(g)
            continue

        if in_evidence and (s.startswith("-") or s.startswith("*")):
            e = s.lstrip("-* ").strip()
            if e:
                evidence.append(e)
            continue

    # Fail closed
    if achieved is not True:
        if not gaps:
            gaps = ["Verifier did not confirm achievement"]
        return (False, gaps[:12])

    # Achieved path: must not list gaps; must show evidence bullets
    if gaps:
        return (False, gaps[:12])
    if not evidence:
        return (False, [
            "VERDICT: achieved rejected — missing EVIDENCE block with concrete proof",
        ])
    return (True, [])
