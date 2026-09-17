"""Follow-up prompt after Esc must not be postponed/dropped."""
import os
import sys
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from grok_main import GrokBridge  # noqa: E402
from turn_state import TurnState  # noqa: E402


class _Fut:
    def __init__(self, done=False):
        self._done = done

    def done(self):
        return self._done


class TestPrecancelBeforePrompt(unittest.TestCase):
    def test_grok_skips_stale_orphan_cancel(self):
        b = GrokBridge()
        b._cancel_in_flight = True
        b._prompt_fut = None
        b._query_req_id = None
        b._orphan_turn_notified = False
        self.assertFalse(b._should_precancel_before_prompt())

    def test_grok_cancels_live_prompt(self):
        b = GrokBridge()
        b._cancel_in_flight = True
        b._prompt_fut = _Fut(done=False)
        b._query_req_id = None
        self.assertTrue(b._should_precancel_before_prompt())

    def test_grok_cancels_orphan_leftover_turn(self):
        b = GrokBridge()
        b._cancel_in_flight = True
        b._prompt_fut = None
        b._query_req_id = None
        b._orphan_turn_notified = True
        self.assertTrue(b._should_precancel_before_prompt())

    def test_kimi_still_settles_after_forced_local(self):
        b = GrokBridge()
        b.BACKEND_NAME = "kimi"
        b._cancel_in_flight = True
        b._prompt_fut = None
        b._query_req_id = None
        b._orphan_turn_notified = False
        self.assertTrue(b._should_precancel_before_prompt())

    def test_no_flag_no_cancel(self):
        b = GrokBridge()
        b._cancel_in_flight = False
        b._prompt_fut = _Fut(done=False)
        self.assertFalse(b._should_precancel_before_prompt())

    def test_cancel_sets_drop_grok_leftover(self):
        path = os.path.join(_BRIDGE, "acp_base.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("self._drop_grok_leftover = True", src)
        self.assertIn("self._drop_grok_leftover = False", src)
        start = src.find("async def _send_prompt")
        end = src.find("\n    def ", start + 10)
        self.assertIn("_drop_grok_leftover = False", src[start:end])
        start = src.find("async def handle_interrupt")
        end = src.find("\n    def _unblock_interaction_waiters")
        body = src[start:end]
        self.assertIn("grok_leftover", body)
        self.assertIn("_orphan_turn_notified", body)

    def test_handle_query_uses_helper(self):
        path = os.path.join(_BRIDGE, "acp_base.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        start = src.find("async def handle_query")
        end = src.find("\n    def ", start + 10)
        body = src[start:end]
        self.assertIn("_should_precancel_before_prompt", body)
        self.assertIn("skip stale orphan session/cancel", body)
        # Must not always cancel on the leftover flag.
        self.assertNotIn(
            "if self._cancel_in_flight:\n"
            "            await self._cancel_agent_turn(\n"
            "                reason=\"post_interrupt\"",
            body,
        )


class TestUnstickStaleInterrupt(unittest.TestCase):
    def test_query_clears_user_cancelled_turn(self):
        path = os.path.join(_ROOT, "session.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("self._user_cancelled_turn = False", src)
        self.assertIn("self._user_cancelled_turn = True", src)
        self.assertIn("_user_cancelled_turn", src)

    def test_method_exists_and_is_used(self):
        path = os.path.join(_ROOT, "session.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("def _unstick_stale_interrupt", src)
        self.assertIn("self._unstick_stale_interrupt()", src)
        self.assertIn("_clear_interrupting", src)

    def test_unstick_settles_and_flushes_queue(self):
        # Bind the real method onto a stub — no Sublime Session import.
        path = os.path.join(_ROOT, "session.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        start = src.find("    def _unstick_stale_interrupt")
        self.assertGreater(start, 0)
        end = src.find("\n    def ", start + 10)
        body = "def _unstick_stale_interrupt(self)" + src[start:end].split(
            "def _unstick_stale_interrupt(self)", 1)[1]
        ns = {}
        exec(body, ns)
        fn = ns["_unstick_stale_interrupt"]

        class S:
            def __init__(self):
                self._interrupting = True
                self.working = True
                self._turn = TurnState()
                self._turn.begin_query()
                self._turn.begin_interrupt()
                self._interrupt_stream = True
                self.output = types.SimpleNamespace(
                    current=types.SimpleNamespace(working=True))
                self._queued_prompts = ["after esc"]
                self.phase = None
                self.idle_reason = None

            def _set_turn_phase(self, p):
                self.phase = p

            def _ensure_idle_input(self, reason=""):
                self.idle_reason = reason
                if self._queued_prompts:
                    self._queued_prompts.pop(0)

        s = S()
        S._unstick_stale_interrupt = fn
        started = s._unstick_stale_interrupt()
        self.assertFalse(s._interrupting)
        self.assertFalse(s.working)
        self.assertEqual(s._turn.kind, "idle")
        self.assertEqual(s.phase, "idle")
        self.assertEqual(s.idle_reason, "interrupt flag clear")
        self.assertEqual(s._queued_prompts, [])
        self.assertTrue(started)

    def test_unstick_does_not_kill_live_followup(self):
        path = os.path.join(_ROOT, "session.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        start = src.find("    def _unstick_stale_interrupt")
        end = src.find("\n    def ", start + 10)
        body = "def _unstick_stale_interrupt(self)" + src[start:end].split(
            "def _unstick_stale_interrupt(self)", 1)[1]
        ns = {}
        exec(body, ns)
        fn = ns["_unstick_stale_interrupt"]

        class S:
            def __init__(self):
                self._interrupting = True
                self.working = True
                self._turn = TurnState()
                self._turn.begin_query()  # live follow-up already started
                self._interrupt_stream = False
                self.output = None
                self._queued_prompts = []
                self.idle_reason = None

            def _set_turn_phase(self, p):
                pass

            def _ensure_idle_input(self, reason=""):
                self.idle_reason = reason

        s = S()
        S._unstick_stale_interrupt = fn
        self.assertFalse(s._unstick_stale_interrupt())
        self.assertTrue(s.working)
        self.assertEqual(s._turn.kind, "live")
        self.assertIsNone(s.idle_reason)


if __name__ == "__main__":
    unittest.main()
