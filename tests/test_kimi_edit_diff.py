"""Kimi Edit: JSON-drip + completed text, no ACP type=diff — still print a patch."""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

import acp_base
from acp_base import AcpBridge


class _Fwd(AcpBridge):
    def __init__(self):
        self.session_id = "session_test"
        self._loading_session = False
        self._foreign_session_drops = 0
        self._calls = {}
        self._tool_id_alias = {}
        self._pending_execute_ids = []
        self._last_execute_id = None
        self._terminals = {}
        self._terminal_bg = {}
        self._released_terminals = set()
        self._detached_snaps = {}
        self._detached_procs = {}
        self._child_sessions = {}
        self._bg_notified_tasks = set()
        self._bg_notified_tools = set()
        self._prompt_fut = None
        self._prompt_cancelled = False
        self._cancel_in_flight = False
        self.TOOL_TO_CANONICAL = dict(AcpBridge.TOOL_TO_CANONICAL)

    def file_log(self, msg):
        pass


def _fwd(updates):
    notes = []
    orig = acp_base.send_notification
    acp_base.send_notification = lambda m, p: notes.append((m, p))
    try:
        b = _Fwd()
        for u in updates:
            b._forward_update({"sessionId": "session_test", "update": u})
        return notes, b
    finally:
        acp_base.send_notification = orig


class TestKimiEditDiff(unittest.TestCase):
    def test_drip_then_rawinput_then_completed_text_has_unified_diff(self):
        tid = "0:tool_edit"
        old = "proc foo(): int =\n  1\n"
        new = "proc foo(): int =\n  2\n"
        notes, b = _fwd([
            {
                "sessionUpdate": "tool_call",
                "toolCallId": tid,
                "title": "Edit",
                "kind": "edit",
                "status": "pending",
                "content": [{"type": "content",
                             "content": {"type": "text", "text": ""}}],
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tid,
                "status": "in_progress",
                "content": [{"type": "content", "content": {
                    "type": "text",
                    "text": '{"path": "/x.nim", "old_string": "proc',
                }}],
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tid,
                "title": "Editing /x.nim",
                "kind": "edit",
                "status": "in_progress",
                "rawInput": {
                    "path": "/x.nim",
                    "old_string": old,
                    "new_string": new,
                },
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tid,
                "status": "completed",
                "content": [{"type": "content", "content": {
                    "type": "text",
                    "text": "Replaced 1 occurrence in /x.nim",
                }}],
            },
        ])
        uses = [p for _, p in notes if p.get("type") == "tool_use"]
        self.assertGreaterEqual(len(uses), 2, uses)
        last_edit = [p for p in uses if p.get("name") == "Edit"][-1]
        inp = last_edit.get("input") or {}
        self.assertEqual(inp.get("file_path"), "/x.nim")
        self.assertTrue(
            inp.get("unified_diff") or (inp.get("old_string") and inp.get("new_string")),
            inp)
        if inp.get("unified_diff"):
            self.assertIn("-  1", inp["unified_diff"])
            self.assertIn("+  2", inp["unified_diff"])
        results = [p for _, p in notes if p.get("type") == "tool_result"]
        self.assertEqual(len(results), 1, results)

    def test_grok_type_diff_on_tool_call(self):
        tid = "call-edit-1"
        notes, _b = _fwd([{
            "sessionUpdate": "tool_call",
            "toolCallId": tid,
            "title": "Edit `/x.nim`",
            "kind": "edit",
            "status": "completed",
            "content": [{
                "type": "diff",
                "path": "/x.nim",
                "oldText": "a = 1\n",
                "newText": "a = 2\n",
            }],
        }])
        uses = [p for _, p in notes if p.get("type") == "tool_use"]
        self.assertEqual(len(uses), 1, uses)
        inp = uses[0].get("input") or {}
        self.assertEqual(inp.get("file_path"), "/x.nim")
        self.assertTrue(inp.get("unified_diff") or inp.get("old_string"))
        if inp.get("unified_diff"):
            self.assertIn("-a = 1", inp["unified_diff"])
            self.assertIn("+a = 2", inp["unified_diff"])


if __name__ == "__main__":
    unittest.main()
