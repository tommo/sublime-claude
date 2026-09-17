"""Kimi Agent result must not leave host ⚙ waiting for bash task_notification."""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SANDBOX = os.path.join(_ROOT, "sandbox", "kimi_agent")
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE, _SANDBOX):
    if p not in sys.path:
        sys.path.insert(0, p)

from host_gate import TestKimiAgentHostGate  # noqa: E402, F401


if __name__ == "__main__":
    unittest.main()
