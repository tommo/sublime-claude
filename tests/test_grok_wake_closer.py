"""Grok self-wake closer is turn_completed, not host prompt_complete."""
import asyncio
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

import acp_base  # noqa: E402
from acp_base import AcpBridge  # noqa: E402


class _Stub(AcpBridge):
    def __init__(self):
        self.BACKEND_NAME = "grok"
        self.session_id = "sess-1"
        self._prompt_fut = None
        self._host_prompt_id = None
        self._orphan_turn_notified = False
        self._logs = []
        self.notes = []

    def file_log(self, msg):
        self._logs.append(msg)


class TestGrokWakeCloser(unittest.TestCase):
    def setUp(self):
        self.notes = []
        acp_base.send_notification = (
            lambda method, params, n=self.notes: n.append((method, params)))

    def _leftovers(self):
        return [
            p for m, p in self.notes
            if m == "message" and p.get("leftover_end")
        ]

    def test_host_prompt_complete_while_rpc_live_is_ignored(self):
        b = _Stub()
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        b._prompt_fut = fut
        b._handle_grok_turn_end({
            "promptId": "host-aaa", "stopReason": "end_turn"})
        self.assertEqual(b._host_prompt_id, "host-aaa")
        self.assertEqual(self._leftovers(), [])
        fut.cancel()
        loop.close()

    def test_host_turn_completed_after_rpc_is_ignored(self):
        b = _Stub()
        b._host_prompt_id = "host-aaa"
        b._handle_grok_turn_end(
            {"sessionId": "sess-1"},
            {"sessionUpdate": "turn_completed",
             "prompt_id": "host-aaa", "stop_reason": "end_turn"})
        self.assertEqual(self._leftovers(), [])

    def test_synthetic_turn_completed_emits_leftover_end(self):
        b = _Stub()
        b._host_prompt_id = "host-aaa"
        b._handle_grok_turn_end(
            {"sessionId": "sess-1"},
            {"sessionUpdate": "turn_completed",
             "prompt_id": "task-completed-term_abc",
             "stop_reason": "end_turn"})
        ends = self._leftovers()
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0].get("stop_reason"), "end_turn")
        self.assertTrue(ends[0].get("leftover_end"))

    def test_xai_session_update_routes_turn_completed(self):
        b = _Stub()
        b._host_prompt_id = "host-aaa"
        b._is_foreign_session = lambda p: False  # noqa: E731

        async def _go():
            # Simulate the reader branch: not a full stdin loop.
            params = {
                "sessionId": "sess-1",
                "update": {
                    "sessionUpdate": "turn_completed",
                    "prompt_id": "task-completed-term_x",
                    "stop_reason": "end_turn",
                },
            }
            upd = params["update"]
            b._handle_grok_turn_end(params, upd)

        asyncio.run(_go())
        self.assertEqual(len(self._leftovers()), 1)


if __name__ == "__main__":
    unittest.main()
