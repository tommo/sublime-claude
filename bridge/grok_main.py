"""Grok Build ACP bridge — thin agent adapter over AcpBridge.

Spawns `grok agent stdio` (optionally with --model / --always-approve),
authenticates via cached_token, and uses standard session/set_model +
session/set_mode.
"""
import os
import shutil
import sys
from typing import Dict, List, Optional

# MCP read_image returns base64 image blocks; default Grok MCP cap is ~20KB
# which truncates screenshots. Raise for vision tool results.
_MCP_IMAGE_OUTPUT_BYTES = str(8 * 1024 * 1024)

_BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_BRIDGE_DIR)
sys.path.insert(0, _BRIDGE_DIR)
sys.path.insert(0, _PLUGIN_DIR)

from acp_base import AcpBridge, run_bridge  # noqa: E402


GROK_BIN = os.environ.get("GROK_BIN") or shutil.which("grok") or "grok"


class GrokBridge(AcpBridge):
    BACKEND_NAME = "grok"
    DEFAULT_MODEL = "grok-4.5"
    LOG_PATH = "/tmp/grok_bridge.log"

    # Grok accepts many modeId strings; map Claude permission modes to the
    # closest Grok Build modes we care about.
    PERM_TO_MODE = {
        "default": "default",
        "acceptEdits": "default",
        "auto": "default",
        "bypassPermissions": "bypassPermissions",
        "plan": "plan",
    }
    MODE_TO_PERM = {
        "default": "default",
        "plan": "plan",
        "bypassPermissions": "bypassPermissions",
        "agent": "acceptEdits",
        "code": "acceptEdits",
        "ask": "default",
    }
    MODEL_ALIASES = {
        "grok-4.5": "grok-4.5",
        "grok-4-fast": "grok-composer-2.5-fast",
        "grok-composer-2.5-fast": "grok-composer-2.5-fast",
        "composer": "grok-composer-2.5-fast",
        "composer-2.5": "grok-composer-2.5-fast",
    }
    # Grok Build tool ids + rawInput.variant values → Claude formatters.
    # Prefer _meta.x.ai/tool.name when present; variants are a common fallback.
    TOOL_TO_CANONICAL = {
        "read_file": "Read",
        "ReadFile": "Read",
        "search_replace": "Edit",
        "StrReplace": "Edit",
        "write": "Write",
        "WriteFile": "Write",
        "run_terminal_command": "Bash",
        "run_terminal_cmd": "Bash",
        "Shell": "Bash",
        "grep": "Grep",
        "list_dir": "Glob",
        "ListDir": "Glob",
        "List": "Glob",  # never use — defensive if title first-word leaks
        "web_search": "WebSearch",
        "web_fetch": "WebFetch",
        "open_page": "WebFetch",
        "todo_write": "TodoWrite",
        "TodoWrite": "TodoWrite",
        "spawn_subagent": "Task",
        "Task": "Task",
        "get_command_or_subagent_output": "TaskGet",
        "TaskOutput": "TaskGet",
        "TaskGet": "TaskGet",
        "ask_user_question": "ask_user",
        "AskUserQuestion": "ask_user",
        "enter_plan_mode": "EnterPlanMode",
        "exit_plan_mode": "ExitPlanMode",
        "EnterPlanMode": "EnterPlanMode",
        "ExitPlanMode": "ExitPlanMode",
        # MCP tool discovery (NOT web search). Keep own name + formatter.
        "search_tool": "search_tool",
        "SearchTool": "search_tool",
        "search_codebase": "Grep",
        # Single active goal progress (not TodoWrite / Task*).
        "update_goal": "update_goal",
        "UpdateGoal": "update_goal",
        # Media generation (Imagine / video) — keep names for preview UX.
        "image_gen": "image_gen",
        "ImageGen": "image_gen",
        "image_edit": "image_edit",
        "ImageEdit": "image_edit",
        "image_to_video": "image_to_video",
        "ImageToVideo": "image_to_video",
        "reference_to_video": "reference_to_video",
        "ReferenceToVideo": "reference_to_video",
        "video_gen": "video_gen",
        "VideoGen": "video_gen",
        # Vision read of on-disk screenshots (MCP sublime.read_image).
        "read_image": "read_image",
        "ReadImage": "read_image",
        "mcp__sublime__read_image": "read_image",
        # X / Twitter search tools.
        "x_keyword_search": "x_keyword_search",
        "x_semantic_search": "x_semantic_search",
        "x_user_search": "x_user_search",
        "x_thread_fetch": "x_thread_fetch",
        "XKeywordSearch": "x_keyword_search",
        "XSemanticSearch": "x_semantic_search",
        "XUserSearch": "x_user_search",
        "XThreadFetch": "x_thread_fetch",
        # Scheduler / /loop (Grok native; maps from Claude Cron* aliases too).
        "scheduler_create": "scheduler_create",
        "SchedulerCreate": "scheduler_create",
        "CronCreate": "scheduler_create",
        "scheduler_list": "scheduler_list",
        "SchedulerList": "scheduler_list",
        "CronList": "scheduler_list",
        "scheduler_delete": "scheduler_delete",
        "SchedulerDelete": "scheduler_delete",
        "CronDelete": "scheduler_delete",
    }


    def __init__(self) -> None:
        super().__init__()
        self._always_approve: bool = False

    def agent_argv(self) -> List[str]:
        args = [GROK_BIN, "agent"]
        if self.model:
            args += ["--model", self.model]
        # Reasoning effort at spawn — Grok's reliable path (session/set_model
        # does not always rebind effort mid-process).
        if self.effort:
            # max → xhigh via normalize_effort; pass canonical wire level.
            args += ["--reasoning-effort", self.effort]
        # Full bypass: agent skips permission RPCs entirely. Other modes
        # (acceptEdits / default / plan) go through session/request_permission
        # so AcpBridge can apply plugin allowed_tools + auto-allow patterns.
        if self._always_approve or self.permission_mode == "bypassPermissions":
            args.append("--always-approve")
        args.append("stdio")
        return args

    def set_model_params(self) -> dict:
        """Include reasoningEffort so set_model can rebind when supported."""
        params = super().set_model_params()
        if self.effort:
            # Try both top-level and _meta — Grok versions vary.
            params["reasoningEffort"] = self.effort
            params["_meta"] = {
                **(params.get("_meta") or {}),
                "reasoningEffort": self.effort,
            }
        return params

    def spawn_env(self) -> Optional[Dict[str, str]]:
        from acp_base import apply_plain_terminal_env
        env = dict(os.environ)
        # Allow MCP read_image screenshot payloads (default ~20KB is too small).
        for key in ("GROK_MAX_MCP_OUTPUT_BYTES", "MAX_MCP_OUTPUT_BYTES"):
            cur = env.get(key)
            try:
                if cur is not None and int(cur) >= int(_MCP_IMAGE_OUTPUT_BYTES):
                    continue
            except ValueError:
                pass
            env[key] = _MCP_IMAGE_OUTPUT_BYTES
        # Prefer monochrome tool output; terminal/create re-forces this too.
        apply_plain_terminal_env(env)
        return env

    def build_session_meta(self, *, system_prompt: str = "",
                           resume_failed: bool = False) -> Dict:
        meta = super().build_session_meta(
            system_prompt=system_prompt, resume_failed=resume_failed)
        # Grok does not expose MCP tools as bare names — discover with
        # search_tool, call with use_tool. Without this, agents invent
        # read_image / fail to find the vision path for screenshots.
        image_rule = (
            "Images on disk: do NOT use read_file (fails with Cannot read "
            "binary file over ACP). Use MCP via use_tool with "
            "tool_name=\"sublime__read_image\" and tool_input="
            "{\"path\":\"/absolute/path.png\"}. If the tool is unknown, "
            "search_tool query=\"read_image\" first. Media tools "
            "(image_edit/image_gen) accept file paths directly."
        )
        existing = meta.get("rules") or ""
        meta["rules"] = (existing + "\n" + image_rule).strip() if existing else image_rule
        return meta

    def permission_mode_to_agent_mode(self, permission_mode: Optional[str]) -> str:
        pm = permission_mode or "default"
        self._always_approve = (pm == "bypassPermissions")
        return super().permission_mode_to_agent_mode(pm)

    async def after_agent_initialize(self, init_result: dict) -> None:
        # Prefer cached ~/.grok/auth.json; fall back to first advertised method.
        methods = init_result.get("authMethods") or self._auth_methods or []
        method_ids = [m.get("id") for m in methods if isinstance(m, dict)]
        preferred = None
        meta = init_result.get("_meta") or {}
        preferred = meta.get("defaultAuthMethodId")
        if preferred not in method_ids:
            preferred = "cached_token" if "cached_token" in method_ids else (
                method_ids[0] if method_ids else None)
        if not preferred:
            self.log("no auth methods advertised; hoping agent is already authed")
            return
        try:
            await self._send_acp("authenticate", {"methodId": preferred})
            self.log(f"authenticated via {preferred}")
        except Exception as e:
            # Session may still work if process already has credentials.
            self.log(f"authenticate({preferred}) failed: {e}")

    def usage_from_prompt_result(self, result: dict) -> Optional[Dict]:
        return super().usage_from_prompt_result(result)


def main() -> None:
    run_bridge(GrokBridge())


if __name__ == "__main__":
    main()
