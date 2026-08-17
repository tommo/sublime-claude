"""Background tool gates: TaskOutput is never ⚙ Read (background)."""
import asyncio
import json
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


class TestPeelUseTool(unittest.TestCase):
    def setUp(self):
        self.b = _NameOnly()

    def test_use_tool_peels_jar_kanban(self):
        name = self.b._normalize_tool_name({
            "title": "use_tool",
            "rawInput": {
                "tool_name": "jar-kanban__read",
                "tool_input": {"cmd": "projects"},
            },
            "_meta": {"x.ai/tool": {"name": "use_tool", "kind": "use_tool"}},
        })
        self.assertEqual(name, "jar-kanban__read")

    def test_use_tool_peels_sublime_read_image(self):
        name = self.b._normalize_tool_name({
            "title": "use_tool",
            "rawInput": {
                "tool_name": "sublime__read_image",
                "tool_input": {"path": "/tmp/a.png"},
            },
        })
        self.assertEqual(name, "read_image")

    def test_title_use_tool_without_inner_stays_tool(self):
        name = self.b._normalize_tool_name({
            "title": "use_tool",
            "rawInput": {},
            "kind": "other",
        })
        self.assertNotEqual(name, "use_tool")


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
        self.assertTrue(AcpBridge._looks_like_background_tool({
            "title": "run_terminal_command",
        }, {"command": "pil test -t play", "background": True}))
        self.assertTrue(AcpBridge._looks_like_background_tool({
            "title": "run_terminal_command",
        }, {"command": "pil test -t play", "timeout": 0,
            "description": "Start XR play test in background"}))
        self.assertFalse(AcpBridge._looks_like_background_tool({
            "title": "Bash",
        }, {"command": "echo", "timeout": 0}))

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

    def test_streamed_run_in_background_pairs_on_create(self):
        """⚙ only at terminal/create — streamed flag must stay pending."""
        b = _TermStub()
        b._note_shell_execute("tool-rib", "Bash")
        b._tool_inputs_by_id["tool-rib"] = {
            "command": "pil test -t ui 2>&1 | tail -40",
            "run_in_background": True,
        }
        self.assertNotIn("tool-rib", b._bg_tool_ids)
        slot = {"cmd": "cd '/Volumes/prj/pil' && pil test -t ui 2>&1 | tail -40"}
        b._mark_terminal_bg("term_rib", slot)
        self.assertTrue(slot.get("bg"))
        self.assertEqual(slot.get("tool_use_id"), "tool-rib")
        self.assertIn("tool-rib", b._bg_tool_ids)
        self.assertEqual(b._pending_execute_ids, [])

    def test_log_path_is_per_pid(self):
        class _Log(AcpBridge):
            def __init__(self):
                self.LOG_PATH = "/tmp/kimi_bridge.log"

        p = _Log().log_path()
        self.assertIn(str(os.getpid()), p)
        self.assertNotEqual(p, "/tmp/kimi_bridge.log")

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
                "Task", "Subagent",
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
        tc = apply("Task", True)
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


class _FwdStub(AcpBridge):
    def __init__(self):
        self.session_id = "session_test"
        self._loading_session = False
        self._foreign_session_drops = 0
        self._tool_names_by_id = {}
        self._tool_inputs_by_id = {}
        self._tool_ids_emitted = set()
        self._tool_id_alias = {}
        self._bg_tool_ids = set()
        self._pending_execute_ids = []
        self._last_execute_id = None
        self._terminals = {}
        self._terminal_bg = {}
        self._released_terminals = set()
        self._child_sessions = {}
        self._bg_notified_tasks = set()
        self._bg_notified_tools = set()
        self._bg_tool_ids = set()
        self._tool_titles_by_id = {}
        self._prompt_fut = None
        self._prompt_cancelled = False
        self.TOOL_TO_CANONICAL = dict(AcpBridge.TOOL_TO_CANONICAL)

    def file_log(self, msg):
        pass


class TestForwardNoEarlyGear(unittest.TestCase):
    def test_empty_bash_then_rib_is_not_gear_until_create(self):
        import acp_base
        notes = []
        orig = acp_base.send_notification
        acp_base.send_notification = lambda m, p: notes.append((m, p))
        try:
            b = _FwdStub()
            b._forward_update({
                "sessionId": "session_test",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "19:tool_x",
                    "title": "Bash",
                    "kind": "execute",
                    "status": "pending",
                    "content": [{"type": "content",
                                 "content": {"type": "text", "text": ""}}],
                },
            })
            b._forward_update({
                "sessionId": "session_test",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "19:tool_x",
                    "status": "in_progress",
                    "content": [{"type": "content", "content": {
                        "type": "text",
                        "text": json.dumps({
                            "command": "pil test -t ui 2>&1 | tail -40",
                            "run_in_background": True,
                            "description": "Run copilot UI test",
                        }),
                    }}],
                },
            })
        finally:
            acp_base.send_notification = orig
        uses = [p for _, p in notes if p.get("type") == "tool_use"]
        self.assertEqual(len(uses), 1, uses)
        self.assertFalse(uses[0].get("background"))
        self.assertNotIn("19:tool_x", b._bg_tool_ids)
        self.assertTrue(
            (b._tool_inputs_by_id.get("19:tool_x") or {}).get(
                "run_in_background"))
        self.assertIn("19:tool_x", b._pending_execute_ids)

    def test_terminal_ids_from_update_is_static(self):
        ids = AcpBridge._terminal_ids_from_update({
            "content": [{"type": "terminal", "terminalId": "term_ab"}],
        })
        self.assertEqual(ids, ["term_ab"])
        b = _FwdStub()
        ids = b._terminal_ids_from_update({
            "content": [{"type": "content", "content": {
                "type": "terminal", "terminalId": "term_cd",
            }}],
        })
        self.assertEqual(ids, ["term_cd"])


class TestLoadReplay(unittest.TestCase):
    def test_load_replay_does_not_paint_history(self):
        """session/load replay must not dump turns (kills ◎ via prompt())."""
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
        self.assertNotIn("replay_user", kinds)
        self.assertNotIn("text_delta", kinds)


class TestModalToolDedupe(unittest.TestCase):
    def test_ask_user_aliases(self):
        self.assertTrue(AcpBridge._same_modal_tool("ask_user", "AskUserQuestion"))
        self.assertTrue(AcpBridge._same_modal_tool("ask_user_question", "ask_user"))
        self.assertTrue(AcpBridge._same_modal_tool("ExitPlanMode", "exit_plan_mode"))
        self.assertFalse(AcpBridge._same_modal_tool("ask_user", "ExitPlanMode"))

    def test_ask_title_normalizes_to_ask_user(self):
        b = _NameOnly()
        name = b._normalize_tool_name({
            "title": "Ask: How do you want to handle the box3d swap?",
        })
        self.assertEqual(name, "ask_user")


class TestMarkAgentDead(unittest.TestCase):
    """Grok closes stdout after a successful end_turn — that is not ⚠."""

    def _dead(self, fut):
        import acp_base
        notes = []
        orig = acp_base.send_notification
        acp_base.send_notification = lambda m, p: notes.append((m, p))
        try:
            b = _FwdStub()
            b._agent_exited = False
            b.proc = None
            b.session_id = "s"
            b._prompt_fut = fut
            b._mark_agent_dead("agent stdout closed (returncode=None)")
        finally:
            acp_base.send_notification = orig
        return [p for _, p in notes if p.get("type") == "result"]

    def test_stdio_close_after_end_turn_is_not_failed_turn(self):
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            fut.set_result({"stopReason": "end_turn"})
            results = self._dead(fut)
        finally:
            loop.close()
        self.assertEqual(results, [])

    def test_stdio_close_with_no_prompt_is_not_failed_turn(self):
        self.assertEqual(self._dead(None), [])

    def test_stdio_close_mid_prompt_is_failed_turn(self):
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            results = self._dead(fut)
        finally:
            loop.close()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].get("is_error"))
        self.assertEqual(results[0].get("stop_reason"), "error")


class TestSubagentTerminalOutput(unittest.TestCase):
    """Grok polls spawn_subagent ids via terminal/output — not SIGTERM."""

    def setUp(self):
        self.b = _FwdStub()

    def test_spawn_text_registers_child(self):
        self.b._note_spawned_child(
            "Subagent started in background.\n"
            "subagent_id: 01a00fc4-b4e1-7692-bb0e-e806eba02589\n")
        self.assertIn(
            "01a00fc4-b4e1-7692-bb0e-e806eba02589",
            self.b._child_sessions)

    def test_output_on_live_child_is_not_sigterm(self):
        sid = "01child"
        self.b._register_child_session(sid)
        self.b._ingest_child_session({
            "sessionId": sid,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "still going"},
            },
        })

        async def _go():
            return await self.b._acp_terminal_output({"terminalId": sid})

        result = asyncio.run(_go())
        self.assertEqual(result.get("output"), "still going")
        self.assertNotIn("exitStatus", result)

    def test_output_after_child_turn_completed_has_exit(self):
        sid = "01done"
        self.b._ingest_child_session({
            "sessionId": sid,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "ok"},
            },
        })
        self.b._ingest_child_session({
            "sessionId": sid,
            "update": {
                "sessionUpdate": "turn_completed",
                "stop_reason": "end_turn",
            },
        })

        async def _go():
            return await self.b._acp_terminal_output({"terminalId": sid})

        result = asyncio.run(_go())
        self.assertEqual(result.get("output"), "ok")
        self.assertEqual(result.get("exitStatus"),
                         {"exitCode": 0, "signal": None})

    def test_unknown_term_id_still_cancelled(self):
        async def _go():
            return await self.b._acp_terminal_output(
                {"terminalId": "term_deadbeef"})

        result = asyncio.run(_go())
        self.assertEqual(
            (result.get("exitStatus") or {}).get("signal"), "SIGTERM")

    def test_spawn_is_background_not_poll(self):
        upd = {
            "title": "spawn_subagent",
            "rawInput": {
                "description": "Simple working check",
                "prompt": "say hi",
            },
        }
        self.assertTrue(AcpBridge._is_subagent_spawn("Task", upd, upd["rawInput"]))
        self.assertTrue(AcpBridge._looks_like_background_tool(upd, upd["rawInput"]))
        poll = {
            "title": "Get task output: 01a00fd8-1258-7413-a3ec-50862ba62a91",
            "rawInput": {
                "task_ids": ["01a00fd8-1258-7413-a3ec-50862ba62a91"],
                "timeout_ms": 30000,
            },
        }
        self.assertFalse(
            AcpBridge._is_subagent_spawn("TaskGet", poll, poll["rawInput"]))
        self.assertTrue(self.b._is_subagent_output_poll(poll, "TaskGet"))
        self.assertTrue(self.b._should_suppress_tool_row(poll, "TaskGet"))

    def test_unknown_subagent_id_is_still_running(self):
        async def _go():
            return await self.b._acp_terminal_output(
                {"terminalId": "01a00never-seen"})

        result = asyncio.run(_go())
        self.assertNotIn("exitStatus", result)
        self.assertIn("01a00never-seen", self.b._child_sessions)


if __name__ == "__main__":
    unittest.main()
