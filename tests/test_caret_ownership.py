"""Unit tests for draft vs history caret ownership policy.

Pure helpers in composer_geometry — no Sublime runtime.
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from composer_geometry import (  # noqa: E402
    OWNER_DRAFT,
    OWNER_HISTORY,
    owner_from_geometry,
    stream_may_move_caret,
    stream_may_force_bottom,
    stream_treat_as_composing,
    stream_tick_actions,
)


class TestCaretOwnership(unittest.TestCase):
    def test_owner_from_geometry(self):
        self.assertEqual(owner_from_geometry("draft"), OWNER_DRAFT)
        self.assertEqual(owner_from_geometry("history"), OWNER_HISTORY)
        self.assertEqual(owner_from_geometry("crossing"), OWNER_HISTORY)
        self.assertEqual(owner_from_geometry("none"), OWNER_DRAFT)

    def test_stream_may_move_caret(self):
        self.assertTrue(stream_may_move_caret(OWNER_DRAFT))
        self.assertFalse(stream_may_move_caret(OWNER_HISTORY))

    def test_stream_may_force_bottom(self):
        self.assertTrue(stream_may_force_bottom(OWNER_DRAFT, True))
        self.assertFalse(stream_may_force_bottom(OWNER_DRAFT, False))
        self.assertFalse(stream_may_force_bottom(OWNER_HISTORY, True))
        self.assertFalse(stream_may_force_bottom(OWNER_HISTORY, False))

    def test_stream_treat_as_composing_live_wins(self):
        self.assertTrue(stream_treat_as_composing(
            True, False, OWNER_HISTORY, True))
        self.assertFalse(stream_treat_as_composing(
            False, False, OWNER_HISTORY, True))

    def test_empty_sel_only_if_draft_owner(self):
        self.assertTrue(stream_treat_as_composing(
            False, True, OWNER_DRAFT, True))
        self.assertFalse(stream_treat_as_composing(
            False, True, OWNER_HISTORY, True))
        self.assertFalse(stream_treat_as_composing(
            False, True, OWNER_DRAFT, False))

    def test_stream_tick_history_no_scroll_no_caret(self):
        for following in (True, False):
            force, reapply = stream_tick_actions(
                OWNER_HISTORY, following,
                live_sel_in_draft=False, sel_empty=False,
                has_saved_draft_off=True)
            self.assertFalse(force)
            self.assertFalse(reapply)

    def test_stream_tick_draft_following(self):
        force, reapply = stream_tick_actions(
            OWNER_DRAFT, True,
            live_sel_in_draft=True, sel_empty=False,
            has_saved_draft_off=True)
        self.assertTrue(force)
        self.assertTrue(reapply)

    def test_stream_tick_draft_not_following(self):
        force, reapply = stream_tick_actions(
            OWNER_DRAFT, False,
            live_sel_in_draft=True, sel_empty=False,
            has_saved_draft_off=True)
        self.assertFalse(force)
        self.assertTrue(reapply)

    def test_stream_tick_history_but_live_draft(self):
        # Live geometry wins for composing; following drives scroll
        force, reapply = stream_tick_actions(
            OWNER_HISTORY, True,
            live_sel_in_draft=True, sel_empty=False,
            has_saved_draft_off=True)
        self.assertTrue(force)
        self.assertTrue(reapply)

    def test_stream_tick_empty_sel_stale_offset_history(self):
        force, reapply = stream_tick_actions(
            OWNER_HISTORY, True,
            live_sel_in_draft=False, sel_empty=True,
            has_saved_draft_off=True)
        self.assertFalse(force)
        self.assertFalse(reapply)


if __name__ == "__main__":
    unittest.main()
