"""DSR ACP bridge — thin agent adapter over AcpBridge.

Spawns `dsr acp` and maps dsr tools/modes onto the shared ACP lifecycle.
"""
import os
import shutil
import sys

_BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_BRIDGE_DIR)
sys.path.insert(0, _BRIDGE_DIR)
sys.path.insert(0, _PLUGIN_DIR)

from acp_base import AcpBridge, run_bridge  # noqa: E402


DSR_BIN = os.environ.get("DSR_BIN") or shutil.which("dsr") or "dsr"


class DsrBridge(AcpBridge):
    BACKEND_NAME = "dsr"
    DEFAULT_MODEL = "deepseek-v4-pro"
    LOG_PATH = "/tmp/dsr_bridge.log"

    # dsr edit modes: review | auto | yolo | plan
    PERM_TO_MODE = {
        "default": "review",
        "acceptEdits": "auto",
        "auto": "auto",
        "bypassPermissions": "yolo",
        "plan": "plan",
    }
    MODE_TO_PERM = {
        "review": "default",
        "auto": "acceptEdits",
        "yolo": "bypassPermissions",
        "plan": "plan",
    }
    MODEL_ALIASES = {
        "pro": "deepseek-v4-pro",
        "flash": "deepseek-v4-flash",
        "deepseek-v4-pro": "deepseek-v4-pro",
        "deepseek-v4-flash": "deepseek-v4-flash",
    }
    TOOL_TO_CANONICAL = {
        "read_file": "Read",
        "edit_file": "Edit",
        "write_file": "Write",
        "multi_edit": "Edit",
        "list_directory": "Glob",
        "search_files": "Glob",
        "search_content": "Grep",
        "run_command": "Bash",
        "run_background": "Bash",
        "web_fetch": "WebFetch",
        "web_search": "WebSearch",
        "todo_write": "TodoWrite",
        "ask_choice": "ask_user",
        "submit_plan": "ExitPlanMode",
        "mark_step_complete": "TaskUpdate",
        "revise_plan": "TaskUpdate",
        "spawn_subagent": "Task",
        "run_skill": "Skill",
        "sublime_eval": "sublime_eval",
        "find_file": "find_file",
        "get_window_summary": "get_window_summary",
        "get_symbols": "get_symbols",
        "goto_symbol": "goto_symbol",
        "read_view": "read_view",
        "terminal_run": "mcp__sublime__terminal_run",
        "terminal_read": "mcp__sublime__terminal_read",
        "terminal_list": "mcp__sublime__terminal_list",
        "terminal_close": "mcp__sublime__terminal_read",
    }

    def agent_argv(self):
        mode = self.agent_mode or "review"
        model = self.model or self.DEFAULT_MODEL
        return [DSR_BIN, "acp",
                "--edit-mode=" + mode,
                "--model=" + model]

    async def apply_model(self) -> None:
        # dsr extension method (not standard session/set_model).
        if not self.session_id or not self.model:
            return
        try:
            result = await self._send_acp("_dsr/session/set_model", {
                "sessionId": self.session_id,
                "modelId": self.model,
            }) or {}
            current = result.get("currentModelId")
            if current:
                self.model = current
        except Exception as e:
            self.log(f"_dsr/session/set_model({self.model}) failed: {e}")

    def usage_from_tool_update(self, upd):
        return upd.get("dsr.usage")


def main() -> None:
    run_bridge(DsrBridge())


if __name__ == "__main__":
    main()
