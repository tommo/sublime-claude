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

    def test_synthetic_pid_is_detected(self):
        self.assertTrue(
            AcpBridge._is_synthetic_grok_prompt_id(
                "task-completed-term_471c5828f8"))
        self.assertFalse(
            AcpBridge._is_synthetic_grok_prompt_id(
                "682a1aec-042f-4506-900c-8e5d2656ea1b"))

    def test_synthetic_turn_completed_while_host_rpc_still_emits(self):
        """Don't swallow task-completed-* just because _prompt_fut is live."""
        b = _Stub()
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        b._prompt_fut = fut
        b._host_prompt_id = "host-aaa"
        b._handle_grok_turn_end(
            {"sessionId": "sess-1"},
            {"sessionUpdate": "turn_completed",
             "prompt_id": "task-completed-term_471c5828f8",
             "stop_reason": "end_turn"})
        self.assertEqual(len(self._leftovers()), 1)
        self.assertEqual(b._host_prompt_id, "host-aaa")
        fut.cancel()
        loop.close()


class TestSelfWakeHost(unittest.TestCase):
    def test_pending_leftover_end_skips_resume(self):
        path = os.path.join(_ROOT, "session.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        start = src.find("    def _resume_interrupt_stream")
        end = src.find("\n    def ", start + 10)
        body = "def _resume_interrupt_stream(self)" + src[start:end].split(
            "def _resume_interrupt_stream(self)", 1)[1]
        ns = {}
        exec(body, ns)
        fn = ns["_resume_interrupt_stream"]

        class S:
            def __init__(self):
                self.working = False
                self._pending_leftover_end = True
                self._awaiting_query_rpc = False
                self.backend = "grok"
                self.output = None
                self.armed = False
                self.resumed = False

            def _arm_self_wake_idle(self):
                self.armed = True

            def _set_turn_phase(self, p):
                pass

            def _animate(self):
                pass

            class _turn:
                @staticmethod
                def resume_stream():
                    raise AssertionError("must not resume after leftover_end")

        s = S()
        S._resume_interrupt_stream = fn
        s._resume_interrupt_stream()
        self.assertFalse(s.working)
        self.assertFalse(s._pending_leftover_end)
        self.assertFalse(s.armed)

    def test_result_stores_pending_when_idle(self):
        path = os.path.join(_ROOT, "session.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("self._pending_leftover_end = True", src)
        self.assertIn("def _arm_self_wake_idle", src)
        self.assertIn("def _maybe_idle_self_wake", src)
        self.assertIn("skip self-wake; leftover_end already arrived", src)


if __name__ == "__main__":
    unittest.main()
