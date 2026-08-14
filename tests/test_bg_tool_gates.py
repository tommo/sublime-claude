"""Background tool gates: TaskOutput is never ⚙ Read (background)."""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from acp_base import AcpBridge  # noqa: E402


class _NameOnly(AcpBridge):
    """Skip BaseBridge init — only need normalize/map helpers."""

    def __init__(self):
        self.TOOL_TO_CANONICAL = dict(AcpBridge.TOOL_TO_CANONICAL)


class TestNormalizeTaskOutputTitle(unittest.TestCase):
    def setUp(self):
        self.b = _NameOnly()

    def test_reading_output_of_task_is_taskget_not_read(self):
        name = self.b._normalize_tool_name({
            "title": "Reading output of task bash-wnvdr6k2",
            "kind": "read",
        })
        self.assertEqual(name, "TaskGet")

    def test_plain_reading_file_is_read(self):
        name = self.b._normalize_tool_name({
            "title": "Reading `/tmp/foo.py`",
            "kind": "read",
        })
        self.assertEqual(name, "Read")

    def test_looks_like_bg_rejects_task_output_title(self):
        self.assertFalse(AcpBridge._looks_like_background_tool({
            "title": "Reading output of task bash-xxx",
        }))
        self.assertFalse(AcpBridge._looks_like_background_tool({
            "title": "TaskOutput: bash-xxx",
            "rawInput": {"task_id": "bash-xxx"},
        }, {"task_id": "bash-xxx"}))

    def test_looks_like_bg_accepts_starting_background(self):
        self.assertTrue(AcpBridge._looks_like_background_tool({
            "title": "Starting background: sleep 999",
        }))

    def test_shell_name_gate(self):
        self.assertTrue(AcpBridge._is_shell_tool_name("Bash"))
        self.assertFalse(AcpBridge._is_shell_tool_name("Read"))
        self.assertFalse(AcpBridge._is_shell_tool_name("TaskGet"))


class TestOutputViewBgDemote(unittest.TestCase):
    def test_non_shell_cannot_be_background(self):
        # Same gate as output_view.tool (no Sublime import)
        class TC:
            def __init__(self, name, status):
                self.name = name
                self.status = status

        PENDING, BACKGROUND = "pending", "background"

        def apply(name, background, existing=None):
            _SHELL_BG = (
                "Bash", "Shell", "execute", "run_terminal_command", "Workflow",
            )
            if background and name not in _SHELL_BG:
                background = False
            if existing is not None and existing.status in (PENDING, BACKGROUND):
                existing.name = name
                if background and name in _SHELL_BG:
                    existing.status = BACKGROUND
                elif existing.status == BACKGROUND and (
                        not background or name not in _SHELL_BG):
                    existing.status = PENDING
                return existing
            return TC(name, BACKGROUND if background else PENDING)

        tc = apply("Read", True)
        self.assertEqual(tc.status, PENDING)
        self.assertEqual(tc.name, "Read")

        tc = apply("Bash", True)
        self.assertEqual(tc.status, BACKGROUND)
        tc = apply("Read", False, existing=tc)
        self.assertEqual(tc.status, PENDING)
        self.assertEqual(tc.name, "Read")


if __name__ == "__main__":
    unittest.main()
