"""Page Edit/Write rows from a session transcript (parent → child)."""
from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional

EDIT_TOOL_NAMES = frozenset({
    "Edit", "Write", "edit", "write",
    "StrReplace", "ApplyPatch", "NotebookEdit",
})
_HUNK = re.compile(r"^@@\s+-(\d+)", re.MULTILINE)
_MAX_LIMIT = 40
_DEFAULT_LIMIT = 10
_DIFF_CAP = 4000


def _tool_name(ev: Any) -> str:
    return str(getattr(ev, "name", "") or "")


def _tool_input(ev: Any) -> dict:
    inp = getattr(ev, "tool_input", None)
    return inp if isinstance(inp, dict) else {}


def _path_of(inp: dict) -> str:
    return str(
        inp.get("file_path")
        or inp.get("path")
        or inp.get("target_file")
        or inp.get("filePath")
        or inp.get("notebook_path")
        or ""
    )


def _line_of(inp: dict) -> Optional[int]:
    unified = inp.get("unified_diff") or ""
    if isinstance(unified, str):
        m = _HUNK.search(unified)
        if m:
            n = int(m.group(1))
            if n > 0:
                return n
    for k in ("line_num", "line", "offset"):
        v = inp.get(k)
        try:
            n = int(v)
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    return None


def _diff_of(inp: dict) -> str:
    unified = inp.get("unified_diff")
    if isinstance(unified, str) and unified.strip():
        return unified
    old = inp.get("old_string") or inp.get("oldText") or ""
    new = inp.get("new_string") or inp.get("newText") or inp.get("content") or ""
    if not old and not new:
        return ""
    import difflib
    lines = list(difflib.unified_diff(
        str(old).splitlines(),
        str(new).splitlines(),
        lineterm="",
    ))
    return "\n".join(lines)


def collect_session_edits(conversations: Iterable) -> List[dict]:
    """Chronological Edit/Write rows from conversation events."""
    out = []
    i = 0
    for conv in conversations or []:
        events = getattr(conv, "events", None) or []
        for ev in events:
            name = _tool_name(ev)
            if name not in EDIT_TOOL_NAMES:
                continue
            status = str(getattr(ev, "status", "") or "")
            if status in ("pending", "background"):
                continue
            inp = _tool_input(ev)
            path = _path_of(inp)
            if not path:
                continue
            diff = _diff_of(inp)
            truncated = False
            if len(diff) > _DIFF_CAP:
                diff = diff[:_DIFF_CAP] + "\n… (diff truncated)"
                truncated = True
            out.append({
                "i": i,
                "tool": name,
                "status": status or "done",
                "file_path": path,
                "line": _line_of(inp),
                "diff": diff,
                "truncated": truncated,
                "id": getattr(ev, "id", None),
            })
            i += 1
    return out


def conversations_of(session: Any) -> list:
    output = getattr(session, "output", None)
    if output is None:
        return []
    convs = list(getattr(output, "conversations", None) or [])
    cur = getattr(output, "current", None)
    if cur is not None:
        convs.append(cur)
    return convs


def page_edits(
    edits: List[dict],
    *,
    offset: int = 0,
    limit: int = _DEFAULT_LIMIT,
    file_path: str = None,
) -> dict:
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(limit if limit is not None else _DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    if limit <= 0:
        limit = _DEFAULT_LIMIT
    limit = min(limit, _MAX_LIMIT)
    needle = (file_path or "").strip()
    if needle:
        filtered = [
            e for e in edits
            if e.get("file_path") == needle
            or str(e.get("file_path") or "").endswith(needle)
        ]
    else:
        filtered = list(edits)
    total = len(filtered)
    slice_ = filtered[offset:offset + limit]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "count": len(slice_),
        "has_more": offset + len(slice_) < total,
        "edits": slice_,
    }
