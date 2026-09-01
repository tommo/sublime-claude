"""Kimi bg mixin: match bash-*.json by command overlap, never newest-unmatched."""
import json
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from kimi_bg import KimiBgMixin  # noqa: E402


class _M(KimiBgMixin):
    def __init__(self, tdir):
        self._tdir = tdir
        self._kimi_bg = {}
        self._logs = []

    def kimi_tasks_dir(self):
        return self._tdir

    def file_log(self, msg):
        self._logs.append(msg)


def _write_task(tdir, tid, command, status="running", started=1):
    path = os.path.join(tdir, f"{tid}.json")
    with open(path, "w") as f:
        json.dump({
            "taskId": tid,
            "status": status,
            "command": command,
            "startedAt": started,
        }, f)


class TestFindMatchingKimiTask(unittest.TestCase):
    def test_matches_command_overlap(self):
        with tempfile.TemporaryDirectory() as tdir:
            _write_task(tdir, "bash-aaa", "cd /proj && pil test -t tree_editor")
            _write_task(tdir, "bash-bbb", "sleep 999", started=9)
            m = _M(tdir)
            hit = m.find_matching_kimi_task(
                "cd /proj && pil test -t tree_editor 2>&1 | head")
            self.assertIsNotNone(hit)
            self.assertEqual(hit["taskId"], "bash-aaa")

    def test_no_unrelated_newest(self):
        with tempfile.TemporaryDirectory() as tdir:
            _write_task(tdir, "bash-zzz", "completely different cmd", started=99)
            m = _M(tdir)
            self.assertIsNone(m.find_matching_kimi_task("pil test -t foo"))

    def test_parse_result_text(self):
        text = (
            "task_id: bash-abc\n"
            "description: hi\n"
            "status: running\n"
            "automatic_notification: true\n"
        )
        p = KimiBgMixin.parse_kimi_bg_result_text(text)
        self.assertEqual(p["task_id"], "bash-abc")
        self.assertTrue(p["auto"])

    def test_parse_live_pid_zero_result(self):
        # Live ACP AcpTerminalProcess.pid is always 0.
        text = (
            "task_id: bash-6rusyl45\n"
            "pid: 0\n"
            "description: grok research: engine baking-group precedents\n"
            "status: running\n"
            "automatic_notification: true\n"
            "next_step: The completion arrives automatically\n"
        )
        p = KimiBgMixin.parse_kimi_bg_result_text(text)
        self.assertEqual(p["task_id"], "bash-6rusyl45")
        self.assertEqual(p["status"], "running")
        self.assertTrue(p["auto"])

    def test_run_in_background_gate(self):
        from acp_base import AcpBridge
        self.assertTrue(AcpBridge._looks_like_background_tool(
            {"title": "Bash"},
            {"command": "grok -p x", "run_in_background": True,
             "timeout": 2400},
        ))
        self.assertFalse(AcpBridge._looks_like_background_tool(
            {"title": "Running: grok -p x"},
            {"command": "grok -p x"},
        ))

    def test_does_not_reuse_already_linked_task(self):
        with tempfile.TemporaryDirectory() as tdir:
            cmd = "cd /gfx && pil test 2>&1 | tail -40"
            _write_task(tdir, "bash-one", cmd, started=1)
            m = _M(tdir)
            m._terminal_bg = {
                "term_a": {"task_id": "bash-one", "tool_use_id": "tool-a"},
            }
            self.assertIsNone(m.find_matching_kimi_task(cmd))


class TestSkipBgNotify(unittest.TestCase):
    def test_sibling_tool_id_is_not_skipped(self):
        from acp_base import AcpBridge

        class _B(AcpBridge):
            def __init__(self):
                self._bg_notified_tasks = {"bash-one"}
                self._bg_notified_tools = {"tool-a"}
                self._terminal_bg = {}

        b = _B()
        self.assertTrue(b._should_skip_bg_notify("bash-one", "tool-a"))
        self.assertFalse(b._should_skip_bg_notify("bash-one", "tool-b"))
        self.assertTrue(b._should_skip_bg_notify("bash-one", ""))

    def test_native_complete_skip_still_ui_closes(self):
        from acp_base import AcpBridge

        class _B(KimiBgMixin, AcpBridge):
            def __init__(self):
                self._kimi_bg = {
                    "bash-0eaakzzi": {"tool_use_id": "8:tool_GwLP", "description": "x"},
                }
                self._terminal_bg = {
                    "term_x": {"task_id": "bash-0eaakzzi", "tool_use_id": "8:tool_GwLP"},
                }
                self._bg_notified_tasks = {"bash-0eaakzzi"}
                self._bg_notified_tools = {"8:tool_GwLP"}
                self._sys = []
                self._logs = []

            def file_log(self, msg):
                self._logs.append(msg)

            def _emit_system(self, subtype, data):
                self._sys.append((subtype, data))

            def read_kimi_task_meta(self, task_id):
                return {"status": "completed", "exitCode": 0}

        b = _B()
        b._emit_kimi_native_bg_complete("bash-0eaakzzi")
        self.assertEqual(b._kimi_bg, {})
        self.assertEqual(b._terminal_bg, {})
        self.assertTrue(any(s == "task_updated" for s, _ in b._sys))
        self.assertFalse(any(s == "task_notification" for s, _ in b._sys))


class TestTakePendingSkipsBound(unittest.TestCase):
    def test_does_not_rebind_live_bg_tool(self):
        from acp_base import AcpBridge

        class _B(AcpBridge):
            def __init__(self):
                self._pending_execute_ids = ["8:tool_GwLP"]
                self._last_execute_id = "8:tool_GwLP"
                self._calls = {}
                self._terminal_bg = {
                    "term_f9": {"task_id": "bash-0eaakzzi",
                                "tool_use_id": "8:tool_GwLP"},
                }

            def _call(self, tid):
                return None

        b = _B()
        self.assertIsNone(b._take_pending_execute_id())
        self.assertEqual(b._pending_execute_ids, [])


if __name__ == "__main__":
    unittest.main()
