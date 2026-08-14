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

    def test_looks_like_bg_running_title_is_foreground(self):
        # Kimi titles every execute `Running:` — that is wait_for_exit, not ⚙.
        self.assertFalse(AcpBridge._looks_like_background_tool({
            "title": "Running: echo ok && which pil",
            "kind": "execute",
        }))
        self.assertFalse(AcpBridge._looks_like_background_tool({
            "title": "Bash",
            "kind": "execute",
        }, {"command": "echo ok && which pil"}))

    def test_looks_like_bg_accepts_detached_input(self):
        self.assertTrue(AcpBridge._looks_like_background_tool({
            "title": "Bash",
            "kind": "execute",
        }, {"command": "pil test", "detached": True}))
        self.assertTrue(AcpBridge._looks_like_background_tool({
            "title": "Bash",
        }, {"command": "pil test", "run_in_background": True}))

    def test_shell_name_gate(self):
        self.assertTrue(AcpBridge._is_shell_tool_name("Bash"))
        self.assertFalse(AcpBridge._is_shell_tool_name("Read"))
        self.assertFalse(AcpBridge._is_shell_tool_name("TaskGet"))

    def test_script_from_kimi_bash_c_args(self):
        cmd = AcpBridge._script_from_terminal_params(
            "/bin/bash", ["-c", "echo ok && which pil"])
        self.assertEqual(cmd, "echo ok && which pil")
        self.assertEqual(
            AcpBridge._script_from_terminal_params("echo hi", []),
            "echo hi")


class _TermStub(AcpBridge):
    def __init__(self):
        self._last_execute_id = None
        self._pending_execute_ids = []
        self._tool_inputs_by_id = {}
        self._bg_tool_ids = set()
        self._terminal_bg = {}
        self._tool_names_by_id = {}
        self._tool_ids_emitted = set()
        self._terminals = {}

    def file_log(self, msg):
        pass

    def _emit_system(self, *a, **k):
        pass

    def _emit_bg_finished(self, *a, **k):
        pass

    def _should_skip_bg_notify(self, *a, **k):
        return False

    def _write_bg_output_file(self, *a, **k):
        return ""

    def _clip_bg_summary(self, *a, **k):
        return ""


class TestMarkTerminalBg(unittest.TestCase):
    def test_explicit_detached_marks_and_consumes(self):
        b = _TermStub()
        b._note_shell_execute("tool-1", "Bash")
        b._tool_inputs_by_id["tool-1"] = {
            "command": "sleep 9", "detached": True}
        slot = {"cmd": "sleep 9"}
        b._mark_terminal_bg("term_a", slot)
        self.assertTrue(slot.get("bg"))
        self.assertEqual(slot.get("tool_use_id"), "tool-1")
        self.assertIsNone(b._last_execute_id)
        self.assertEqual(b._pending_execute_ids, [])
        # next terminal must not inherit
        slot2 = {"cmd": "echo ok"}
        b._mark_terminal_bg("term_b", slot2)
        self.assertFalse(slot2.get("bg"))

    def test_running_title_only_is_not_bg(self):
        b = _TermStub()
        b._note_shell_execute("tool-2", "Bash")
        b._tool_inputs_by_id["tool-2"] = {"command": "echo ok && which pil"}
        slot = {"cmd": "echo ok && which pil"}
        b._mark_terminal_bg("term_c", slot)
        self.assertFalse(slot.get("bg"))
        # still consumed so it cannot stain a later execute
        self.assertEqual(b._pending_execute_ids, [])

    def test_native_detached_meta_marks(self):
        b = _TermStub()
        b._kimi_detached_meta = lambda cmd: {
            "taskId": "bash-abc", "detached": True, "command": cmd,
        } if "pil test" in cmd else None
        b._link_terminal_to_kimi_task = lambda *a, **k: None
        slot = {"cmd": "cd /proj && pil test -t tree_editor"}
        b._mark_terminal_bg("term_d", slot)
        self.assertTrue(slot.get("bg"))

    def test_wait_for_exit_has_no_fake_success(self):
        import inspect
        src = inspect.getsource(AcpBridge._acp_terminal_wait)
        # kimi-code: exitCode ?? -1. Must not return 0 or null while running.
        self.assertNotIn('return {"exitCode": 0, "signal": None}', src)
        live = [
            ln for ln in src.splitlines()
            if "return" in ln and "exitCode" in ln and not ln.lstrip().startswith("#")
        ]
        self.assertTrue(any("es.get(" in ln for ln in live))
        self.assertFalse(any(
            'None, "signal": None' in ln for ln in live))


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


class _ReplayStub(AcpBridge):
    def __init__(self):
        self._tool_names_by_id = {}
        self._tool_inputs_by_id = {}
        self._tool_ids_emitted = set()
        self._tool_id_alias = {}

    def _handle_mode_update(self, upd):
        pass

    def _handle_commands_update(self, upd):
        pass


class TestLoadReplay(unittest.TestCase):
    def test_load_replay_forwards_user_and_assistant(self):
        import acp_base
        notes = []
        orig = acp_base.send_notification
        acp_base.send_notification = lambda m, p: notes.append((m, p))
        try:
            b = _ReplayStub()
            b._forward_load_replay({
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "hello"},
                },
            })
            b._forward_load_replay({
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "hi back"},
                },
            })
        finally:
            acp_base.send_notification = orig
        kinds = [p.get("type") for _, p in notes]
        self.assertIn("replay_user", kinds)
        self.assertIn("text_delta", kinds)
        text = next(p for _, p in notes if p.get("type") == "text_delta")
        self.assertTrue(text.get("replay"))
        self.assertEqual(text.get("text"), "hi back")


if __name__ == "__main__":
    unittest.main()
