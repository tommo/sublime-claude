"""Unit tests for sticky ◎ vs history selection geometry.

Drives the real shipped helpers in composer_geometry.py (not reimplemented).
"""
import os
import sys
import unittest

# tests/ is under package root
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from composer_geometry import (  # noqa: E402
    classify_regions,
    wholly_in_draft,
    wholly_in_history,
    crosses_draft_boundary,
    mutation_allowed_in_draft,
    clamp_region_to_draft,
    history_select_range,
    draft_select_range,
)


class TestComposerGeometry(unittest.TestCase):
    def test_all_carets_in_draft(self):
        # draft starts at 100, eof 150
        regs = [(100, 100), (120, 130), (149, 150)]
        self.assertEqual(classify_regions(regs, 100, 150), "draft")
        self.assertTrue(wholly_in_draft(regs, 100, 150))
        self.assertTrue(mutation_allowed_in_draft(regs, 100, 150))
        self.assertFalse(wholly_in_history(regs, 100, 150))
        self.assertFalse(crosses_draft_boundary(regs, 100, 150))

    def test_caret_in_history(self):
        regs = [(0, 0), (50, 60)]
        self.assertEqual(classify_regions(regs, 100, 150), "history")
        self.assertTrue(wholly_in_history(regs, 100, 150))
        self.assertFalse(mutation_allowed_in_draft(regs, 100, 150))

    def test_caret_exactly_at_input_start_is_draft(self):
        regs = [(100, 100)]
        self.assertEqual(classify_regions(regs, 100, 200), "draft")

    def test_selection_spanning_boundary(self):
        regs = [(90, 110)]
        self.assertEqual(classify_regions(regs, 100, 200), "crossing")
        self.assertTrue(crosses_draft_boundary(regs, 100, 200))
        self.assertFalse(mutation_allowed_in_draft(regs, 100, 200))

    def test_multi_caret_one_outside(self):
        regs = [(50, 50), (120, 120)]  # history + draft
        self.assertEqual(classify_regions(regs, 100, 200), "crossing")
        self.assertFalse(mutation_allowed_in_draft(regs, 100, 200))

    def test_reversed_region_normalized(self):
        # ST can report end < begin
        regs = [(130, 110)]
        self.assertEqual(classify_regions(regs, 100, 200), "draft")

    def test_empty_regions(self):
        self.assertEqual(classify_regions([], 100, 200), "none")
        self.assertFalse(mutation_allowed_in_draft([], 100, 200))

    def test_clamp_region_to_draft(self):
        self.assertEqual(clamp_region_to_draft(0, 50, 100, 200), (100, 100))
        self.assertEqual(clamp_region_to_draft(90, 150, 100, 200), (100, 150))
        self.assertEqual(clamp_region_to_draft(150, 250, 100, 200), (150, 200))

    def test_select_ranges(self):
        self.assertEqual(history_select_range(100, 200), (0, 100))
        self.assertEqual(draft_select_range(100, 200), (100, 200))


if __name__ == "__main__":
    unittest.main()
