#!/usr/bin/env python3
"""Assert Kimi resume preview can parse a real wire + the fixture."""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from resume_preview import parse_kimi_wire, find_kimi_wire, select_preview  # noqa: E402


def main() -> int:
    fixture = os.path.join(_ROOT, "tests", "fixtures", "kimi_wire_preview.jsonl")
    turns = parse_kimi_wire(fixture)
    if len(turns) != 2 or "performance" not in turns[0]["prompt"]:
        print("FAIL fixture parse", turns)
        return 1
    print("fixture turns", len(turns), [t["prompt"][:40] for t in turns])

    sid = "session_cf25c592-d78b-4d1c-bfb1-774ded6bf5ac"
    path = find_kimi_wire(sid)
    if path:
        live = parse_kimi_wire(path)
        chosen = select_preview(live)
        print("live", path)
        print("live turns", len(live), "preview", len(chosen))
        if live and not chosen:
            print("FAIL live preview empty")
            return 1
        if live:
            print("last prompt", live[-1]["prompt"][:80])
            print("last reply", (live[-1]["reply"] or "")[:80])
    else:
        print("no live session on disk (ok)")
    print("kimi resume preview parses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
