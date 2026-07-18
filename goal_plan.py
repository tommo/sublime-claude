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

# Case-insensitive → canonical heading (agent title-case must still parse)
_CANONICAL_HEADING = {
    h.lower(): h for h in (REQUIRED_HEADINGS + OPTIONAL_HEADINGS)
}


def normalize_heading(raw: str) -> str:
    """Map agent heading variants to canonical section names."""
    key = (raw or "").strip().lower()
    return _CANONICAL_HEADING.get(key, (raw or "").strip())


def plan_schema_for_prompt() -> str:
    """Explicit plan.md contract for planner prompts (show the schema, don't make them guess)."""
    return """PLAN.MD SCHEMA (host accept gate — match exactly):

Required ## headings (case-insensitive OK; body must use these names):
  ## Acceptance criteria   — ≥2 numbered/bulleted criteria
  ## Verification plan     — ≥2 numbered/bulleted checks
Strongly recommended:
  ## Task checklist        — `- [ ]` items the implementer flips to `[x]`

Each acceptance criterion MUST name at least one concrete artifact, e.g.:
  - absolute or repo path (`/…/foo`, `src/…`, `.claude/goals/<id>/…`)
  - command (`pytest …`, `rg …`, `test -f …`, `git status`)
  - file/test/module/UI surface

Each verification step MUST be an executable or inspectable check (command,
path exists, file contents), not "make sure it works".

Minimal valid example (copy structure; replace with THIS objective):

```markdown
# Plan: <objective>

## Acceptance criteria
1. **Artifact**: path/to/output exists with expected content.
2. **Check**: `command that proves done` exits 0 (or documented success).

## Verification plan
1. `gating`: run that command; require exit 0.
2. `gating`: read/assert the artifact path/content.

## Task checklist
- [ ] Create the artifact
- [ ] Run verification and capture log
```

Reject reasons you will see if the gate fails:
  - missing required ## sections
  - fewer than 2 acceptance criteria or verification steps
  - criteria/steps with no concrete path/command/artifact
  - generic host template / objective-only restatement
"""


def is_generic_template_plan(plan_md: str) -> bool:
    """True if plan is the old host-side string template (not a real plan)."""
    text = plan_md or ""
    # Fingerprints from materialize_plan() stub — never accept as real work plan
    marks = (
        "Expand the objective into concrete code/docs changes, verify as you go",
        "Full multi-skeptic Grok classifier panel (optional later).",
        "Drive the real shipped entry points / pure functions that "
        "implement the objective; capture proof under a private scratch dir",
    )
    return sum(1 for m in marks if m in text) >= 2


# Paths, commands, evidence — not code-only (docs/demo goals must pass)
_CONCRETE_HINT = re.compile(
    r"("
    r"\.[a-zA-Z0-9]{1,8}\b|"          # file extension
    r"`[^`]+`|"                       # backtick command/path
    r"/[\w./\-]+|"                    # absolute/repo path fragment
    r"\bsrc/|\btests?/|\bdocs?/|"
    r"\.claude/|\.grok/|"
    r"\bpytest\b|\bnpm\b|\bcargo\b|\bgit\b|\brg\b|\bgrep\b|"
    r"\btest\s+-|\bmkdir\b|\bprintf\b|\bcat\b|\bls\b|"
    r"\bfile\b|\bpath\b|\bcommand\b|\bevidence\b|\bscratch\b|"
    r"\blog\b|\bmarker\b|\bartifact\b|\bexit\s*0\b|"
    r"\bapi\b|\bui\b|\bbutton\b|\bpanel\b|\bsession\b|\bbackend\b"
    r")",
    re.I,
)


def plan_quality_issues(plan_md: str, objective: str = "") -> List[str]:
    """Host gate: reject empty/template/vague plans. Empty list = acceptable.

    Issue strings are actionable (what to fix), not opaque status codes.
    """
    issues: List[str] = []
    body = (plan_md or "").strip()
    if not body:
        return [
            "plan is empty — write plan.md with ## Acceptance criteria and "
            "## Verification plan (see schema)"
        ]
    if not plan_has_required_sections(body):
        issues.append(
            "missing required headings exactly as "
            "`## Acceptance criteria` and `## Verification plan` "
            "(case-insensitive; must be level-2 ## headings on their own line)"
        )
    if is_generic_template_plan(body):
        issues.append(
            "generic host template rejected — write a plan for THIS objective "
            "with concrete paths/commands (not a fill-in shell)"
        )
    ac = extract_acceptance_criteria(body)
    vs = extract_verification_steps(body)
    if len(ac) < 2:
        issues.append(
            f"need ≥2 acceptance criteria under `## Acceptance criteria` "
            f"(found {len(ac)}; use numbered/bulleted lines)"
        )
    if len(vs) < 2:
        issues.append(
            f"need ≥2 verification steps under `## Verification plan` "
            f"(found {len(vs)}; each step = command or path check)"
        )
    if ac and not any(_CONCRETE_HINT.search(c or "") for c in ac):
        issues.append(
            "acceptance criteria need a concrete artifact in at least one line "
            "(path, `command`, evidence file, test, UI surface) — "
            "not only restating the objective"
        )
    if vs and not any(_CONCRETE_HINT.search(s or "") for s in vs):
        issues.append(
            "verification steps need a concrete check in at least one line "
            "(`pytest`/`rg`/`test -f`/path to read, etc.)"
        )
    obj = (objective or "").strip()
    if obj and body.count(obj) >= 5 and len(body) < len(obj) * 8:
        issues.append(
            "plan only restates the objective — expand into real steps "
            "with paths/commands"
        )
    return issues


def sample_concrete_plan(
    objective: str,
    goal_id: str = "test",
    *,
    checklist_done: bool = False,
) -> str:
    """Concrete plan fixture for unit tests (passes plan_quality_issues).

    checklist_done=True marks all tasks [x] so complete-claim preflight can pass.
    """
    obj = (objective or "test objective").strip()
    oid = (goal_id or "test").strip()
    box = "[x]" if checklist_done else "[ ]"
    return "\n".join([
        f"# Plan: {obj}",
        "",
        "## Goal kind",
        "code-change",
        "",
        "## Acceptance criteria",
        f"1. **Code path**: Implementation lives in `src/goal_feature.py` (or package equivalent) for: {obj}",
        "2. **Tests**: `tests/test_goal_feature.py` covers the happy path and one failure case via pytest.",
        "3. **CLI/UI**: User-visible entry (command or function) exercises the feature without manual steps.",
        "",
        "## Verification plan",
        "1. `gating`: `pytest tests/test_goal_feature.py -q` exits 0; save log under "
        f"`.claude/goals/{oid}/pytest.log`.",
        "2. `gating`: `rg -n \"def \" src/goal_feature.py` shows the shipped entry points.",
        "3. `evidence`: Run the CLI/one-liner once; capture stdout in "
        f"`.claude/goals/{oid}/smoke.txt`.",
        "",
        "## Task checklist",
        f"- {box} Implement core logic for: {obj}",
        f"- {box} Add pytest coverage in tests/test_goal_feature.py",
        f"- {box} Wire public entry point and run smoke command",
        "",
    ])


def materialize_plan(
    objective: str,
    *,
    goal_kind: str = "code-change",
    goal_id: str = "",
) -> str:
    """DEPRECATED stub plan for offline unit tests only.

    Live goal mode must NOT use this as the product plan — it is a generic
    template. Prefer model-written plans validated by plan_quality_issues().
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
    """Split plan markdown into ## heading → body (stripped).

    Known goal headings are normalized to canonical names so
    ``## Acceptance Criteria`` still extracts as ``Acceptance criteria``.
    """
    text = plan_md or ""
    sections: Dict[str, str] = {}
    current: Optional[str] = None
    buf: List[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = normalize_heading(m.group(1))
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def _section_body(plan_md: str, canonical: str) -> str:
    secs = parse_plan_sections(plan_md)
    if secs.get(canonical):
        return secs[canonical]
    # Fallback: any key that normalizes to canonical
    want = canonical.lower()
    for k, v in secs.items():
        if (k or "").strip().lower() == want:
            return v
    return ""


def extract_acceptance_criteria(plan_md: str) -> List[str]:
    """List bullet/numbered criteria lines from ## Acceptance criteria."""
    body = _section_body(plan_md, "Acceptance criteria")
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
    body = _section_body(plan_md, "Verification plan")
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


_CHECKBOX_LINE = re.compile(
    r"^(\s*[-*]\s*)\[([ xX])\](\s+)(.+?)\s*$"
)


def _normalize_checklist_key(text: str) -> str:
    """Stable key for matching checklist items across frozen vs disk plans."""
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t


def open_checklist_items(plan_md: str) -> List[str]:
    """Unchecked task checklist lines from ## Task checklist (or whole plan)."""
    secs = parse_plan_sections(plan_md)
    body = secs.get("Task checklist") or ""
    # Also scan whole plan for `- [ ]` in case checklist heading varies
    scan = body if body.strip() else (plan_md or "")
    out: List[str] = []
    for line in scan.splitlines():
        m = re.match(r"^\s*[-*]\s*\[\s*\]\s+(.+)$", line)
        if m:
            item = m.group(1).strip()
            if item:
                out.append(item)
    return out


def checklist_marks_from_plan(plan_md: str) -> Dict[str, bool]:
    """Map normalized item text → done (True if [x]/[X])."""
    secs = parse_plan_sections(plan_md)
    body = secs.get("Task checklist") or ""
    scan = body if body.strip() else (plan_md or "")
    marks: Dict[str, bool] = {}
    for line in scan.splitlines():
        m = _CHECKBOX_LINE.match(line)
        if not m:
            continue
        done = m.group(2).lower() == "x"
        key = _normalize_checklist_key(m.group(4))
        if key:
            # Prefer done=True if any disk line marks it done
            marks[key] = marks.get(key, False) or done
    return marks


def merge_checklist_marks(frozen_plan: str, disk_plan: str) -> Tuple[str, bool]:
    """Apply disk ``[x]`` marks onto frozen plan checklist only.

    Acceptance criteria, verification plan, and checklist *items* stay from the
    accepted contract. Disk may only flip checkbox state for items whose text
    matches a frozen checklist entry. Deleting the checklist or thinning AC on
    disk does not change the frozen contract (open items remain open).

    Returns (merged_plan_md, changed).
    """
    frozen = frozen_plan or ""
    if not frozen.strip():
        return frozen, False
    disk_marks = checklist_marks_from_plan(disk_plan or "")
    if not disk_marks:
        return frozen if frozen.endswith("\n") or not frozen else frozen + "\n", False

    changed = False
    out_lines: List[str] = []
    for line in frozen.splitlines():
        m = _CHECKBOX_LINE.match(line)
        if not m:
            out_lines.append(line)
            continue
        prefix, mark, sp, text = m.group(1), m.group(2), m.group(3), m.group(4)
        key = _normalize_checklist_key(text)
        if key in disk_marks and disk_marks[key] and mark.strip().lower() != "x":
            out_lines.append(f"{prefix}[x]{sp}{text}")
            changed = True
        else:
            out_lines.append(line)
    merged = "\n".join(out_lines)
    if frozen.endswith("\n"):
        merged += "\n"
    elif merged and not merged.endswith("\n"):
        merged += "\n"
    return merged, changed


# Claim language that means "partial round" not "objective fully done"
_PARTIAL_CLAIM_RE = re.compile(
    r"\b("
    r"one\s+sprint|first\s+slice|thin\s+but|partial(?:ly)?|"
    r"deferred|defer(?:red)?|not\s+yet|wip|prototype\s+only|"
    r"easy\s+slice|round\s+1|phase\s+1\s+only|mvp\s+only|"
    r"as\s+a\s+start|initial\s+pass|soft\s+complete|interim|"
    r"remaining\s+work|still\s+need|follow[- ]?on|out\s+of\s+scope\s+for\s+now|"
    r"not\s+(?:fully|completely)\s+(?:done|achieved|complete)|"
    r"good\s+enough\s+for\s+(?:now|this\s+round)"
    r")\b",
    re.I,
)


def partial_claim_language(claim: str) -> Optional[str]:
    """If claim text is deferral/partial, return matched phrase; else None."""
    text = (claim or "").strip()
    if not text:
        return None
    m = _PARTIAL_CLAIM_RE.search(text)
    return m.group(0) if m else None


def complete_claim_preflight(
    claim: str,
    plan_md: str,
    objective: str = "",
) -> List[str]:
    """Host gates before scheduling verification. Empty list = may proceed.

    Blocks soft-complete theater: open checklist items, partial/deferral claim
    language, and plans that drop the north-star objective without concrete
    criteria still covering it.
    """
    issues: List[str] = []
    claim = (claim or "").strip()
    plan_md = plan_md or ""
    objective = (objective or "").strip()

    partial = partial_claim_language(claim)
    if partial:
        issues.append(
            f"claim is partial/deferral language ({partial!r}) — "
            "finish the objective or /goal pause; do not complete"
        )

    open_items = open_checklist_items(plan_md)
    if open_items:
        preview = "; ".join(open_items[:3])
        more = f" (+{len(open_items) - 3} more)" if len(open_items) > 3 else ""
        issues.append(
            f"{len(open_items)} open checklist item(s) remain: {preview}{more}"
        )

    # North star: acceptance criteria must still engage the objective wording
    # (or the plan body must still state the objective as the goal).
    if objective:
        ac = extract_acceptance_criteria(plan_md)
        obj_l = objective.lower()
        # Significant tokens from objective (len>=4) should appear in criteria/plan
        tokens = [
            t for t in re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]{3,}", objective)
            if t.lower() not in {
                "with", "that", "this", "from", "into", "using", "make",
                "have", "will", "should", "would", "could", "just", "only",
                "more", "like", "level", "close", "full", "real",
            }
        ]
        # Use unique lower tokens, cap
        seen = set()
        uniq = []
        for t in tokens:
            tl = t.lower()
            if tl not in seen:
                seen.add(tl)
                uniq.append(tl)
        uniq = uniq[:12]
        if uniq:
            blob = ("\n".join(ac) + "\n" + plan_md).lower()
            hits = sum(1 for t in uniq if t in blob)
            # If objective is multi-token ambition and almost none appear in plan, reject complete
            if len(uniq) >= 3 and hits < max(2, len(uniq) // 3):
                issues.append(
                    "plan/criteria barely mention objective keywords — "
                    "north-star objective was diluted; re-plan or finish full scope"
                )

    # Quality re-check on frozen plan
    issues.extend(plan_quality_issues(plan_md, objective))
    return issues


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
