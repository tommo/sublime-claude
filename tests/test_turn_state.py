"""CI entry for TurnState + host must not adopt leftover into working."""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SANDBOX = os.path.join(_ROOT, "sandbox", "busy_state")
for p in (_ROOT, _SANDBOX):
    if p not in sys.path:
        sys.path.insert(0, p)

from patterns import (  # noqa: E402,F401
    TestCompact,
    TestGitappAfterDone,
    TestInboundNeverAdopts,
    TestInterrupt,
    TestLiveCloser,
    TestParentNotify,
)


class TestHostDoesNotAdoptWorking(unittest.TestCase):
    def _live(self, name):
        with open(os.path.join(_ROOT, "session.py"), encoding="utf-8") as f:
            src = f.read()
        start = src.find(f"def {name}")
        self.assertGreater(start, 0)
        nxt = src.find("\n    def ", start + 10)
        out = []
        for line in src[start:nxt].splitlines():
            s = line.lstrip()
            if s.startswith("#"):
                continue
            out.append(line)
        return "\n".join(out)

    def test_adopt_does_not_assign_working(self):
        self.assertNotIn("self.working = True", self._live("_adopt_agent_turn"))

    def test_flush_does_not_soft_adopt(self):
        body = self._live("_flush_bg_notifications")
        self.assertNotIn("self._adopt_agent_turn(", body)
        self.assertNotIn("_bg_soft_fallback_query", body)


if __name__ == "__main__":
    unittest.main()
