"""Host-owned goal plan materialization and light parsing (no Sublime).

Plan schema is compatible with Grok Build frozen plan sections so implementer
and verifier share one contract.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple


REQUIRED_HEADINGS = (
    "Acceptance criteria",
    "Verification plan",
)

OPTIONAL_HEADINGS = (
    "Goal kind",
    "Non-goals",
    "Assumed scope",
    "Implementation approach",
    "Task checklist",
)


def materialize_plan(
    objective: str,
    *,
    goal_kind: str = "code-change",
    goal_id: str = "",
) -> str:
    """Expand a free-form objective into a structured plan markdown document.

    Host-owned: deterministic, no model call. Criteria and verification steps
    are derived from the objective so offline tests can assert structure without
    hardcoding a specific product feature string beyond the objective itself.
    """
    obj = (objective or "").strip()
    if not obj:
        raise ValueError("objective required")
    kind = (goal_kind or "code-change").strip() or "code-change"
    oid = (goal_id or "goal").strip()
    # One primary acceptance criterion is the objective; plus structural bars.
    lines = [
        f"# Plan: {obj}",
        "",
        "## Goal kind",
        kind,
        "",
        "## Acceptance criteria",
        f"1. **Objective delivered**: {obj}",
        "2. **Evidence exists**: Real files, tests, or command outputs demonstrate "
        "the change (not prose claims alone).",
        "3. **No silent regressions**: Relevant checks that already existed still "
        "pass, or failures are explicitly scoped as non-goals.",
        "",
        "## Verification plan",
        "1. `gating`: Drive the real shipped entry points / pure functions that "
        "implement the objective; capture proof under a private scratch dir "
        f"(goal id `{oid}`). Fail if only narration exists.",
        "2. `gating`: Assert acceptance criterion 1 is met with concrete artifacts "
        "(paths, test names, or command output) — not a restatement of the claim.",
        "3. `gating`: Run the smallest relevant automated check suite for touched "
        "modules; capture the run log.",
        "4. `evidence`: Optional UI/smoke notes; honest skip if not applicable.",
        "",
        "## Non-goals",
        "- Unrelated refactors outside the objective.",
        "- Full multi-skeptic Grok classifier panel (optional later).",
        "",
        "## Assumed scope",
        f"- Objective as stated: {obj}",
        "- Host goal harness continues until verified complete or paused.",
        "",
        "## Implementation approach",
        "Expand the objective into concrete code/docs changes, verify as you go, "
        "and only claim complete when the verification plan can pass.",
        "",
        "## Task checklist",
        f"- [ ] Deliver: {obj}",
        "- [ ] Add or extend offline tests for shipped entry points",
        "- [ ] Capture verification evidence and claim complete with proof",
        "",
    ]
    return "\n".join(lines)


def plan_has_required_sections(plan_md: str) -> bool:
    """True if plan text includes required Grok-compatible section headings."""
    if not (plan_md or "").strip():
        return False
    text = plan_md
    for h in REQUIRED_HEADINGS:
        if not re.search(rf"(?im)^##\s*{re.escape(h)}\s*$", text):
            return False
    return True


def parse_plan_sections(plan_md: str) -> Dict[str, str]:
    """Split plan markdown into ## heading → body (stripped)."""
    text = plan_md or ""
    sections: Dict[str, str] = {}
    current: Optional[str] = None
    buf: List[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1).strip()
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def extract_acceptance_criteria(plan_md: str) -> List[str]:
    """List bullet/numbered criteria lines from ## Acceptance criteria."""
    secs = parse_plan_sections(plan_md)
    body = secs.get("Acceptance criteria") or ""
    out: List[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        # 1. **x** or - item
        s = re.sub(r"^\d+\.\s*", "", s)
        s = re.sub(r"^[-*]\s+", "", s)
        if s:
            out.append(s)
    return out


def extract_verification_steps(plan_md: str) -> List[str]:
    secs = parse_plan_sections(plan_md)
    body = secs.get("Verification plan") or ""
    out: List[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^\d+\.\s*", "", s)
        s = re.sub(r"^[-*]\s+", "", s)
        if s:
            out.append(s)
    return out


def default_plan_path(project_root: Optional[str], goal_id: str) -> str:
    """Plugin convention: ``{project}/.claude/goals/{goal_id}/plan.md``."""
    gid = (goal_id or "goal").strip() or "goal"
    root = (project_root or ".").rstrip(os.sep)
    return os.path.join(root, ".claude", "goals", gid, "plan.md")


def write_plan_file(path: str, plan_md: str) -> str:
    """Write plan markdown; return absolute path."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(plan_md if plan_md.endswith("\n") else plan_md + "\n")
    return path


def load_plan_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def try_extract_plan_from_model_text(text: str) -> Optional[str]:
    """If a model planning turn emitted a fenced or ##-headed plan, extract it."""
    if not text:
        return None
    # Prefer fenced block marked plan
    m = re.search(
        r"```(?:markdown|md|plan)?\s*\n(#\s*Plan:[\s\S]*?)```",
        text,
        re.IGNORECASE,
    )
    if m:
        body = m.group(1).strip()
        if plan_has_required_sections(body):
            return body
    # Whole-message plan starting with # Plan:
    m2 = re.search(r"(#\s*Plan:[\s\S]+)", text)
    if m2:
        body = m2.group(1).strip()
        if plan_has_required_sections(body):
            return body
    if plan_has_required_sections(text):
        return text.strip()
    return None
