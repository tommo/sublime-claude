"""acp-term-* and bash-* for the same tool_use must notify once.

Mirrors Session._alias_bg_task_ids / _bg_notify_already / _mark_bg_notify_ids
(session.py cannot import here — relative package).
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _alias(task_tool_map, tool_use_id, task_id=""):
    ids = set()
    if task_id:
        ids.add(str(task_id))
    if tool_use_id:
        for tid, tuid in (task_tool_map or {}).items():
            if tuid == tool_use_id:
                ids.add(str(tid))
    return ids


def _already(s, task_id="", tool_use_id=""):
    if tool_use_id and tool_use_id in s["_bg_notified_tool_ids"]:
        return True
    if tool_use_id and tool_use_id in s["_pending_bg_tool_ids"]:
        return True
    for tid in _alias(s["_task_tool_map"], tool_use_id, task_id):
        if tid in s["_bg_notified_task_ids"] or tid in s["_pending_bg_task_ids"]:
            return True
    return False


def _mark(s, task_id="", tool_use_id=""):
    if tool_use_id:
        s["_bg_notified_tool_ids"].add(tool_use_id)
        s["_pending_bg_tool_ids"].discard(tool_use_id)
    for tid in _alias(s["_task_tool_map"], tool_use_id, task_id):
        s["_bg_notified_task_ids"].add(tid)
        s["_pending_bg_task_ids"].discard(tid)


class TestBgNotifyDedupe(unittest.TestCase):
    def setUp(self):
        self.s = {
            "_task_tool_map": {
                "acp-term-abc": "tool-1",
                "bash-xyz": "tool-1",
                "bash-other": "tool-2",
            },
            "_bg_notified_task_ids": set(),
            "_bg_notified_tool_ids": set(),
            "_pending_bg_task_ids": set(),
            "_pending_bg_tool_ids": set(),
        }

    def test_session_py_has_alias_helpers(self):
        with open(os.path.join(_ROOT, "session.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("def _alias_bg_task_ids", src)
        self.assertIn("def _bg_notify_already", src)
        self.assertIn("def _mark_bg_notify_ids", src)

    def test_aliases_include_both_ids(self):
        ids = _alias(self.s["_task_tool_map"], "tool-1", "acp-term-abc")
        self.assertEqual(ids, {"acp-term-abc", "bash-xyz"})

    def test_second_id_for_same_tool_is_already(self):
        self.assertFalse(_already(self.s, "acp-term-abc", "tool-1"))
        _mark(self.s, "acp-term-abc", "tool-1")
        self.assertTrue(_already(self.s, "bash-xyz", "tool-1"))
        self.assertTrue(_already(self.s, "bash-xyz", ""))
        self.assertFalse(_already(self.s, "bash-other", "tool-2"))


if __name__ == "__main__":
    unittest.main()
