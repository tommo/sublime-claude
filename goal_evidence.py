"""Host-side goal evidence validation (fail-closed).

Agents game green checklists by:
  1. Shipping minimal stubs with full technique names (\"Hi-Z\", \"froxel\")
  2. Logging green `pil test` / unit counts
  3. Putting residuals in message as \"non-blockers\" with gaps=[]
  4. Claiming achieved

Host must reject narrative-only evidence, require on-disk artifacts, and for
visual/render goals require image captures — not code structure alone.
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Sequence, Tuple

# Minimum size for a log/capture to count as real (not empty touch)
_MIN_LOG_BYTES = 40
_MIN_IMAGE_BYTES = 200

_IMAGE_EXT = (
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tga", ".exr", ".hdr",
)
_LOG_EXT = (".log", ".txt", ".out", ".json", ".jsonl")

# Prose that is not proof
_NARRATIVE_PREFIX = re.compile(
    r"^\s*(?:"
    r"structure|api|readmes?|prior|residual|pattern|non-?goals?|"
    r"claimed|honest|v1\s+scope|documented|later|"
    r"implementation\s+approach|assumed\s+scope"
    r")\s*[:—\-]",
    re.I,
)

# Paths that look like files (evidence/foo.log, /abs/path, rel/path.ext)
_PATH_TOKEN = re.compile(
    r"(?:"
    r"`([^`\n]{3,200})`"  # backticks
    r"|(?<![\w/])((?:/?[\w.~-]+/)+[\w.~-]+\.[A-Za-z0-9]{1,8})"  # path with ext
    r"|(?<![\w/])(evidence/[\w./~-]+)"
    r")"
)

# Visual / graphics domain — need captures, not only green tests
_VISUAL_GOAL = re.compile(
    r"\b(?:"
    r"ssr|ssao|taa|fsr|hiz|hi-?z|froxel|fog|shadow|render|shader|gfx|"
    r"gbuffer|pass|pipeline|visual|capture|screenshot|image|pixel|"
    r"bloom|ao|reflection|refraction|lighting|spot\s*light|forward|"
    r"velocity|mip|texture|viewport|framebuffer|post[- ]?process"
    r")\b",
    re.I,
)

# Marketing technique names that must not be proven by unit OK alone
_TECHNIQUE_CLAIM = re.compile(
    r"\b(?:"
    r"hi-?z|hierarchical\s*z|froxel|object\s*velocity|"
    r"screen[- ]?space\s*reflection|temporal\s*aa|fsr\s*1?"
    r")\b",
    re.I,
)

# Message laundering residuals while claiming complete
_RESIDUAL_LAUNDER = re.compile(
    r"(?:"
    r"non-?blockers?|residual|later|"
    r"documented(?:\s+only)?|"
    r"not\s+(?:true|real|full)|"
    r"stub|placeholder|mip0-?only|camera[- ]only|"
    r"v1\s+scope|honest\s+notes?"
    r")",
    re.I,
)


def goal_is_visual(objective: str = "", plan_body: str = "") -> bool:
    text = f"{objective or ''}\n{plan_body or ''}"
    return bool(_VISUAL_GOAL.search(text))


def extract_path_candidates(item: str) -> List[str]:
    """Pull path-like tokens from an evidence line."""
    if not item:
        return []
    found: List[str] = []
    for m in _PATH_TOKEN.finditer(item):
        for g in m.groups():
            if g:
                found.append(g.strip().strip("`'\"(),;"))
    # Bare filename.ext
    for m in re.finditer(r"(?<![\w/])([\w.-]+\.(?:png|jpe?g|webp|log|txt|json))\b", item, re.I):
        found.append(m.group(1))
    # Dedupe preserve order
    out: List[str] = []
    seen = set()
    for p in found:
        if p and p not in seen and len(p) >= 4:
            seen.add(p)
            out.append(p)
    return out


def resolve_existing_file(
    path: str,
    roots: Sequence[str],
) -> Optional[str]:
    """Return absolute path if file exists under roots or as absolute."""
    if not path:
        return None
    path = path.strip().strip("`\"'")
    candidates = []
    if os.path.isabs(path):
        candidates.append(path)
    for root in roots:
        if not root:
            continue
        candidates.append(os.path.join(root, path))
        # evidence/foo relative to plan parent
        candidates.append(os.path.join(root, "evidence", os.path.basename(path)))
    for c in candidates:
        try:
            ap = os.path.abspath(c)
            if os.path.isfile(ap):
                return ap
        except Exception:
            continue
    return None


def classify_artifact(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _LOG_EXT:
        return "log"
    return "file"


def validate_evidence_for_achieved(
    evidence: Sequence[str],
    *,
    message: str = "",
    objective: str = "",
    plan_body: str = "",
    plan_path: str = "",
    cwd: str = "",
) -> Tuple[bool, List[str], List[str]]:
    """Host gate for achieved=true.

    Returns (ok, host_gaps, notes).
    ok False → caller must flip achieved and merge host_gaps into gaps.
    """
    host_gaps: List[str] = []
    notes: List[str] = []
    items = [str(x).strip() for x in (evidence or []) if str(x).strip()]
    if not items:
        return False, ["Host: achieved requires non-empty evidence[]"], notes

    # Residual laundering in message while claiming done
    msg = (message or "").strip()
    if msg and _RESIDUAL_LAUNDER.search(msg):
        host_gaps.append(
            "Host: message admits residual/stub/non-blocker work while "
            "claiming achieved — put those in gaps[] or finish the real feature "
            "(no green-checklist theater)"
        )

    plan_dir = ""
    if plan_path:
        plan_dir = os.path.dirname(os.path.abspath(plan_path))
    evid_dir = os.path.join(plan_dir, "evidence") if plan_dir else ""
    roots = [r for r in (cwd, plan_dir, evid_dir) if r]

    grounded = 0  # lines with at least one existing file
    image_hits = 0
    log_hits = 0
    narrative_only = 0
    missing_paths: List[str] = []

    for item in items:
        if _NARRATIVE_PREFIX.match(item) and not extract_path_candidates(item):
            narrative_only += 1
            continue
        paths = extract_path_candidates(item)
        if not paths:
            # No path token at all — pure prose claim
            narrative_only += 1
            continue
        found_any = False
        for p in paths:
            resolved = resolve_existing_file(p, roots)
            if not resolved:
                missing_paths.append(p)
                continue
            try:
                size = os.path.getsize(resolved)
            except OSError:
                continue
            kind = classify_artifact(resolved)
            if kind == "image" and size >= _MIN_IMAGE_BYTES:
                image_hits += 1
                found_any = True
            elif kind == "log" and size >= _MIN_LOG_BYTES:
                log_hits += 1
                found_any = True
            elif size >= _MIN_LOG_BYTES:
                found_any = True
        if found_any:
            grounded += 1

    if narrative_only and grounded == 0:
        host_gaps.append(
            "Host: evidence[] is narrative/structure claims only — "
            "cite real files under evidence/ (logs, captures) that exist on disk"
        )
    elif grounded == 0:
        miss = ", ".join(missing_paths[:5])
        more = f" (+{len(missing_paths) - 5} more)" if len(missing_paths) > 5 else ""
        host_gaps.append(
            "Host: no evidence line points to an existing non-empty file"
            + (f" (missing: {miss}{more})" if miss else "")
        )
    else:
        notes.append(f"grounded={grounded}/{len(items)} images={image_hits} logs={log_hits}")

    # Visual goals: green tests alone are insufficient
    visual = goal_is_visual(objective, plan_body)
    if visual and image_hits < 1:
        host_gaps.append(
            "Host: visual/render goal requires ≥1 on-disk image capture "
            "(png/jpg/…) in evidence — code structure + green unit tests are not enough"
        )
    if visual and grounded < 2:
        host_gaps.append(
            "Host: visual goal needs ≥2 grounded evidence lines "
            "(e.g. capture + command log), not a single green checklist claim"
        )

    # Technique-name claims without capture/log proof
    blob = "\n".join(items) + "\n" + msg
    if _TECHNIQUE_CLAIM.search(blob) and image_hits < 1 and log_hits < 1:
        host_gaps.append(
            "Host: technique names (Hi-Z/froxel/object-velocity/SSR/TAA/…) "
            "require proof artifacts; rename stubs honestly or ship the real feature"
        )

    # ARSENAL/✅ marketing without files
    if re.search(r"✅|ARSENAL", blob, re.I) and grounded < 1:
        host_gaps.append(
            "Host: ARSENAL/✅ claims without on-disk evidence are rejected"
        )

    ok = len(host_gaps) == 0
    return ok, host_gaps, notes
