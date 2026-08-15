"""Fail if session.py still claims busy from leftover inbound."""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def _live_body(src: str, name: str) -> str:
    start = src.find(f"def {name}")
    nxt = src.find("\n    def ", start + 10)
    lines = []
    for line in src[start:nxt].splitlines():
        s = line.lstrip()
        if s.startswith("#"):
            continue
        if "#" in line:
            line = line[:line.index("#")]
        lines.append(line)
    return "\n".join(lines)


src = open(os.path.join(_ROOT, "session.py"), encoding="utf-8").read()
bad = []
adopt = _live_body(src, "_adopt_agent_turn")
if "self.working = True" in adopt:
    bad.append("_adopt_agent_turn sets working=True")
flush = _live_body(src, "_flush_bg_notifications")
if "self._adopt_agent_turn(" in flush:
    bad.append("_flush_bg_notifications still soft-adopts")
if bad:
    print("FAIL:", "; ".join(bad))
    sys.exit(1)
print("ok: leftover inbound cannot own busy")
