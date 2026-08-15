"""send_to_session stamps [from agent …] so the receiver can tell user ◎ from mail."""
from __future__ import annotations

import os
import sys
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class TestStampSenderPrompt(unittest.TestCase):
    def setUp(self):
        sys.modules.setdefault("sublime", types.SimpleNamespace())
        import session_registry as sr
        self.sr = sr

    def test_agent_header_has_ids(self):
        out = self.sr.stamp_sender_prompt(
            "please review the diff",
            sender_agent_id="agent-abc123",
            sender_session_id="session_deadbeef",
            sender_name="fixer",
        )
        self.assertTrue(out.startswith("[from agent agent-abc123]"))
        self.assertIn("session_id=session_deadbeef", out)
        self.assertIn("name=fixer", out)
        self.assertIn("please review the diff", out.split("\n", 1)[1])

    def test_user_header(self):
        out = self.sr.stamp_sender_prompt("hi", from_user=True)
        self.assertEqual(out, "[from user]\nhi")

    def test_unstamped_user_style_is_not_this_helper(self):
        # ◎ query() sends raw text; only the communication tool stamps.
        self.assertFalse(self.sr._SENDER_LINE.match("please review the diff"))

    def test_idempotent(self):
        once = self.sr.stamp_sender_prompt("x", sender_agent_id="agent-1")
        twice = self.sr.stamp_sender_prompt(once, sender_agent_id="agent-2")
        self.assertEqual(once, twice)
        self.assertEqual(once.count("[from agent"), 1)

    def test_display_label(self):
        stamped = self.sr.stamp_sender_prompt(
            "long body\nmore", sender_agent_id="agent-abc123")
        self.assertEqual(
            self.sr.sender_display_prompt(stamped), "📬 from agent-abc123")
        self.assertEqual(
            self.sr.sender_display_prompt("[from user]\nhi"), "📬 from user")


if __name__ == "__main__":
    unittest.main()
