"""Page Edit/Write rows from a child session transcript."""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from session_edits import collect_session_edits, page_edits  # noqa: E402


class _TC:
    def __init__(self, name, inp, status="done", id=None):
        self.name = name
        self.tool_input = inp
        self.status = status
        self.id = id


class _Conv:
    def __init__(self, events):
        self.events = events


class TestSessionEdits(unittest.TestCase):
    def _edits(self):
        return collect_session_edits([
            _Conv([
                "prose",
                _TC("Read", {"file_path": "/x.nim"}),
                _TC("Edit", {
                    "file_path": "/x.nim",
                    "unified_diff": "@@ -21,2 +21,2 @@\n-  1\n+  2\n",
                }, id="e1"),
                _TC("Edit", {"file_path": "/y.nim", "old_string": "a",
                             "new_string": "b"}, status="pending"),
            ]),
            _Conv([
                _TC("Write", {"path": "/z.nim", "content": "hello\n"}, id="w1"),
                _TC("Edit", {
                    "file_path": "/pkg/y.nim",
                    "unified_diff": "@@ -3,1 +3,1 @@\n-old\n+new\n",
                }, id="e2"),
            ]),
        ])

    def test_collects_done_edits_in_order(self):
        rows = self._edits()
        self.assertEqual([r["id"] for r in rows], ["e1", "w1", "e2"])
        self.assertEqual(rows[0]["line"], 21)
        self.assertEqual(rows[0]["file_path"], "/x.nim")
        self.assertIn("+  2", rows[0]["diff"])
        self.assertEqual(rows[1]["tool"], "Write")

    def test_skips_pending(self):
        self.assertFalse(any(r["id"] == "e-pending" for r in self._edits()))

    def test_page_offset_limit(self):
        rows = self._edits()
        p = page_edits(rows, offset=1, limit=1)
        self.assertEqual(p["total"], 3)
        self.assertEqual(p["count"], 1)
        self.assertTrue(p["has_more"])
        self.assertEqual(p["edits"][0]["id"], "w1")
        p2 = page_edits(rows, offset=2, limit=10)
        self.assertFalse(p2["has_more"])
        self.assertEqual(p2["count"], 1)

    def test_file_path_filter(self):
        rows = self._edits()
        p = page_edits(rows, file_path="y.nim")
        self.assertEqual(p["total"], 1)
        self.assertEqual(p["edits"][0]["id"], "e2")

    def test_limit_clamped(self):
        rows = self._edits()
        p = page_edits(rows, offset=0, limit=999)
        self.assertEqual(p["limit"], 40)
        p0 = page_edits(rows, offset=0, limit=0)
        self.assertEqual(p0["limit"], 10)


if __name__ == "__main__":
    unittest.main()
