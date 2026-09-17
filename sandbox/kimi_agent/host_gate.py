#!/usr/bin/env python3
"""What the ACP host would paint for a Kimi Agent tool_call / result.

No live kimi. Uses the same classmethods as the bridge.
"""
from __future__ import annotations

import os
import re
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_HERE, _ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from acp_base import AcpBridge  # noqa: E402
from kimi_bg import KimiBgMixin  # noqa: E402
from kimi_main import KimiBridge  # noqa: E402

_FIX = os.path.join(_HERE, "fixtures", "agent_result.txt")
_AGENT_DONE_RE = re.compile(
    r"agent_id:\s*(agent-\S+)\s*\n"
    r"(?:actual_subagent_type:\s*.+\n)?"
    r"status:\s*(completed|failed|cancelled|canceled)",
    re.I,
)


def canonicalize(name: str, title: str = "") -> str:
    key = name or title
    return KimiBridge.TOOL_TO_CANONICAL.get(key, key) or key


def verdict(title: str, name: str, raw: dict, result_text: str,
            result_status: str = "completed") -> dict:
    """Host ⚙ / closer decision for one Agent row."""
    canon = canonicalize(name, title)
    upd = {"title": title}
    spawn = KimiBridge._is_subagent_spawn(canon, upd, raw)
    look_bg = AcpBridge._looks_like_background_tool(upd, raw)
    paint_bg = bool(spawn or look_bg)
    sub_name = AcpBridge._is_subagent_tool_name(canon)
    keep_spinner = (
        paint_bg
        and result_status == "completed"
        and sub_name
    )
    parsed_bash = KimiBgMixin.parse_kimi_bg_result_text(result_text or "")
    m = _AGENT_DONE_RE.search(result_text or "")
    agent_id = m.group(1) if m else None
    agent_status = (m.group(2) or "").lower() if m else None
    leftover_gear = bool(
        keep_spinner
        and not parsed_bash
        and agent_status in ("completed", "failed", "cancelled", "canceled")
    )
    return {
        "canon": canon,
        "spawn": spawn,
        "look_bg": look_bg,
        "paint_bg": paint_bg,
        "keep_spinner": keep_spinner,
        "parsed_bash": parsed_bash,
        "agent_id": agent_id,
        "agent_status": agent_status,
        "leftover_gear": leftover_gear,
    }


class TestKimiAgentHostGate(unittest.TestCase):
    def test_agent_maps_to_task_but_does_not_leave_gear(self):
        with open(_FIX, encoding="utf-8") as f:
            text = f.read()
        v = verdict("Agent", "Agent", {"description": "explore"}, text)
        self.assertEqual(v["canon"], "Task")
        self.assertFalse(v["spawn"])
        self.assertFalse(v["paint_bg"])
        self.assertEqual(v["agent_id"], "agent-0")
        self.assertEqual(v["agent_status"], "completed")
        self.assertIsNone(v["parsed_bash"])
        self.assertFalse(v["leftover_gear"])
        launch = verdict(
            "Launching explore agent: Sandbox connectivity test",
            "Agent", {}, text)
        self.assertFalse(launch["spawn"])
        self.assertFalse(launch["leftover_gear"])

    def test_grok_spawn_subagent_still_bg(self):
        v_title = AcpBridge._is_subagent_spawn(
            "Task", {"title": "spawn_subagent"}, {})
        self.assertTrue(v_title)

    def test_bash_bg_result_is_not_agent(self):
        text = (
            "task_id: bash-abc123\n"
            "pid: 1\n"
            "description: sleep\n"
            "status: running\n"
        )
        v = verdict("Running: sleep", "Bash", {"command": "sleep 1"}, text)
        self.assertFalse(v["spawn"])
        self.assertFalse(v["leftover_gear"])


def main() -> int:
    v = verdict(
        "Agent", "Agent", {},
        open(_FIX, encoding="utf-8").read(),
    )
    print("canon", v["canon"])
    print("paint_bg", v["paint_bg"])
    print("keep_spinner", v["keep_spinner"])
    print("agent_id", v["agent_id"], "status", v["agent_status"])
    print("parsed_bash", v["parsed_bash"])
    print("leftover_gear", v["leftover_gear"])
    if v["leftover_gear"]:
        print("FAIL host would keep ⚙ after Kimi Agent completed")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        sys.argv = [sys.argv[0]]
        unittest.main()
    else:
        raise SystemExit(main())
