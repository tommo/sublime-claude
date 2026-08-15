"""One parent notification per child completion — not stock + custom."""
import os
import sys
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if "sublime" not in sys.modules:
    sys.modules["sublime"] = types.SimpleNamespace(
        _claude_sessions={},
        _claude_agents={},
        _claude_subsession_waits={},
    )

from session_registry import (  # noqa: E402
    is_stock_subsession_wake,
    merge_subsession_queue,
    subsession_notify_key,
    child_parent_already_notified,
    mark_child_parent_notified,
    fire_subsession_waits,
    parent_notify_should_inject,
)


STOCK = (
    "✅ Subsession agent-ae635c6b3cb2 completed "
    "(agent_id=agent-ae635c6b3cb2, view_id=294)"
)
CUSTOM = (
    "Codex design consultant (skin-editor-design / "
    "agent-ae635c6b3cb2) finished. Memo is on disk."
)


class TestMergeSubsessionQueue(unittest.TestCase):
    def test_same_key(self):
        self.assertEqual(
            subsession_notify_key(STOCK),
            subsession_notify_key(CUSTOM),
        )
        self.assertTrue(is_stock_subsession_wake(STOCK))
        self.assertFalse(is_stock_subsession_wake(CUSTOM))

    def test_stock_then_custom_keeps_custom(self):
        q = merge_subsession_queue([STOCK], CUSTOM)
        self.assertEqual(q, [CUSTOM])

    def test_custom_then_stock_keeps_custom(self):
        q = merge_subsession_queue([CUSTOM], STOCK)
        self.assertEqual(q, [CUSTOM])

    def test_other_prompts_untouched(self):
        q = merge_subsession_queue(["hello"], STOCK)
        self.assertEqual(q, ["hello", STOCK])

    def test_unrelated_child_not_merged(self):
        other = "✅ Subsession agent-ffffffffffff completed"
        q = merge_subsession_queue([STOCK], other)
        self.assertEqual(q, [STOCK, other])


class _Child:
    def __init__(self):
        self.agent_id = "agent-ae635c6b3cb2"
        self.subsession_id = "agent-ae635c6b3cb2"
        self._parent_notified = False


class TestAlreadyNotified(unittest.TestCase):
    def test_inject_xor_waiter_same_completion_only(self):
        """Waiter delivered this complete → do not also inject/queue."""
        self.assertTrue(parent_notify_should_inject(0))
        self.assertFalse(parent_notify_should_inject(1))

    def test_next_complete_can_inject_again(self):
        c = _Child()
        mark_child_parent_notified(c)
        # Lifetime flag must not block a later round.
        self.assertTrue(parent_notify_should_inject(0, c))
        self.assertEqual(fire_subsession_waits(c, "done"), 0)


if __name__ == "__main__":
    unittest.main()
