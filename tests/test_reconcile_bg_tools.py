"""Dead ⚙ must clear when bridge running=[] even if never seen live."""
import os
import textwrap
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SESS = os.path.join(_ROOT, "session.py")


def _extract_method(path, name):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    start = src.find(f"    def {name}")
    if start < 0:
        raise AssertionError(f"{name} missing in {path}")
    nxt = src.find("\n    def ", start + 10)
    return textwrap.dedent(src[start:nxt])


class _Tool:
    def __init__(self):
        self.status = "background"


class _Sess:
    def __init__(self):
        self._bg_tools = {"8:tool_GwLP": _Tool()}
        self._bg_task_ids = {"8:tool_GwLP"}
        self._task_tool_map = {"bash-0eaakzzi": "8:tool_GwLP"}
        self._seen_running = set()
        self._bg_notified_task_ids = set()
        self._bg_notified_tool_ids = set()
        self._pending_bg_task_ids = set()
        self._pending_bg_tool_ids = set()
        self.working = False
        self.backend = "kimi"
        self.finalized = []
        self.woke = []

    def _bg_notify_already(self, task_id="", tool_use_id=""):
        return False

    def _finalize_bg_tool(self, tool_use_id, keep):
        self.finalized.append((tool_use_id, keep))
        t = self._bg_tools.get(tool_use_id)
        if t is not None:
            t.status = "done"

    def _on_sys_task_notification(self, data):
        self.woke.append(data)


class TestReconcileUnseenDeadGear(unittest.TestCase):
    def setUp(self):
        src = _extract_method(_SESS, "_reconcile_bg_tools")
        src = src.replace("from .output import BACKGROUND",
                          "BACKGROUND = 'background'")
        ns = {"_SELF_WAKE_BACKENDS": {"grok"}}
        exec(src, ns)
        self.fn = ns["_reconcile_bg_tools"]

    def test_kimi_unseen_completed_finalizes_without_wake(self):
        s = _Sess()
        self.fn(s, running=[])
        self.assertIn(("8:tool_GwLP", True), s.finalized)
        self.assertEqual(s.woke, [])
        self.assertNotIn("8:tool_GwLP", s._bg_task_ids)
        self.assertNotIn("bash-0eaakzzi", s._task_tool_map)

    def test_still_running_keeps_gear(self):
        s = _Sess()
        self.fn(s, running=["bash-0eaakzzi"])
        self.assertEqual(s.finalized, [])
        self.assertEqual(s.woke, [])
        self.assertIn("8:tool_GwLP", s._bg_task_ids)

    def test_grok_idle_vanished_wakes(self):
        s = _Sess()
        s.backend = "grok"
        s._seen_running.add("bash-0eaakzzi")
        self.fn(s, running=[])
        self.assertEqual(len(s.woke), 1)
        self.assertEqual(s.woke[0]["tool_use_id"], "8:tool_GwLP")

    def test_on_done_polls_bg(self):
        with open(_SESS, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("self._bg_poll()", body)
        self.assertIn("bg poll on turn end", body)

    def test_task_updated_uses_payload_tool_id(self):
        src = _extract_method(_SESS, "_on_sys_task_updated")
        self.assertIn('data.get("tool_use_id")', src)


if __name__ == "__main__":
    unittest.main()
