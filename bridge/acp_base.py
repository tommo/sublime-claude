"""Generic ACP (Agent Client Protocol) bridge for sublime-claude.

Architecture:
  Sublime plugin ──JSON-RPC──> AcpBridge subclass ──ACP NDJSON──> agent stdio

Subclass hooks (override as needed):
  - agent_argv()              command to spawn (required)
  - normalize_model()         model id aliases
  - permission_mode_to_agent_mode() / agent_mode_to_permission_mode()
  - after_agent_initialize()  e.g. authenticate
  - apply_model() / apply_mode()
  - tool_name_map / usage extraction
  - spawn_env()               extra env for the agent process

Claude-parity surface shared by all ACP backends:
  session_id on init, session/load resume, set_model, set_permission_mode,
  plan mode notifications, system_prompt/_meta, additional dirs, Sublime MCP,
  fs/* + terminal/* client capabilities, native permission UI.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid as uuidlib
from typing import Any, Dict, List, Optional

from base import BaseBridge
from rpc_helpers import send_notification, send_result, send_error


# CSI / OSC sequences leftover when tools ignore NO_COLOR.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))"
)


def strip_ansi(text: str) -> str:
    """Remove ANSI color/style codes from tool/terminal text."""
    if not text or "\x1b" not in text:
        return text or ""
    return _ANSI_ESCAPE_RE.sub("", text)


def apply_plain_terminal_env(env: dict) -> dict:
    """Force monochrome non-TTY env for agent-spawned shells/tools."""
    env["TERM"] = "dumb"
    env.pop("COLORTERM", None)
    env["NO_COLOR"] = "1"
    env["FORCE_COLOR"] = "0"
    env["CLICOLOR"] = "0"
    env["CLICOLOR_FORCE"] = "0"
    env["PAGER"] = "cat"
    env["GIT_PAGER"] = "cat"
    env["DEBIAN_FRONTEND"] = "noninteractive"
    # Line-buffer Python / many CLIs when stdout is a pipe (ACP terminal).
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


# Shared ACP ToolKind → Claude formatter names.
KIND_TO_NAME = {
    "read": "Read",
    "edit": "Edit",
    "write": "Write",
    "execute": "Bash",
    "search": "Grep",
    "glob": "Glob",
    "list": "Glob",
    "fetch": "WebFetch",
    "delete": "Bash",
    "move": "Bash",
    "think": "Thinking",
    "other": "",
}

# Agent / ACP rawInput keys → Claude tool_formatters input shape only.
# Formatters stay Claude-only (file_path, pattern, command, …); all agent
# quirks are normalized here before the plugin sees the tool_use.
INPUT_KEY_MAP = {
    "filePath": "file_path",
    "filepath": "file_path",
    "target_file": "file_path",
    "targetFile": "file_path",
    "oldString": "old_string",
    "newString": "new_string",
    "oldText": "old_string",
    "newText": "new_string",  # Edit; Write also copies to content below
    "contents": "content",  # Grok write body alias
    "oldText": "old_string",
    "newText": "new_string",
    "old_str": "old_string",
    "new_str": "new_string",
    "unifiedDiff": "unified_diff",
    "notebookPath": "notebook_path",
    "subagentType": "subagent_type",
    "replaceAll": "replace_all",
    "target_directory": "pattern",   # list_dir → Glob expects pattern
    "targetDirectory": "pattern",
}


class AcpBridge(BaseBridge):
    """Protocol-level ACP client; agent-specific details live in subclasses."""

    BACKEND_NAME: str = "acp"
    # Opt-in vision MCP tool (mcp__sublime__read_image). Default off for most
    # ACP agents; GrokBridge sets True. Overridable via initialize param
    # mcp_enable_read_image / settings mcp_enable_read_image.
    MCP_ENABLE_READ_IMAGE: bool = False
    DEFAULT_MODEL: str = ""
    CLIENT_NAME: str = "sublime-claude"
    CLIENT_VERSION: str = "0.2"
    LOG_PATH: str = "/tmp/acp_bridge.log"

    # Agent tool name → Claude canonical formatter name.
    TOOL_TO_CANONICAL: Dict[str, str] = {}
    # Claude permission_mode → agent modeId for session/set_mode.
    PERM_TO_MODE: Dict[str, str] = {}
    MODE_TO_PERM: Dict[str, str] = {}
    MODEL_ALIASES: Dict[str, str] = {}

    def __init__(self) -> None:
        super().__init__()
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.session_id: Optional[str] = None
        self.next_acp_id: int = 0
        self.pending: Dict[int, asyncio.Future] = {}
        self.reader_task: Optional[asyncio.Task] = None
        self.model: str = self.DEFAULT_MODEL
        self.effort: str = ""  # reasoning effort (low/medium/high/…); empty = agent default
        self.cwd: str = os.getcwd()
        self.agent_mode: str = ""
        self._view_id: Optional[Any] = None
        self._mcp_enable_read_image: bool = bool(
            getattr(self, "MCP_ENABLE_READ_IMAGE", False))
        self.agent_capabilities: Dict[str, Any] = {}
        self.negotiated_protocol_version: int = 1
        self._terminals: Dict[str, Dict[str, Any]] = {}
        # Generic ACP bg: tool_use ids marked ⚙ + terminalId → job.
        # Kimi bash-*.json tracking lives on KimiBgMixin, not here.
        self._bg_tool_ids: set = set()
        self._terminal_bg: Dict[str, Dict[str, Any]] = {}
        self._bg_notified_tasks: set = set()
        self._bg_notified_tools: set = set()
        # toolCallIds already shown as tool_use (avoid duplicate ☐ rows).
        self._tool_ids_emitted: set = set()
        # Secondary ExitPlanMode/… toolCallId → primary open id (one UI row).
        self._tool_id_alias: Dict[str, str] = {}
        self._loading_session: bool = False
        self._in_plan_mode: bool = False
        self._available_modes: List[dict] = []
        self._available_models: List[dict] = []
        self._resumed: bool = False
        self._resume_fallback: bool = False
        self._auth_methods: List[dict] = []
        self._init_meta: Dict[str, Any] = {}
        # Plugin permission surface (mirrors Claude can_use_tool / settings).
        self.permission_mode: str = "default"
        self.allowed_tools: List[str] = []
        self._auto_allow_patterns: List[str] = []
        self._prompt_cancelled: bool = False
        self._prompt_fut: Optional[asyncio.Future] = None
        self._prompt_acp_id: Optional[int] = None
        # True from first cancel notify until query fully settles — blocks
        # spam session/cancel (Grok ChatStateActor dies on cancel-after-done).
        self._cancel_in_flight: bool = False
        # Grok scheduler: track next fire for loop banner / wakes.
        self._schedule_next_fire: Optional[float] = None
        # toolCallId → last known input (completed updates often omit rawInput).
        self._tool_inputs_by_id: Dict[str, dict] = {}
        # toolCallId → normalized name (completed updates often omit title/_meta).
        self._tool_names_by_id: Dict[str, str] = {}
        self._last_execute_id: Optional[str] = None
        self._pending_execute_ids: List[str] = []
        self._last_bg_tool_id: Optional[str] = None
        self._agent_exited: bool = False
        # Client-side backup timers when host does not inject scheduled prompts.
        # task_id (or toolCallId) → asyncio.Task
        self._client_schedule_tasks: Dict[str, Any] = {}
        # Grok multiplexes subagent session/update on the parent ACP pipe with
        # a different sessionId. Count drops so we can log without spam.
        self._foreign_session_drops: int = 0
        # Serialize writes to agent stdin — concurrent create_task handlers
        # (permission + terminal + fs) would otherwise interleave JSON lines.
        self._acp_write_lock: Optional[asyncio.Lock] = None
        # terminal/wait_for_exit: 0 = wait until process exits (ACP default).
        # Positive = optional client-side cap (seconds). Long agent tools
        # (codex exec, builds) need unlimited wait; use terminal/kill to abort.
        self.terminal_wait_timeout_s: float = float(
            os.environ.get("SUBLIME_CLAUDE_TERM_TIMEOUT", "0") or 0)
        # Hard gates (fail / drop), not soft truncation of useful content.
        # StreamReader limit must be >> largest NDJSON we will parse.
        self.acp_stream_limit = int(
            os.environ.get("SUBLIME_CLAUDE_ACP_STREAM_LIMIT", str(16 * 1024 * 1024)))
        # Inbound agent line: refuse to parse above this (leave headroom under stream).
        self.acp_max_inbound_line = int(
            os.environ.get("SUBLIME_CLAUDE_ACP_MAX_INBOUND", str(8 * 1024 * 1024)))
        # fs/read whole-file response content (chars). Agent must page with line/limit.
        self.fs_read_max_chars = int(
            os.environ.get("SUBLIME_CLAUDE_FS_READ_MAX", str(2 * 1024 * 1024)))
        self.fs_write_max_chars = int(
            os.environ.get("SUBLIME_CLAUDE_FS_WRITE_MAX", str(2 * 1024 * 1024)))
        # terminal/create outputByteLimit clamp (bytes retained in client).
        self.terminal_output_max_bytes = int(
            os.environ.get("SUBLIME_CLAUDE_TERM_OUTPUT_MAX", str(1 * 1024 * 1024)))

    def _get_acp_write_lock(self) -> asyncio.Lock:
        if self._acp_write_lock is None:
            self._acp_write_lock = asyncio.Lock()
        return self._acp_write_lock

    # Tools auto-approved under acceptEdits (file + search), matching Claude
    # Code's "accept edits" posture — Bash still prompts unless listed in
    # allowed_tools / autoAllowedMcpTools. Read-only research tools (WebSearch,
    # search_tool, Grep, …) are included so ACP sessions don't freeze waiting
    # on a permission UI for every search.
    ACCEPT_EDITS_TOOLS = frozenset({
        "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite", "NotebookEdit",
        "WebSearch", "WebFetch", "search_tool", "update_goal",
        "read_image", "mcp__sublime__read_image",
        "x_keyword_search", "x_semantic_search", "x_user_search",
        "x_thread_fetch",
        # Enter plan is free; ExitPlanMode is NEVER auto-allowed (see below).
        "EnterPlanMode",
    })
    # Must show plan approval UI — never auto-allow under acceptEdits/bypass.
    PLAN_EXIT_TOOLS = frozenset({
        "ExitPlanMode", "exit_plan_mode", "exitPlanMode",
        "submit_plan", "SubmitPlan", "exit-plan-mode",
    })
    # Read-only tools safe in plan mode without prompting.
    PLAN_READONLY_TOOLS = frozenset({
        "Read", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite",
        "search_tool", "update_goal",
        "read_image", "mcp__sublime__read_image",
        "x_keyword_search", "x_semantic_search", "x_user_search",
        "x_thread_fetch",
        "scheduler_list", "CronList",
        "EnterPlanMode",
    })
    # ACP / Grok tool kinds that are read-only research (from toolCall.kind or
    # _meta x.ai/tool.kind).
    READONLY_KINDS = frozenset({
        "read", "search", "fetch", "think", "search_tool", "grep", "glob",
    })

    # ── Subclass hooks ─────────────────────────────────────────────────

    def agent_argv(self) -> List[str]:
        """Return the argv used to spawn the ACP agent process."""
        raise NotImplementedError

    def spawn_env(self) -> Optional[Dict[str, str]]:
        """Optional env overrides for the agent process (None → inherit)."""
        return None

    def normalize_model(self, model: Optional[str]) -> str:
        if not model:
            return self.DEFAULT_MODEL
        key = model.strip()
        return self.MODEL_ALIASES.get(
            key, self.MODEL_ALIASES.get(key.lower(), key))

    # Canonical effort levels used by Claude + Grok (Grok also has none/minimal/xhigh).
    EFFORT_ALIASES: Dict[str, str] = {
        "max": "xhigh",  # Claude "max" → Grok xhigh; harmless for Claude
        "x-high": "xhigh",
        "extra_high": "xhigh",
        "extra-high": "xhigh",
    }

    def normalize_effort(self, effort: Optional[str]) -> str:
        """Return a normalized effort string, or '' if unset/invalid."""
        if not effort:
            return ""
        key = str(effort).strip().lower().replace(" ", "_")
        key = self.EFFORT_ALIASES.get(key, key)
        # Accept common levels; agent rejects unknowns at apply time.
        if key in (
            "none", "minimal", "low", "medium", "high", "xhigh", "max",
            "deep",  # per-model menu id some agents accept
        ):
            return key
        return key  # pass through; agent validates

    def permission_mode_to_agent_mode(self, permission_mode: Optional[str]) -> str:
        if not permission_mode:
            return self.PERM_TO_MODE.get("default", "")
        return self.PERM_TO_MODE.get(
            permission_mode, self.PERM_TO_MODE.get("default", permission_mode or ""))

    def agent_mode_to_permission_mode(self, mode: str) -> str:
        return self.MODE_TO_PERM.get(mode, mode)

    async def after_agent_initialize(self, init_result: dict) -> None:
        """Hook after ACP `initialize` (e.g. authenticate)."""
        return None

    def set_model_params(self) -> dict:
        """Params for session/set_model. Subclasses may add effort/_meta."""
        return {
            "sessionId": self.session_id,
            "modelId": self.model,
        }

    async def apply_model(self) -> None:
        """Push self.model (and effort, if any) to the live session."""
        if not self.session_id or not self.model:
            return
        try:
            result = await self._send_acp(
                "session/set_model", self.set_model_params()) or {}
            # Grok: {_meta: {model: {Ok: id}}} ; others may return currentModelId
            current = result.get("currentModelId")
            if not current:
                meta = result.get("_meta") or {}
                model_meta = meta.get("model") or {}
                if isinstance(model_meta, dict):
                    current = model_meta.get("Ok") or model_meta.get("ok")
            if current:
                self.model = current
        except Exception as e:
            self.log(f"session/set_model({self.model}) failed: {e}")

    def _advertised_mode_ids(self) -> List[str]:
        """modeIds from session/new|load modes.availableModes (if any)."""
        ids: List[str] = []
        for m in self._available_modes or []:
            if isinstance(m, dict) and m.get("id"):
                ids.append(str(m["id"]))
            elif isinstance(m, str):
                ids.append(m)
        return ids

    def _resolve_set_mode_id(self) -> str:
        """Map self.agent_mode to a modeId the agent actually advertises.

        Open-source kimi-cli only advertises ``default`` (set_session_mode
        asserts mode_id == "default"). Kimi Code may advertise
        default|plan|auto|yolo. Never send Claude-only ids like acceptEdits.
        """
        want = (self.agent_mode or "default").strip()
        advertised = self._advertised_mode_ids()
        if not advertised:
            # No list yet — only send safe universal id
            if want in ("default", "plan", "auto", "yolo"):
                return want
            return "default"
        if want in advertised:
            return want
        # Fallbacks when host wants acceptEdits/bypass but agent has other names
        for cand in (
            want,
            "yolo" if want in ("acceptEdits", "auto") else "",
            "auto" if want in ("bypassPermissions", "dontAsk") else "",
            "plan" if want == "plan" else "",
            "default",
        ):
            if cand and cand in advertised:
                return cand
        return advertised[0]

    async def apply_mode(self) -> None:
        """Push mode via session/set_mode (only if agent advertises modes)."""
        if not self.session_id:
            return
        mode_id = self._resolve_set_mode_id()
        # kimi-cli OSS: only "default" is valid; skip no-op churn
        advertised = self._advertised_mode_ids()
        if advertised == ["default"] and mode_id == "default":
            self.agent_mode = "default"
            return
        if advertised and mode_id not in advertised:
            self.file_log(
                f"session/set_mode skip unknown modeId={mode_id!r} "
                f"advertised={advertised}")
            return
        try:
            await self._send_acp("session/set_mode", {
                "sessionId": self.session_id,
                "modeId": mode_id,
            })
            self.agent_mode = mode_id
        except Exception as e:
            self.log(f"session/set_mode({mode_id}) failed: {e}")

    def usage_from_tool_update(self, upd: dict) -> Optional[dict]:
        """Optional usage payload embedded in tool_call_update (e.g. dsr.usage)."""
        return upd.get("dsr.usage")

    def usage_from_prompt_result(self, result: dict) -> Optional[dict]:
        """Optional usage from session/prompt result (e.g. Grok _meta tokens)."""
        meta = (result or {}).get("_meta") or {}
        if not meta:
            return None
        # Normalize common token fields if present.
        keys = ("inputTokens", "outputTokens", "cachedReadTokens",
                "reasoningTokens", "totalTokens")
        if not any(k in meta for k in keys):
            return None
        def _tok(key: str) -> int:
            v = meta.get(key)
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0
        return {
            "input_tokens": _tok("inputTokens"),
            "output_tokens": _tok("outputTokens"),
            "cache_read_input_tokens": _tok("cachedReadTokens"),
            "reasoning_tokens": _tok("reasoningTokens"),
            "total_tokens": _tok("totalTokens"),
            "model": meta.get("modelId"),
        }

    def build_session_meta(self, *, system_prompt: str = "",
                           resume_failed: bool = False) -> Dict[str, Any]:
        meta: Dict[str, Any] = {}
        if system_prompt:
            meta["systemPromptOverride"] = system_prompt
        if resume_failed:
            meta["rules"] = (
                "This Sublime session was reopened without a loadable agent "
                "transcript. The user can still see prior UI history; do not "
                "assume you remember earlier turns unless restated."
            )
        return meta

    def log_path(self) -> str:
        """Per-process file. Shared kimi_bridge.log is truncated on every
        spawn and two sessions interleave — diagnoses hit the wrong tab.
        """
        base = self.LOG_PATH
        root, ext = os.path.splitext(base)
        return f"{root}.{os.getpid()}{ext or '.log'}"

    def file_log(self, msg: str) -> None:
        try:
            with open(self.log_path(), "a") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    def _fail_all_pending(self, err: BaseException) -> None:
        """Unblock prompt/RPC waiters when the agent stdio dies."""
        for _rid, fut in list(self.pending.items()):
            if fut is not None and not fut.done():
                fut.set_exception(err)
        self.pending.clear()
        pf = self._prompt_fut
        if pf is not None and not pf.done():
            pf.set_exception(err)

    def _mark_agent_dead(self, reason: str) -> None:
        """Drop a dead agent handle so we do not write to a closed stdin."""
        self._agent_exited = True
        proc = self.proc
        self.proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass
        self.file_log(f"agent dead: {reason}")
        try:
            send_notification("message", {
                "type": "result",
                "session_id": self.session_id or "",
                "duration_ms": 0,
                "is_error": True,
                "num_turns": 1,
                "total_cost_usd": 0,
                "stop_reason": "error",
                "error": reason,
            })
        except Exception:
            pass

    # ── Subprocess lifecycle ───────────────────────────────────────────

    async def _spawn(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            return
        if self.proc is not None:
            self.proc = None
        if self._agent_exited and self.session_id:
            raise RuntimeError("agent process died; restart the session")
        args = self.agent_argv()
        try:
            path = self.log_path()
            with open(path, "w") as f:
                f.write(
                    f"# {self.BACKEND_NAME}-bridge — {args}, "
                    f"cwd={self.cwd} pid={os.getpid()}\n")
            shared = getattr(self, "LOG_PATH", None)
            if shared and shared != path:
                with open(shared, "a") as f:
                    f.write(
                        f"# {self.BACKEND_NAME} pid={os.getpid()} -> {path}\n")
        except Exception:
            pass
        env = self.spawn_env()
        # Default asyncio limit is 64KiB — routine tool_call_update lines exceed
        # that and kill the reader (session freeze). Raise high; app-level gates
        # reject/drop messages that are still unreasonably large.
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=env,
            limit=max(self.acp_stream_limit, 1024 * 1024))
        self.reader_task = asyncio.create_task(self._read_agent_stdout())
        asyncio.create_task(self._read_agent_stderr())

    async def _read_agent_stderr(self) -> None:
        if self.proc is None or self.proc.stderr is None:
            return
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            try:
                self.file_log("[agent stderr] " + line.decode(errors="replace").rstrip())
            except Exception:
                pass

    async def _read_agent_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        max_line = max(self.acp_max_inbound_line, 64 * 1024)
        while True:
            try:
                line = await self.proc.stdout.readline()
            except ValueError as e:
                # Over stream limit — transport may be wedged; stop cleanly.
                self.file_log(f"agent stdout readline failed (limit): {e}")
                self._fail_all_pending(RuntimeError(
                    f"agent stdout readline failed: {e}"))
                self._mark_agent_dead(f"agent stdout readline failed: {e}")
                break
            if not line:
                rc = self.proc.returncode if self.proc else None
                reason = f"agent stdout closed (returncode={rc})"
                self.file_log(f"agent stdout EOF (returncode={rc})")
                self._fail_all_pending(RuntimeError(reason))
                self._mark_agent_dead(reason)
                break
            if len(line) > max_line:
                self.file_log(
                    f"drop oversized agent NDJSON line: {len(line)} bytes "
                    f"(max {max_line}); not parsing")
                continue
            try:
                msg = json.loads(line.decode().strip())
            except json.JSONDecodeError:
                continue
            has_id = "id" in msg
            method = msg.get("method")
            if has_id and method is None:
                fut = self.pending.pop(msg["id"], None)
                if fut is not None and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(
                            msg["error"].get("message", "acp error")))
                    else:
                        fut.set_result(msg.get("result"))
                continue
            params = msg.get("params", {})
            if has_id:
                # Agent → client request (permission, fs, terminal, …).
                self.file_log(
                    f"← acp REQ {method} (id={msg.get('id')}): "
                    f"{json.dumps(params)[:600]}")
                asyncio.create_task(self._dispatch_acp_request(
                    msg["id"], method, params))
                continue
            if method == "session/update":
                kind = (params.get("update") or {}).get("sessionUpdate")
                # Drop child/subagent streams before logging noise (Grok fans
                # subagent tool_call + agent_message onto this same stdio).
                if self._is_foreign_session(params):
                    self._note_foreign_session_drop(
                        kind or "session/update", params)
                    continue
                if kind in (
                    "tool_call", "tool_call_update", "current_mode_update",
                    "scheduled_task_created", "scheduled_task_fired",
                    "scheduled_task_deleted",
                ):
                    self.file_log(f"← acp update {kind}: {json.dumps(params)[:400]}")
                if kind in ("tool_call", "tool_call_update",
                            "agent_message_chunk", "agent_thought_chunk"):
                    # Host uses this to avoid double-painting synthetic
                    # terminal/* tool rows when Kimi also emits tool_call.
                    self._last_session_tool_ts = time.time()
                try:
                    self._forward_update(params)
                except Exception as e:
                    # A crash here used to kill the ACP reader — Kimi's
                    # terminal/create then sat in the pipe and ⚙ never ended.
                    self.file_log(
                        f"forward_update failed kind={kind}: {e}")
            elif method and "mcp" in method.lower():
                # Surface MCP lifecycle (servers_updated, init_progress, …)
                # Still skip foreign-session MCP chatter if tagged.
                if self._is_foreign_session(params):
                    self._note_foreign_session_drop(method, params)
                    continue
                self.file_log(
                    f"← acp {method}: {json.dumps(params)[:600]}")
            elif method in (
                "x.ai/session/update", "_x.ai/session/update",
            ):
                # Grok may nest schedule lifecycle under x.ai/session/update.
                if self._is_foreign_session(params):
                    self._note_foreign_session_drop(method, params)
                    continue
                self.file_log(
                    f"← acp {method}: {json.dumps(params)[:600]}")
                upd = params.get("update") or params
                if isinstance(upd, dict):
                    self._handle_schedule_lifecycle(upd)
            # Other notifications (_x.ai/*, etc.) are intentionally ignored.

    def _acp_id(self) -> int:
        self.next_acp_id += 1
        return self.next_acp_id

    async def _send_acp(self, method: str, params: dict,
                         *, timeout: Optional[float] = None) -> Any:
        await self._spawn()
        assert self.proc is not None and self.proc.stdin is not None
        rid = self._acp_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        line = json.dumps({
            "jsonrpc": "2.0", "id": rid, "method": method, "params": params,
        })
        self.file_log(f"→ acp {method} (id={rid}): {line[:800]}")
        async with self._get_acp_write_lock():
            self.proc.stdin.write((line + "\n").encode())
            await self.proc.stdin.drain()
        try:
            if timeout is not None:
                result = await asyncio.wait_for(fut, timeout=timeout)
            else:
                result = await fut
        except asyncio.TimeoutError:
            self.pending.pop(rid, None)
            self.file_log(f"← acp {method} (id={rid}) TIMEOUT after {timeout}s")
            raise
        try:
            self.file_log(
                f"← acp {method} (id={rid}) result: {json.dumps(result)[:800]}")
        except Exception:
            self.file_log(f"← acp {method} (id={rid}) result: {result!r}")
        return result

    async def _notify_acp(self, method: str, params: dict) -> None:
        """JSON-RPC notification (no id) — used for session/cancel on Grok."""
        await self._spawn()
        assert self.proc is not None and self.proc.stdin is not None
        line = json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params,
        })
        self.file_log(f"→ acp NOTIFY {method}: {line[:800]}")
        async with self._get_acp_write_lock():
            self.proc.stdin.write((line + "\n").encode())
            await self.proc.stdin.drain()

    # ── session/update → Sublime message notifications ─────────────────

    def _is_foreign_session(self, params: Optional[dict]) -> bool:
        """True when update/request is for a subagent (or other) sessionId.

        Grok Build streams subagent turns on the *parent* ACP pipe, each tagged
        with the child sessionId. Painting those agent_message_chunk /
        tool_call rows into the parent view floods the transcript. fs/terminal
        *requests* still use child sessionIds and must keep being answered —
        only UI-bound notifications are filtered via this helper.
        """
        if not params or not isinstance(params, dict):
            return False
        if not self.session_id:
            return False
        sid = params.get("sessionId") or params.get("session_id")
        if not sid:
            return False
        return str(sid) != str(self.session_id)

    def _note_foreign_session_drop(self, kind: str, params: dict) -> None:
        self._foreign_session_drops += 1
        n = self._foreign_session_drops
        # Log first few + then every 50th so parallel subagents are visible
        # without drowning the bridge log.
        if n <= 5 or n % 50 == 0:
            sid = params.get("sessionId") or params.get("session_id") or "?"
            self.file_log(
                f"drop foreign session update #{n} kind={kind!r} "
                f"sid={sid} (parent={self.session_id})")

    def _forward_update(self, params: dict) -> None:
        # Defense in depth: reader already drops foreign sessions; keep
        # filter here if anything calls this path directly.
        if self._is_foreign_session(params):
            kind = (params.get("update") or {}).get("sessionUpdate")
            self._note_foreign_session_drop(kind or "forward", params)
            return

        if self._loading_session:
            self._forward_load_replay(params)
            return

        upd = params.get("update", {})
        kind = upd.get("sessionUpdate")
        # Only suppress streams while *our* host prompt is being cancelled.
        # When idle / auto-continue after end_turn, _prompt_cancelled must not
        # black-hole agent activity (that left the view empty while kimi worked).
        host_prompt_live = (
            self._prompt_fut is not None and not self._prompt_fut.done())
        if self._prompt_cancelled and not host_prompt_live:
            # Stale cancel flag after prompt ended — clear so auto-continue paints
            self._prompt_cancelled = False
        suppress = bool(self._prompt_cancelled and host_prompt_live)
        # After user interrupt: drop *new* tool starts so ☐ rows don't appear
        # post-[interrupted]. Still accept tool_call_update completions so
        # already-open rows can settle.
        if suppress and kind == "tool_call":
            self.file_log(
                f"drop tool_call after cancel: "
                f"{(upd.get('title') or upd.get('toolCallId') or '')!r}")
            return
        if kind == "agent_message_chunk":
            if suppress:
                return
            text = (upd.get("content") or {}).get("text", "")
            if text:
                send_notification("message",
                                  {"type": "text_delta", "text": text})
        elif kind == "agent_thought_chunk":
            if suppress:
                return
            text = (upd.get("content") or {}).get("text", "")
            if text:
                send_notification("message",
                                  {"type": "thinking", "thinking": text})
        elif kind == "tool_call":
            tool_name = self._normalize_tool_name(upd)
            tool_input = self._tool_input_from_update(upd, tool_name)
            tool_name, tool_input = self._reclassify_read_dir(
                tool_name, tool_input)
            tid = upd.get("toolCallId")
            # Kimi lifecycle rows (title "Starting") — never paint ☐/✔ noise
            if self._should_suppress_tool_row(upd, tool_name):
                self.file_log(
                    f"suppress tool_call noise: title={upd.get('title')!r} "
                    f"name={tool_name!r} id={tid!r}")
                return
            if tid:
                if tool_name and tool_name != "tool":
                    self._tool_names_by_id[tid] = tool_name
                if tool_input:
                    prev = self._tool_inputs_by_id.get(tid) or {}
                    self._tool_inputs_by_id[tid] = {**prev, **tool_input}
            # Kimi streams tool_call with empty input before title is useful;
            # still emit when we have a real name so UI is not "☐ tool".
            if tool_name == "tool" and not tool_input:
                # Wait for tool_call_update with title/kind/rawInput
                return
            # Kimi: title "Bash" then JSON drip. Emitting an empty row here
            # plus later run_in_background painted two ⚙ with no process.
            if (self._is_shell_tool_name(tool_name)
                    and not (isinstance(tool_input, dict)
                             and tool_input.get("command"))):
                if tid:
                    self._note_shell_execute(tid, tool_name)
                return
            # One open ExitPlanMode at a time — second toolCallId aliases the first
            # (permission + session/update double-open painted two rows).
            if tool_name in ("ExitPlanMode", "EnterPlanMode") and tid:
                for oid, oname in list(self._tool_names_by_id.items()):
                    if (oname == tool_name and oid != tid
                            and oid in self._tool_ids_emitted):
                        if not hasattr(self, "_tool_id_alias"):
                            self._tool_id_alias = {}
                        self._tool_id_alias[tid] = oid
                        self._tool_names_by_id[tid] = tool_name
                        if tool_input:
                            prev = self._tool_inputs_by_id.get(oid) or {}
                            self._tool_inputs_by_id[oid] = {**prev, **tool_input}
                        self.file_log(
                            f"alias {tool_name} {tid} → open {oid} (no 2nd row)")
                        return
            self._tool_ids_emitted.add(tid)
            self._note_shell_execute(tid, tool_name)
            is_bg = self._looks_like_background_tool(upd, tool_input)
            # Only shell may be ⚙ — TaskOutput/"Reading output…" never.
            if is_bg and not self._is_shell_tool_name(tool_name):
                is_bg = False
            # Cache the flag for terminal/create pairing. Do not ⚙ yet —
            # Kimi spawn is create; ⚙ with no process never ends.
            if is_bg and tid and isinstance(tool_input, dict):
                tool_input = {**tool_input, "run_in_background": True}
                self._tool_inputs_by_id[tid] = {
                    **(self._tool_inputs_by_id.get(tid) or {}),
                    **tool_input,
                }
            send_notification("message", {
                "type": "tool_use",
                "id": tid,
                "name": tool_name,
                "input": tool_input,
                "background": False,
            })
        elif kind == "tool_call_update":
            usage = self.usage_from_tool_update(upd)
            if usage is not None:
                send_notification("message",
                                  {"type": "turn_usage", "usage": usage})
            status = upd.get("status")
            tid = self._resolve_tool_id(upd.get("toolCallId"))
            tool_name = self._normalize_tool_name(upd)
            # Completed updates often strip title/_meta → name becomes "tool".
            # Recover the name we saw on the open tool_call / earlier update.
            if (not tool_name or tool_name == "tool") and tid:
                tool_name = self._tool_names_by_id.get(tid) or tool_name or "tool"
            elif tid and tool_name and tool_name != "tool":
                self._tool_names_by_id[tid] = tool_name
            if status not in ("completed", "failed"):
                self._note_shell_execute(tid, tool_name)
            # Lifecycle rows that never opened a real tool — drop entirely
            if (self._should_suppress_tool_row(upd, tool_name)
                    and (not tid or tid not in self._tool_ids_emitted)):
                self.file_log(
                    f"suppress tool_call_update noise: "
                    f"title={upd.get('title')!r} name={tool_name!r} "
                    f"status={status!r}")
                return
            # Grok: bare tool_call then richer update. Emit tool_use at most
            # once per id (plugin upserts); re-emitting created a second ☐
            # that never received tool_result → last row stuck pending.
            enriched = self._tool_input_from_update(upd, tool_name)
            tool_name, enriched = self._reclassify_read_dir(tool_name, enriched)
            if tid and tool_name and tool_name != "tool":
                self._tool_names_by_id[tid] = tool_name
            if tid and enriched:
                prev = self._tool_inputs_by_id.get(tid) or {}
                self._tool_inputs_by_id[tid] = {**prev, **enriched}
            if tid not in self._tool_ids_emitted:
                # Skip anonymous early stream chunks (Kimi JSON drip without title)
                if tool_name == "tool" and status not in ("completed", "failed"):
                    return
                # Don't open a brand-new row only to close lifecycle noise
                if (tool_name == "tool"
                        and status in ("completed", "failed")
                        and not enriched
                        and not self._tool_update_has_substance(upd)):
                    return
                if (enriched or upd.get("rawInput") or upd.get("locations")
                        or upd.get("title") or status in ("completed", "failed")):
                    # Skip opening rows for lifecycle titles at completed
                    if self._should_suppress_tool_row(upd, tool_name):
                        return
                    self._tool_ids_emitted.add(tid)
                    send_notification("message", {
                        "type": "tool_use",
                        "id": tid,
                        "name": tool_name,
                        "input": enriched or self._tool_inputs_by_id.get(tid) or {},
                    })
            elif tool_name != "tool" or (enriched and status not in ("completed", "failed")):
                # Enrich open row (same id → output.tool upserts). Prefer real name.
                # kimi-cli streams arg JSON one token at a time as tool_call_update;
                # only re-paint when title/args became usable (not every drip).
                enrich_name = tool_name
                if enrich_name == "tool" and tid:
                    enrich_name = self._tool_names_by_id.get(tid) or "tool"
                if not self._should_suppress_tool_row(upd, enrich_name):
                    if status in ("completed", "failed") or self._should_repaint_tool(
                            tid, upd, enriched):
                        send_notification("message", {
                            "type": "tool_use",
                            "id": tid,
                            "name": enrich_name,
                            "input": enriched or self._tool_inputs_by_id.get(tid) or {},
                        })
            # Cache run_in_background for create pairing. Do not ⚙ / do not
            # drop pending here — that left ⚙ unbound when create arrived
            # later (or never), so the row never cleared.
            is_bg = bool(
                tid and self._is_shell_tool_name(tool_name) and (
                    tid in self._bg_tool_ids
                    or self._looks_like_background_tool(upd, enriched)
                )
            )
            if is_bg and tid:
                cached = dict(self._tool_inputs_by_id.get(tid) or {})
                if isinstance(enriched, dict):
                    cached.update(enriched)
                cached["run_in_background"] = True
                self._tool_inputs_by_id[tid] = cached
                for term_id in self._terminal_ids_from_update(upd):
                    slot = self._terminals.get(term_id)
                    if slot is None:
                        continue
                    if tid not in self._bg_tool_ids:
                        self._register_bg_tool(
                            tid, cached, str(upd.get("title") or ""))
                    self._bind_terminal_to_bg_tool(term_id, tid)
                    slot["bg"] = True
                    slot["tool_use_id"] = tid

            if status in ("completed", "failed"):
                # No open row for this id → nothing to close (noise already dropped)
                if tid not in self._tool_ids_emitted and not self._tool_update_has_substance(upd):
                    # may have been suppressed at open
                    if self._should_suppress_tool_row(upd, tool_name) or tool_name == "tool":
                        return
                diff_input = (
                    self._extract_diff_input(upd)
                    if tool_name in ("Edit", "Write") else None
                )
                if diff_input:
                    # Attach diff onto the open row before closing (upsert).
                    payload = dict(enriched or {})
                    payload.update(diff_input)
                    if tid not in self._tool_ids_emitted:
                        self._tool_ids_emitted.add(tid)
                    send_notification("message", {
                        "type": "tool_use",
                        "id": tid,
                        "name": tool_name,
                        "input": payload,
                        "background": bool(tid and tid in self._bg_tool_ids),
                    })
                # Bind terminal ids on completed payload (Kimi attaches them here)
                if tid and tid in self._bg_tool_ids:
                    for term_id in self._terminal_ids_from_update(upd):
                        self._bind_terminal_to_bg_tool(term_id, tid)

                text = self._extract_tool_content(upd, tool_name)
                is_error = status == "failed"
                # Grok read_file marks images failed ("Cannot read binary file")
                # even after a successful fs/read — pixels need read_image, not
                # text FS. Don't paint a red FAILED when path is an image; the
                # agent can still use the path (image_edit) or read_image.
                if is_error:
                    soft = self._soften_image_read_fail(text, enriched, tool_name)
                    if soft is not None:
                        text, is_error = soft

                # TaskOutput / "Reading output of task …" only *polls* a
                # bash-* job — never register it as a new ⚙ background tool.
                _title_l = str(upd.get("title") or "").lower()
                _inp = enriched if isinstance(enriched, dict) else {}
                is_task_poll = (
                    tool_name in ("TaskGet", "TaskOutput", "Task")
                    or "reading output of task" in _title_l
                    or bool(_inp.get("task_id") or _inp.get("taskId"))
                    or (tool_name == "Read" and bool(
                        _inp.get("task_id") or _inp.get("taskId")))
                )
                if self._kimi_handle_tool_result(
                        tid, tool_name, text, enriched, is_task_poll,
                        status, upd):
                    return

                # ACP-terminal background: tool_result is only an ack (host keeps
                # ⚙ until task_notification). Same as Claude run_in_background.
                if (
                    tid
                    and tid in self._bg_tool_ids
                    and status == "completed"
                    and self._is_shell_tool_name(tool_name)
                    and not is_task_poll
                ):
                    send_notification("message", {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": text or "background",
                        "is_error": False,
                    })
                    # Keep name/input maps until process exit notification
                    return
                if tid and tid in self._bg_tool_ids and (
                        is_task_poll or not self._is_shell_tool_name(tool_name)):
                    # Drop mistaken bg mark so normal tool_result can close the row
                    self._bg_tool_ids.discard(tid)

                send_notification("message", {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": text,
                    "is_error": is_error,
                })
                self._tool_ids_emitted.discard(tid)
                # Drop aliases that pointed at this primary
                for alias, primary in list(
                        getattr(self, "_tool_id_alias", {}).items()):
                    if primary == tid or alias == tid:
                        self._tool_id_alias.pop(alias, None)
                if not is_error and tool_name in (
                    "scheduler_create", "CronCreate", "ScheduleWakeup",
                    "scheduler_delete", "CronDelete", "SchedulerDelete",
                ):
                    # completed updates often drop rawInput — use cached input.
                    cached = self._tool_inputs_by_id.get(tid) or {}
                    merged = {**cached, **(enriched or {})}
                    self.file_log(
                        f"scheduler complete name={tool_name} tid={tid} "
                        f"keys={list(merged.keys())}")
                    self._note_scheduler_tool_result(
                        tool_name, merged, text, tool_call_id=tid or "")
                self._tool_inputs_by_id.pop(tid, None)
                self._tool_names_by_id.pop(tid, None)
                self._bg_tool_ids.discard(tid)
        elif kind == "user_message_chunk":
            # Agents (notably Grok) re-broadcast the user prompt. The plugin
            # already renders ◎ <prompt> — do not double-print as text_delta.
            pass
        elif kind == "plan":
            # Kimi TodoWrite lands as ACP plan entries (title/status), not only
            # tool_use.rawInput. Drive the plugin Tasks strip; never dump a
            # **Plan:** text block into the transcript.
            entries = upd.get("entries") or upd.get("plan") or []
            if isinstance(entries, list) and entries:
                send_notification("message", {
                    "type": "plan_todos",
                    "entries": entries,
                })
        elif kind == "current_mode_update":
            self._handle_mode_update(upd)
        elif kind == "available_commands_update":
            self._handle_commands_update(upd)
        elif kind in (
            "scheduled_task_created", "scheduled_task_fired",
            "scheduled_task_deleted",
        ):
            self._handle_schedule_lifecycle(upd)
        # turn_completed / session_summary_generated: ignore (result RPC covers end)
        # elif kind == "turn_completed":
        #     pf = getattr(self, "_prompt_fut", None)
        #     if pf is None or pf.done():
        #         send_notification(...)  # fake result — rejected

    def _forward_load_replay(self, params: dict) -> None:
        """Paint session/load history. Kimi replays before load settles.

        Live `_forward_update` would adopt these as a new turn (⚙) and skip
        `user_message_chunk`. Replay must not pair terminals or mark ⚙.
        """
        upd = params.get("update", {}) or {}
        kind = upd.get("sessionUpdate")
        if kind == "current_mode_update":
            self._handle_mode_update(upd)
            return
        if kind == "available_commands_update":
            self._handle_commands_update(upd)
            return
        # Do not paint load replay into the view. output.prompt() on each
        # user_message_chunk dumps the whole transcript, starts working=True,
        # and exit_input_mode() — ◎ never comes back. Agent already has the
        # history via session/load; UI uses _paint_resume_preview (last turn).
        if kind in (
            "user_message_chunk", "agent_message_chunk",
            "agent_thought_chunk",
        ):
            return
        # if kind == "user_message_chunk":
        #     text = (upd.get("content") or {}).get("text", "")
        #     if text:
        #         send_notification("message", {
        #             "type": "replay_user", "text": text,
        #         })
        #     return
        # if kind == "agent_message_chunk":
        #     text = (upd.get("content") or {}).get("text", "")
        #     if text:
        #         send_notification("message", {
        #             "type": "text_delta", "text": text, "replay": True,
        #         })
        #     return
        # if kind == "agent_thought_chunk":
        #     return
        if kind == "tool_call":
            tool_name = self._normalize_tool_name(upd)
            tool_input = self._tool_input_from_update(upd, tool_name)
            tool_name, tool_input = self._reclassify_read_dir(
                tool_name, tool_input)
            tid = upd.get("toolCallId")
            if self._should_suppress_tool_row(upd, tool_name):
                return
            if tool_name == "tool" and not tool_input:
                return
            if tid:
                if tool_name and tool_name != "tool":
                    self._tool_names_by_id[tid] = tool_name
                if tool_input:
                    prev = self._tool_inputs_by_id.get(tid) or {}
                    self._tool_inputs_by_id[tid] = {**prev, **tool_input}
                self._tool_ids_emitted.add(tid)
            # send_notification("message", {
            #     "type": "tool_use",
            #     "id": tid,
            #     "name": tool_name,
            #     "input": tool_input,
            #     "background": False,
            #     "replay": True,
            # })
            return
        if kind == "tool_call_update":
            status = upd.get("status")
            tid = self._resolve_tool_id(upd.get("toolCallId"))
            tool_name = self._normalize_tool_name(upd)
            if (not tool_name or tool_name == "tool") and tid:
                tool_name = self._tool_names_by_id.get(tid) or tool_name or "tool"
            elif tid and tool_name and tool_name != "tool":
                self._tool_names_by_id[tid] = tool_name
            enriched = self._tool_input_from_update(upd, tool_name)
            tool_name, enriched = self._reclassify_read_dir(tool_name, enriched)
            if tid and enriched:
                prev = self._tool_inputs_by_id.get(tid) or {}
                self._tool_inputs_by_id[tid] = {**prev, **enriched}
            if tid not in self._tool_ids_emitted and (
                    enriched or upd.get("title")
                    or status in ("completed", "failed")):
                if not self._should_suppress_tool_row(upd, tool_name):
                    self._tool_ids_emitted.add(tid)
                    # send_notification("message", {
                    #     "type": "tool_use",
                    #     "id": tid,
                    #     "name": tool_name,
                    #     "input": enriched or self._tool_inputs_by_id.get(tid) or {},
                    #     "background": False,
                    #     "replay": True,
                    # })
            if status in ("completed", "failed"):
                # text = self._extract_tool_content(upd, tool_name)
                # send_notification("message", {
                #     "type": "tool_result",
                #     "tool_use_id": tid,
                #     "content": text,
                #     "is_error": status == "failed",
                #     "replay": True,
                # })
                self._tool_ids_emitted.discard(tid)

    # ── Scheduler / /loop (Grok native) ────────────────────────────────

    @staticmethod
    def _parse_interval_seconds(interval: str) -> Optional[float]:
        """Parse Grok interval strings: 60s, 5m, 2h, 1d (min 60s)."""
        if not interval or not isinstance(interval, str):
            return None
        s = interval.strip().lower()
        m = re.fullmatch(r"(\d+)\s*([smhd])?", s)
        if not m:
            return None
        n = int(m.group(1))
        unit = m.group(2) or "s"
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        sec = float(n * mult)
        return max(60.0, sec) if sec > 0 else None

    @staticmethod
    def _parse_fire_at(value: Any) -> Optional[float]:
        """Parse next_fire_at from epoch, ms, or ISO string → epoch seconds."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            t = float(value)
            # ms timestamps
            if t > 1e12:
                t = t / 1000.0
            return t if t > 0 else None
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            try:
                return float(s)
            except ValueError:
                pass
            try:
                # ISO-8601
                from datetime import datetime
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                return datetime.fromisoformat(s).timestamp()
            except Exception:
                return None
        return None

    def _emit_loop_scheduled(self, fire_at: Optional[float]) -> None:
        self._schedule_next_fire = fire_at
        send_notification("loop_scheduled", {"fire_at": fire_at})
        self.file_log(
            f"loop_scheduled fire_at={fire_at!r}"
            + (f" ({datetime.datetime.fromtimestamp(fire_at).isoformat()})"
               if fire_at else ""))

    def _handle_schedule_lifecycle(self, upd: dict) -> None:
        """Grok sessionUpdate: scheduled_task_created|fired|deleted."""
        kind = (
            upd.get("sessionUpdate")
            or upd.get("kind")
            or upd.get("type")
            or ""
        )
        kind = str(kind).replace("-", "_")
        if kind == "scheduled_task_created" or "created" in kind and "schedul" in kind:
            fire = self._parse_fire_at(
                upd.get("next_fire_at")
                or upd.get("nextFireAt")
                or upd.get("fire_at")
                or upd.get("fireAt")
            )
            if fire is None:
                # Derive from interval on create payload
                interval = (
                    upd.get("interval")
                    or (upd.get("task") or {}).get("interval")
                    or ""
                )
                sec = self._parse_interval_seconds(str(interval)) if interval else None
                if sec:
                    fire = time.time() + sec
            if fire:
                self._emit_loop_scheduled(fire)
            return
        if kind == "scheduled_task_fired" or kind.endswith("task_fired"):
            prompt = (
                upd.get("prompt")
                or upd.get("human_schedule")
                or (upd.get("task") or {}).get("prompt")
                or ""
            )
            # Next fire for recurring (if provided)
            fire = self._parse_fire_at(
                upd.get("next_fire_at") or upd.get("nextFireAt")
            )
            self._emit_loop_scheduled(fire)
            if prompt:
                display = "↻ " + str(prompt).split("\n", 1)[0][:60]
                send_notification("notification_wake", {
                    "wake_prompt": prompt,
                    "display_message": display,
                })
                self.file_log(f"scheduled_task_fired → wake: {prompt[:80]!r}")
            return
        if kind == "scheduled_task_deleted" or "deleted" in kind and "schedul" in kind:
            # Best-effort: clear banner; list may still have other jobs.
            # Prefer next_fire_at if agent includes remaining tasks' soonest fire.
            fire = self._parse_fire_at(
                upd.get("next_fire_at") or upd.get("nextFireAt")
            )
            self._emit_loop_scheduled(fire)
            return

    def _cancel_client_schedule(self, key: str) -> None:
        t = self._client_schedule_tasks.pop(key, None)
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass

    def _arm_client_schedule_backup(
            self, key: str, interval_sec: float, prompt: str,
            fire_immediately: bool, recurring: bool) -> None:
        """Bridge-local timer: inject wake if host never sends scheduled_task_*.

        Grok ACP often creates the schedule server-side but does not always
        push x.ai/scheduled_task_inject_prompt back into the bridge. Claude
        ScheduleWakeup already uses an in-process timer; mirror that here so
        goal+cron dogfood actually re-enters the session.
        """
        if not prompt or interval_sec <= 0:
            return
        self._cancel_client_schedule(key)

        async def _run() -> None:
            try:
                # Let the current turn finish emitting tool_result / end_turn.
                first_delay = 1.5 if fire_immediately else interval_sec
                self.file_log(
                    f"client_schedule[{key}]: first_delay={first_delay:.1f}s "
                    f"interval={interval_sec:.0f}s immediate={fire_immediately} "
                    f"recurring={recurring}")
                await asyncio.sleep(first_delay)
                while True:
                    nxt = (time.time() + interval_sec) if recurring else None
                    self._emit_loop_scheduled(nxt)
                    display = "↻ " + prompt.strip().split("\n", 1)[0][:60]
                    send_notification("notification_wake", {
                        "wake_prompt": prompt,
                        "display_message": display,
                    })
                    self.file_log(
                        f"client_schedule[{key}]: wake fired "
                        f"({len(prompt)} chars)")
                    if not recurring:
                        self._emit_loop_scheduled(None)
                        break
                    await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                self.file_log(f"client_schedule[{key}]: cancelled")
            except Exception as e:
                self.file_log(f"client_schedule[{key}]: error {e}")
            finally:
                self._client_schedule_tasks.pop(key, None)

        try:
            loop = asyncio.get_running_loop()
            self._client_schedule_tasks[key] = loop.create_task(_run())
        except RuntimeError:
            self.file_log(
                f"client_schedule[{key}]: no running loop — cannot arm timer")

    def _note_scheduler_tool_result(
            self, tool_name: str, tool_input: dict, text: str,
            tool_call_id: str = "") -> None:
        """Arm loop banner + client wake backup from scheduler tool results.

        Completed tool_call_update often omits rawInput; callers must pass the
        cached create payload. Host sessionUpdate inject remains preferred when
        present — client timer is a reliability layer for ACP.
        """
        name = (tool_name or "").strip()
        # Delete / cancel → drop backup timer + clear banner if no other jobs.
        if name in ("scheduler_delete", "CronDelete", "SchedulerDelete"):
            del_id = (
                (tool_input or {}).get("id")
                or (tool_input or {}).get("task_id")
                or ""
            )
            data = None
            try:
                data = json.loads(text) if text and text.lstrip().startswith("{") else None
            except Exception:
                pass
            if isinstance(data, dict):
                del_id = del_id or data.get("id") or ""
            if del_id:
                self._cancel_client_schedule(str(del_id))
            if tool_call_id:
                self._cancel_client_schedule(f"tc:{tool_call_id}")
            # If no client jobs left, clear banner.
            if not self._client_schedule_tasks:
                self._emit_loop_scheduled(None)
            self.file_log(f"scheduler delete: cancelled client timer id={del_id!r}")
            return

        interval = (
            (tool_input or {}).get("interval")
            or (tool_input or {}).get("cron")
            or ""
        )
        delay = (tool_input or {}).get("delaySeconds") or (tool_input or {}).get("delay_seconds")
        prompt = (tool_input or {}).get("prompt") or (tool_input or {}).get("message") or ""
        fire_immediately = bool(
            (tool_input or {}).get("fire_immediately")
            or (tool_input or {}).get("fireImmediately")
        )
        recurring = (tool_input or {}).get("recurring")
        if recurring is None:
            recurring = True
        else:
            recurring = bool(recurring)

        fire = None
        task_id = ""
        # Prefer explicit next fire in tool output JSON
        try:
            data = json.loads(text) if text and text.lstrip().startswith("{") else None
        except Exception:
            data = None
        if isinstance(data, dict):
            task_id = str(data.get("id") or data.get("task_id") or "")
            fire = self._parse_fire_at(
                data.get("next_fire_at")
                or data.get("nextFireAt")
                or data.get("fire_at")
            )
            if fire is None and isinstance(data.get("task"), dict):
                fire = self._parse_fire_at(data["task"].get("next_fire_at"))
                task_id = task_id or str(data["task"].get("id") or "")
            # humanSchedule "every 2 minutes" — fall through to interval parse
        sec = None
        if interval:
            sec = self._parse_interval_seconds(str(interval))
            if fire is None and sec:
                fire = time.time() + (1.5 if fire_immediately else sec)
        if fire is None and delay is not None:
            try:
                d = float(delay)
                sec = max(60.0, min(d, 7 * 86400))
                fire = time.time() + sec
            except (TypeError, ValueError):
                pass
        if fire:
            self._emit_loop_scheduled(fire)
            self.file_log(
                f"scheduler tool {tool_name}: armed next_fire≈{fire:.0f} "
                f"immediate={fire_immediately}")
        # Client backup timer (host inject often missing in ACP).
        key = task_id or (f"tc:{tool_call_id}" if tool_call_id else "")
        if key and prompt and sec:
            self._arm_client_schedule_backup(
                key, float(sec), str(prompt), fire_immediately, recurring)
        elif key and prompt and delay is not None and sec:
            self._arm_client_schedule_backup(
                key, float(sec), str(prompt), True, False)

    def _handle_mode_update(self, upd: dict) -> None:
        mode = upd.get("currentModeId") or upd.get("modeId") or ""
        if mode:
            self.agent_mode = mode
        entering_plan = (mode == "plan")
        if entering_plan and not self._in_plan_mode:
            self._in_plan_mode = True
            send_notification("plan_mode_enter", {})
        elif not entering_plan and self._in_plan_mode:
            self._in_plan_mode = False
            send_notification("message", {
                "type": "system",
                "subtype": "mode_update",
                "data": {"mode": mode, "left_plan": True},
            })
        send_notification("message", {
            "type": "system",
            "subtype": "mode_update",
            "data": {
                "mode": mode,
                "permission_mode": self.agent_mode_to_permission_mode(mode),
            },
        })

    def _handle_commands_update(self, upd: dict) -> None:
        cmds = upd.get("availableCommands") or upd.get("commands") or []
        send_notification("message", {
            "type": "system",
            "subtype": "available_commands",
            "data": {"commands": cmds},
        })

    # Claude formatter names we accept as-is (never treat freeform title
    # prose like "Smoke-test subagent harness" as a tool id).
    _CANONICAL_NAMES = frozenset({
        "Read", "Write", "Edit", "Bash", "Glob", "Grep",
        "WebSearch", "WebFetch", "TodoWrite", "Task", "TaskGet",
        "TaskCreate", "TaskUpdate", "TaskList", "NotebookEdit",
        "Skill", "EnterPlanMode", "ExitPlanMode", "ask_user",
        "Thinking",
    })
    # Kimi (and others) emit lifecycle *titles* as tool_call rows — e.g.
    # title="Starting" → was kept as PascalCase tool id → "✔ Starting".
    _LIFECYCLE_TOOL_NOISE = frozenset({
        "starting", "started", "start", "loading", "loaded", "load",
        "thinking", "thought", "working", "processing", "waiting",
        "initializing", "initialize", "init", "preparing", "prepare",
        "running", "done", "finished", "complete", "completed",
        "idle", "ready", "pending", "progress", "status", "update",
        "beginning", "ending", "end", "stop", "stopped", "cancel",
        "cancelled", "canceled", "continue", "continuing",
    })

    def _map_agent_tool_id(self, name: str) -> Optional[str]:
        """Map an agent tool id / variant → Claude formatter name, or None."""
        if not name or not isinstance(name, str):
            return None
        n = name.strip()
        if not n:
            return None
        mapped = self.TOOL_TO_CANONICAL.get(n) or self.TOOL_TO_CANONICAL.get(n.lower())
        if mapped:
            return mapped
        # ReadFile / WriteFile / ListDir → strip trailing File/Dir noise
        if n.endswith("File") and len(n) > 4:
            base = n[:-4]
            if base in self._CANONICAL_NAMES:
                return base
        if n.endswith("Dir") and len(n) > 3:
            # ListDir → Glob (directory listing uses Glob formatter)
            mapped = self.TOOL_TO_CANONICAL.get(n[:-3]) or self.TOOL_TO_CANONICAL.get(
                n[:-3].lower())
            if mapped:
                return mapped
            if n in ("ListDir", "list_dir"):
                return "Glob"
        if n in self._CANONICAL_NAMES:
            return n
        return None

    def _is_lifecycle_tool_noise(self, text: str) -> bool:
        """True for status titles like 'Starting' / 'Working…' (not real tools)."""
        if not text or not isinstance(text, str):
            return False
        t = text.strip().lower().rstrip(".…! ")
        if not t:
            return False
        if t in self._LIFECYCLE_TOOL_NOISE:
            return True
        first = t.split()[0].strip("`'\"*") if t else ""
        return first in self._LIFECYCLE_TOOL_NOISE

    def _title_looks_like_tool_id(self, first: str) -> bool:
        """Accept multi-hump CamelCase (EnterPlanMode) / snake_case; reject Starting."""
        if not first or not first.isascii():
            return False
        if first in self._CANONICAL_NAMES or self._map_agent_tool_id(first):
            return True
        if "_" in first and first.replace("_", "").isalnum():
            return True  # snake_case machine id
        if first[0].isupper() and first.isalnum():
            # Multi-hump: ReadFile, TodoWrite, ExitPlanMode (≥2 capitals)
            if sum(1 for c in first if c.isupper()) >= 2:
                return True
        return False

    def _resolve_tool_id(self, tid: Optional[str]) -> Optional[str]:
        """Map aliased secondary toolCallId → primary open row id."""
        if not tid:
            return tid
        aliases = getattr(self, "_tool_id_alias", None) or {}
        return aliases.get(tid, tid)

    def _tool_update_has_substance(self, upd: dict) -> bool:
        """True if rawInput/locations/content-JSON look like a real tool call."""
        raw = upd.get("rawInput") or {}
        if isinstance(raw, dict) and raw:
            for k in (
                "command", "path", "file_path", "target_file", "query",
                "pattern", "content", "old_string", "new_string", "tool",
                "name", "toolName", "variant", "tool_name", "tool_input",
                "arguments", "prompt", "description", "todos",
            ):
                v = raw.get(k)
                if v is not None and v != "" and v != {} and v != []:
                    return True
        if self._parse_content_args_json(upd):
            return True
        locs = upd.get("locations") or []
        if isinstance(locs, list) and any(
                isinstance(x, dict) and x.get("path") for x in locs):
            return True
        return False

    def _should_suppress_tool_row(self, upd: dict, tool_name: str) -> bool:
        """Drop Kimi lifecycle tool_call noise (✔ Starting)."""
        title = (upd.get("title") or "").strip()
        name = (tool_name or "").strip()
        # Real mapped / mcp tools always keep
        if name and name not in ("tool",):
            if (name in self._CANONICAL_NAMES
                    or self._map_agent_tool_id(name)
                    or name.startswith("mcp__")
                    or name.startswith("sublime__")
                    or "__" in name):
                if not self._is_lifecycle_tool_noise(name):
                    return False
        # Lifecycle title with no real args → suppress
        if self._is_lifecycle_tool_noise(title) or self._is_lifecycle_tool_noise(name):
            if not self._tool_update_has_substance(upd):
                return True
        # Bare single-hump PascalCase name that isn't a known tool
        if (name and name[0].isupper() and name.isalpha()
                and name not in self._CANONICAL_NAMES
                and not self._map_agent_tool_id(name)
                and sum(1 for c in name if c.isupper()) < 2
                and not self._tool_update_has_substance(upd)):
            return True
        return False

    def _normalize_tool_name(self, upd: dict) -> str:
        # 1) Grok advertises the real tool id on _meta.x.ai/tool.name — prefer it.
        meta = upd.get("_meta") or {}
        if isinstance(meta, dict):
            xai = meta.get("x.ai/tool") or meta.get("xai_tool") or {}
            if isinstance(xai, dict):
                mapped = self._map_agent_tool_id(xai.get("name") or "")
                if mapped:
                    return mapped

        # 2) rawInput tool / name / toolName / variant (e.g. variant=Task, ReadFile)
        raw = upd.get("rawInput") or {}
        if isinstance(raw, dict):
            for key in ("tool", "name", "toolName", "variant"):
                mapped = self._map_agent_tool_id(raw.get(key) or "")
                if mapped:
                    return mapped

        # 3) title — tool id, decorated prose, or PascalCase agent names (Kimi).
        # Bare: "spawn_subagent", "list_dir", "TodoList", "AskUserQuestion".
        # Official kimi-cli: "ToolName: key_arg" (session.py get_title).
        # Decorated: "Read `/path`", "Running: grep…", "Asking user questions".
        # Do NOT use first word of free prose / lifecycle ("Starting", "Working").
        title = upd.get("title")
        if isinstance(title, str) and title.strip():
            t = title.strip()
            # Lifecycle titles are never tool ids (Kimi "Starting" spam)
            if self._is_lifecycle_tool_noise(t) and not self._tool_update_has_substance(upd):
                mapped_kind = KIND_TO_NAME.get((upd.get("kind") or "").lower())
                return mapped_kind or "tool"
            mapped = self._map_agent_tool_id(t)
            if mapped:
                return mapped
            # "Read: /path" / "Bash: ls" / "Agent: Implement …" (kimi-cli title)
            if ":" in t:
                head = t.split(":", 1)[0].strip()
                mapped = self._map_agent_tool_id(head)
                if mapped:
                    return mapped
                if head in self._CANONICAL_NAMES:
                    return head
            # Human-prefixed activity titles (Kimi streams these often)
            low = t.lower()
            # TaskOutput polls: "Reading output of task bash-…" — NOT a file Read
            # (was mis-mapped → "⚙ Read (background)" when bg gates misfired).
            if (
                "reading output of task" in low
                or low.startswith("taskoutput")
                or low.startswith("task output")
                or low.startswith("taskget")
            ):
                return "TaskGet"
            if low.startswith(("reading ", "read ")):
                return "Read"
            if low.startswith(("writing ", "write ", "wrote ")):
                return "Write"
            if low.startswith(("editing ", "edit ", "applying ")):
                return "Edit"
            if low.startswith(("running:", "running ", "execute ", "executing ")):
                # "Running" alone is lifecycle noise; "Running: cmd" is Bash
                if low.startswith("running:") or len(t.split()) > 1:
                    return "Bash"
            if low.startswith("launching ") and "agent" in low:
                return "Task"
            if low.startswith("asking ") or "question" in low:
                return "AskUserQuestion"
            if low.startswith("todo") or "todolist" in low.replace(" ", ""):
                return "TodoWrite"
            first = t.split()[0].strip("`'\"*")
            mapped = self._map_agent_tool_id(first)
            if mapped:
                return mapped
            # PascalCase / CamelCase / snake_case only when it looks like a tool id
            if first and self._title_looks_like_tool_id(first):
                mapped = self._map_agent_tool_id(first)
                if mapped:
                    return mapped
                if first in self._CANONICAL_NAMES:
                    return first
                if "_" in first or sum(1 for c in first if c.isupper()) >= 2:
                    return first
            # snake_case / lowercase machine id without map entry
            if (first and first.isascii() and first.replace("_", "").isalnum()
                    and ("_" in first or first.islower())
                    and not self._is_lifecycle_tool_noise(first)):
                return first

        mapped = KIND_TO_NAME.get((upd.get("kind") or "").lower())
        if mapped:
            return mapped
        return "tool"

    def _normalize_tool_input(self, raw: Any, tool_name: str = "") -> dict:
        """Map agent rawInput → Claude formatter keys only."""
        if not isinstance(raw, dict):
            return {}
        # Grok UseTool / MCP wrapper: {tool_name, tool_input:{path:…}} — peel
        # so formatters see the real args (read_image path, etc.).
        nested = raw.get("tool_input") or raw.get("arguments") or raw.get("input")
        if isinstance(nested, dict) and (
                raw.get("variant") in ("UseTool", "use_tool")
                or raw.get("tool_name")
                or raw.get("name")
                or tool_name in (
                    "use_tool", "CallMcpTool", "call_mcp_tool",
                    "read_image", "mcp__sublime__read_image")
                or (isinstance(tool_name, str) and (
                    tool_name.startswith("sublime__")
                    or tool_name.startswith("mcp__sublime__")))):
            # Prefer nested args when present; keep outer keys only as fallback
            peeled = dict(nested)
            for k, v in raw.items():
                if k in ("tool_input", "arguments", "input", "tool_name",
                         "name", "variant", "server"):
                    continue
                peeled.setdefault(k, v)
            raw = peeled
        out: dict = {}
        for k, v in raw.items():
            # Grep search root stays as path (Claude Grep also uses path);
            # don't collapse it into file_path.
            if k == "path" and tool_name in ("Grep", "Glob"):
                out["path"] = v
                continue
            if k == "path" and tool_name in ("Read", "Write", "Edit"):
                out["file_path"] = v
                continue
            if k == "path" and (
                    tool_name in ("read_image", "mcp__sublime__read_image")
                    or (isinstance(tool_name, str)
                        and tool_name.endswith("read_image"))):
                # Keep as path — formatter + MCP expect path, not file_path
                out["path"] = v
                continue
            if k == "path":
                # Default: file path for file tools
                out["file_path"] = v
                continue
            out[INPUT_KEY_MAP.get(k, k)] = v
        # list_dir / Glob: pattern is the display field for Claude Glob formatter
        if tool_name == "Glob" and not out.get("pattern"):
            out["pattern"] = out.get("path") or out.get("file_path") or ""
        # WebSearch-shaped tools: ensure query
        if tool_name == "WebSearch" and not out.get("query"):
            out["query"] = out.get("pattern") or out.get("q") or ""
        # Write: Grok/ACP may only set new_string (diff) or contents
        if tool_name == "Write" and not out.get("content"):
            for alt in ("contents", "new_string", "newText", "text", "body"):
                if out.get(alt):
                    out["content"] = out[alt]
                    break
        return out

    def _reclassify_read_dir(
            self, tool_name: str, tool_input: Optional[dict]) -> tuple:
        """Kimi Read on a directory is a listing — show as Glob, not Read lines."""
        if tool_name != "Read":
            return tool_name, tool_input or {}
        inp = tool_input or {}
        path = inp.get("file_path") or inp.get("path") or ""
        if not path or not os.path.isdir(path):
            return tool_name, inp
        return "Glob", {"pattern": path, "path": path}

    def _should_repaint_tool(
            self, tid: Optional[str], upd: dict, enriched: dict) -> bool:
        """True when a tool_call_update is worth re-sending to the plugin UI.

        kimi-cli ToolCallProgress re-sends full args every delta (session.py
        _send_tool_call_part). Re-painting each char floods the transcript.
        """
        if not tid:
            return True
        title = (upd.get("title") or "").strip()
        prev_title = getattr(self, "_tool_titles_by_id", {}).get(tid) or ""
        if not hasattr(self, "_tool_titles_by_id"):
            self._tool_titles_by_id = {}
        if title and title != prev_title:
            self._tool_titles_by_id[tid] = title
            # Prefer titles that gained a subtitle ("Bash: cmd") or Agent label
            if ":" in title or len(title) > len(prev_title) + 2:
                return True
        # Full rawInput or complete content JSON → paint once usable
        if isinstance(upd.get("rawInput"), dict) and upd.get("rawInput"):
            prev = self._tool_inputs_by_id.get(tid) or {}
            if not prev or any(
                    enriched.get(k) and enriched.get(k) != prev.get(k)
                    for k in ("file_path", "path", "command", "pattern",
                              "description", "query", "content", "old_string")):
                return True
        if self._parse_content_args_json(upd):
            prev = self._tool_inputs_by_id.get(tid) or {}
            if not prev:
                return True
            # Only if a display-critical field newly appeared or grew a lot
            for k in ("file_path", "path", "command", "pattern", "description"):
                a, b = str(prev.get(k) or ""), str(enriched.get(k) or "")
                if b and (not a or len(b) > len(a) + 8):
                    return True
        return False

    def _parse_content_args_json(self, upd: dict) -> dict:
        """Parse tool args JSON from ACP content blocks (kimi-cli official).

        ToolCallStart / ToolCallProgress put accumulated args as::
          content: [{type: "content", content: {type: "text", text: "{...}"}}]
        Partial streams fail json.loads — return {}.
        """
        for block in (upd.get("content") or []):
            if not isinstance(block, dict):
                continue
            text = None
            if block.get("type") == "content":
                inner = block.get("content")
                if isinstance(inner, dict):
                    text = inner.get("text")
                elif isinstance(inner, str):
                    text = inner
            elif block.get("type") == "text":
                text = block.get("text")
            if not isinstance(text, str):
                continue
            s = text.strip()
            if not s.startswith("{"):
                continue
            try:
                data = json.loads(s)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            if isinstance(data, dict) and data:
                return data
        return {}

    def _tool_input_from_update(self, upd: dict, tool_name: str = "") -> dict:
        """Claude-formatter-ready input from rawInput + content JSON + title.

        kimi-cli (github.com/MoonshotAI/kimi-cli acp/session.py) streams args
        as content text JSON, not only rawInput.
        """
        out = self._normalize_tool_input(upd.get("rawInput") or {}, tool_name)
        # Official ACP: full args JSON in content blocks
        if not out:
            content_args = self._parse_content_args_json(upd)
            if content_args:
                out = self._normalize_tool_input(content_args, tool_name)
        else:
            # Merge content keys as fill-ins when rawInput sparse
            content_args = self._parse_content_args_json(upd)
            if content_args:
                extra = self._normalize_tool_input(content_args, tool_name)
                for k, v in extra.items():
                    if v and not out.get(k):
                        out[k] = v
        for loc in (upd.get("locations") or []):
            if not isinstance(loc, dict) or not loc.get("path"):
                continue
            if tool_name in ("Grep", "Glob"):
                out.setdefault("path", loc["path"])
                if tool_name == "Glob":
                    out.setdefault("pattern", loc["path"])
            else:
                out.setdefault("file_path", loc["path"])
            break
        title = upd.get("title") or ""
        # Title often embeds path: Read `/abs/path`
        if not out.get("file_path") and not out.get("pattern"):
            if isinstance(title, str) and "`" in title:
                try:
                    path = title.split("`")[1]
                    if path:
                        if tool_name == "Glob":
                            out.setdefault("pattern", path)
                        elif tool_name == "Grep":
                            out.setdefault("path", path)
                        else:
                            out.setdefault("file_path", path)
                except IndexError:
                    pass
        # kimi-cli get_title: "ToolName: key_arg" when args partial
        if isinstance(title, str) and ":" in title:
            _, _, sub = title.partition(":")
            sub = sub.strip()
            if sub:
                if tool_name == "Bash" and not out.get("command"):
                    out["command"] = sub
                elif tool_name in ("Read", "Write", "Edit") and not out.get(
                        "file_path"):
                    out["file_path"] = sub.split()[0] if sub else sub
                elif tool_name in ("Grep", "Glob") and not out.get("pattern"):
                    out["pattern"] = sub
                elif tool_name == "Task" and not out.get("description"):
                    out["description"] = sub[:200]
        # Task / Kimi Agent: description in rawInput or title
        # ("Launching coder agent: Implement render.playground…")
        if tool_name == "Task":
            if not out.get("description"):
                for k in ("description", "prompt", "task"):
                    v = out.get(k)
                    if isinstance(v, str) and v.strip():
                        # Prefer short description; prompt is often huge
                        if k == "prompt" and len(v) > 120:
                            out["description"] = v.strip().split("\n", 1)[0][:100]
                        else:
                            out["description"] = v.strip()[:200]
                        break
            if isinstance(title, str):
                t = title.strip()
                if t and not out.get("description"):
                    low = t.lower()
                    # Strip "Launching coder agent: " prefix (Kimi)
                    for prefix in (
                        "launching coder agent:",
                        "launching agent:",
                        "launching explore agent:",
                        "running agent:",
                    ):
                        if low.startswith(prefix):
                            t = t[len(prefix):].strip()
                            low = t.lower()
                            break
                    if t and low not in (
                            "task", "agent", "agentswarm", "spawn_subagent",
                            "spawn subagent"):
                        out["description"] = t[:200]
                # "Launching coder agent: …" → subagent_type=coder
                if not out.get("subagent_type") and isinstance(title, str):
                    m = re.search(
                        r"\b(coder|explore|general|reviewer|plan)\b",
                        title, flags=re.I)
                    if m:
                        out["subagent_type"] = m.group(1).lower()
            if not out.get("subagent_type"):
                st = (
                    out.get("subagentType")
                    or out.get("subagent_type")
                    or out.get("type")
                    or out.get("agent_type")
                    or ""
                )
                if st:
                    out["subagent_type"] = str(st)
        # TaskGet / get_command_or_subagent_output: task_ids → taskId
        if tool_name == "TaskGet" and not out.get("taskId"):
            ids = out.get("task_ids") or out.get("taskIds") or []
            if isinstance(ids, list) and ids:
                out["taskId"] = str(ids[0])
            elif out.get("task_id"):
                out["taskId"] = str(out["task_id"])
        return out

    @staticmethod
    def _extract_tool_content(upd: dict, tool_name: str = "") -> str:
        out: list = []
        for block in (upd.get("content") or []):
            if not isinstance(block, dict):
                continue
            inner = block.get("content") if block.get("type") == "content" else block
            if isinstance(inner, dict):
                if inner.get("type") == "text" and inner.get("text"):
                    out.append(inner["text"])
                elif inner.get("type") == "diff" and inner.get("newText"):
                    out.append(inner["newText"])
        if out:
            return "\n".join(out)
        raw = upd.get("rawOutput")
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            if tool_name == "Bash":
                stdout = raw.get("stdout") or ""
                stderr = raw.get("stderr") or ""
                joined = stdout + (("\n" + stderr) if stderr.strip() else "")
                if joined:
                    return joined
            try:
                return json.dumps(raw, ensure_ascii=False, indent=2)
            except Exception:
                return str(raw)
        return str(raw)

    def _soften_image_read_fail(
            self, text: str, tool_input: Optional[dict],
            tool_name: str) -> Optional[tuple]:
        """Rewrite Grok image read_file fails so UI is not red FAILED.

        Returns (new_text, is_error) or None if not this case.
        """
        t = (text or "").strip()
        low = t.lower()
        if "cannot read binary" not in low and "binary file" not in low:
            return None
        path = ""
        if isinstance(tool_input, dict):
            path = (
                tool_input.get("file_path")
                or tool_input.get("target_file")
                or tool_input.get("path")
                or ""
            )
        if not path and ":" in t:
            # "Cannot read binary file: /abs/path.png"
            path = t.split(":", 1)[-1].strip()
        path_l = (path or "").lower()
        is_img = any(path_l.endswith(e) for e in self._IMAGE_EXTS)
        if not is_img and tool_name not in ("Read", "read_file", "ReadFile"):
            return None
        if not is_img and "cannot read binary" not in low:
            return None
        # Still soften when path missing but message is the binary-file stock error
        # on a Read tool (Grok image reads).
        if not is_img and tool_name not in ("Read", "read_file", "ReadFile", ""):
            return None
        if not is_img and not path:
            # generic binary fail — leave as error
            return None
        if not is_img:
            return None
        note = (
            f"Image on disk: {path}\n"
            f"read_file cannot load pixels over ACP. For vision call "
            f"use_tool with tool_name=\"sublime__read_image\" and "
            f"tool_input={{\"path\": {path!r}}} "
            f"(search_tool query=\"read_image\" if needed). "
            f"image_edit/image_gen can take this path directly."
        )
        self.file_log(
            f"soften image read fail → non-error UI for {path!r}")
        return note, False

    @staticmethod
    def _extract_diff_input(upd: dict) -> Optional[dict]:
        for block in (upd.get("content") or []):
            if not isinstance(block, dict):
                continue
            inner = block.get("content") if block.get("type") == "content" else block
            if isinstance(inner, dict) and inner.get("type") == "diff":
                out: dict = {}
                if inner.get("path"):
                    out["file_path"] = inner["path"]
                if inner.get("oldText") is not None:
                    out["old_string"] = inner["oldText"]
                if inner.get("newText") is not None:
                    # Edit formatter uses new_string; Write size uses content.
                    out["new_string"] = inner["newText"]
                    out["content"] = inner["newText"]
                if out:
                    return out
        return None

    # ── BaseBridge overrides ───────────────────────────────────────────

    def extra_dispatch(self):
        return {
            "set_model": self.handle_set_model,
            "set_permission_mode": self.handle_set_permission_mode,
            # plan_response: BaseBridge.handle_plan_response (+ mode switch override)
            "rewind_points": self.handle_rewind_points,
            "rewind_execute": self.handle_rewind_execute,
            "cancel_loop": self.handle_cancel_loop,
            # Same RPC as Claude SDK bridge — host polls when idle with ⚙ tasks.
            "poll_bg_tasks": self.handle_poll_bg_tasks,
        }

    async def handle_poll_bg_tasks(self, req_id: Optional[int],
                                    params: dict) -> None:
        """Host idle poll while ⚙ tasks are live. Kimi mixin scans bash-*.json."""
        checked = 0
        poll = getattr(self, "_poll_kimi_bg_tasks", None)
        if callable(poll):
            try:
                checked = poll()
            except Exception as e:
                self.file_log(f"poll_bg_tasks: {e}")
        running = list(self._live_bg_task_ids())
        send_result(req_id, {
            "pending": len(running),
            "checked": checked,
            "running": running,
        })

    def _live_bg_task_ids(self) -> set:
        ids = set()
        for info in (self._terminal_bg or {}).values():
            tid = info.get("task_id")
            if tid:
                ids.add(tid)
        extra = getattr(self, "_live_kimi_task_ids", None)
        if callable(extra):
            ids.update(extra())
        return ids

    def _kimi_handle_tool_result(
            self, tid, tool_name, text, enriched, is_task_poll, status, upd) -> bool:
        return False

    @staticmethod
    def _clip_bg_summary(summary: str, code=None) -> str:
        summary = (summary or "").strip()
        if "\n" in summary:
            summary = " ⏎ ".join(
                s.strip() for s in summary.splitlines() if s.strip())
        if len(summary) > 80:
            summary = summary[:79] + "…"
        if code is not None:
            summary = f"{summary} (exit {code})"
        return summary

    def _write_bg_output_file(self, prefix: str, body: str) -> str:
        try:
            fd, path = tempfile.mkstemp(prefix=prefix, suffix=".log", text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body or "")
            return path
        except Exception as e:
            self.file_log(f"bg output_file write failed: {e}")
            return ""

    def _emit_bg_finished(
            self, task_id: str, tool_use_id: str, status: str,
            summary: str, output_file: str) -> None:
        self._mark_bg_notified(task_id, tool_use_id)
        self._emit_system("task_updated", {
            "task_id": task_id,
            "tool_use_id": tool_use_id,
            "patch": {"status": status},
        })
        self._emit_system("task_notification", {
            "task_id": task_id,
            "tool_use_id": tool_use_id,
            "status": status,
            "summary": summary,
            "output_file": output_file,
        })
        if tool_use_id:
            self._bg_tool_ids.discard(tool_use_id)
            self._tool_inputs_by_id.pop(tool_use_id, None)
            self._tool_names_by_id.pop(tool_use_id, None)

    _SHELL_BG_NAMES = frozenset({
        "Bash", "Shell", "execute", "run_terminal_command", "tool",
    })

    @classmethod
    def _is_shell_tool_name(cls, name: str) -> bool:
        return (name or "") in cls._SHELL_BG_NAMES or (name or "") == "Workflow"

    @staticmethod
    def _looks_like_background_tool(upd: dict, tool_input: Optional[dict] = None) -> bool:
        """True only for explicit detach — not every Kimi `Running:` shell.

        Kimi titles *all* execute `Running: <cmd>`. That is foreground +
        wait_for_exit. Only `run_in_background` / `detached` / Starting
        background is ⚙. TaskOutput is never ⚙.
        """
        title = str(upd.get("title") or "")
        low = title.lower().strip()
        # Poll tools are foreground — never ⚙
        if "reading output of task" in low or low.startswith("taskoutput"):
            return False
        if low.startswith("starting background"):
            return True
        if low.startswith("running in background") or low.startswith("background task"):
            return True
        ri = tool_input if isinstance(tool_input, dict) else {}
        if not ri:
            raw = upd.get("rawInput")
            if isinstance(raw, dict):
                ri = raw
            elif isinstance(raw, str) and raw.strip().startswith("{"):
                try:
                    ri = json.loads(raw)
                except Exception:
                    ri = {}
        # task_id alone = poll args, not a detached shell
        if isinstance(ri, dict) and (
                ri.get("task_id") or ri.get("taskId")) and not ri.get("command"):
            return False
        if isinstance(ri, dict) and (
                ri.get("run_in_background") is True
                or ri.get("detached") is True
                or ri.get("background") is True):
            return True
        # Grok: timeout 0 on run_terminal_command = no-timeout / bg
        # (same session: background:true detaches; timeout:0 still
        # wait_for_exit — still ⚙ so the row is not a blocking ☐).
        if isinstance(ri, dict) and ri.get("timeout") in (0, 0.0):
            if "run_terminal_command" in title or title.startswith("Execute "):
                return True
        return False

    def _note_shell_execute(self, tid: Optional[str], tool_name: str) -> None:
        """Remember this shell tool so the next terminal/create can pair to it."""
        if not tid or not self._is_shell_tool_name(tool_name):
            return
        self._last_execute_id = tid
        pending = getattr(self, "_pending_execute_ids", None)
        if pending is None:
            self._pending_execute_ids = []
            pending = self._pending_execute_ids
        if tid not in pending:
            pending.append(tid)

    def _drop_pending_execute(self, tid: Optional[str]) -> None:
        if not tid:
            return
        pending = getattr(self, "_pending_execute_ids", None) or []
        self._pending_execute_ids = [x for x in pending if x != tid]
        if getattr(self, "_last_execute_id", None) == tid:
            self._last_execute_id = None

    def _take_pending_execute_id(self) -> Optional[str]:
        pending = getattr(self, "_pending_execute_ids", None) or []
        if pending:
            eid = pending.pop(0)
            self._pending_execute_ids = pending
            if getattr(self, "_last_execute_id", None) == eid:
                self._last_execute_id = None
            return eid
        eid = getattr(self, "_last_execute_id", None)
        self._last_execute_id = None
        return eid

    @staticmethod
    def _script_from_terminal_params(cmd, args_in) -> str:
        """Real command for display/match. Kimi: /bin/bash + args=['-c', script]."""
        args = list(args_in or [])
        for i, a in enumerate(args):
            if str(a) in ("-c",) and i + 1 < len(args):
                return str(args[i + 1])
        return str(cmd or "")

    @staticmethod
    def _terminal_ids_from_update(upd: dict) -> List[str]:
        out = []
        for block in (upd.get("content") or []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "terminal" and block.get("terminalId"):
                out.append(str(block["terminalId"]))
            inner = block.get("content") if block.get("type") == "content" else None
            if isinstance(inner, dict) and inner.get("type") == "terminal":
                tid = inner.get("terminalId")
                if tid:
                    out.append(str(tid))
        return out

    def _emit_system(self, subtype: str, data: dict) -> None:
        """Same envelope as Claude SDK bridge SystemMessage → host dispatch."""
        send_notification("message", {
            "type": "system",
            "subtype": subtype,
            "data": data or {},
        })

    def _should_skip_bg_notify(self, task_id: str, tool_use_id: str = "") -> bool:
        """True if we already sent task_notification for this logical bg job."""
        if task_id and task_id in self._bg_notified_tasks:
            return True
        if tool_use_id and tool_use_id in self._bg_notified_tools:
            return True
        return False

    def _mark_bg_notified(self, task_id: str, tool_use_id: str = "") -> None:
        if task_id:
            self._bg_notified_tasks.add(task_id)
        if tool_use_id:
            self._bg_notified_tools.add(tool_use_id)
        # Bound aliases for the same terminal/tool
        for term_id, info in list(self._terminal_bg.items()):
            if (task_id and info.get("task_id") == task_id) or (
                    tool_use_id and info.get("tool_use_id") == tool_use_id):
                tid = info.get("task_id")
                if tid:
                    self._bg_notified_tasks.add(str(tid))
                tuid = info.get("tool_use_id")
                if tuid:
                    self._bg_notified_tools.add(str(tuid))

    def _register_bg_tool(self, tool_use_id: str, tool_input: dict = None,
                          title: str = "") -> None:
        if not tool_use_id:
            return
        # Title-only poll tools must never become ⚙
        tlow = (title or "").lower()
        if "reading output of task" in tlow or tlow.startswith("taskoutput"):
            return
        name = self._tool_names_by_id.get(tool_use_id) or "Bash"
        # Never promote Read / TaskOutput / etc. to ⚙ background
        if not self._is_shell_tool_name(name):
            return
        if name == "tool":
            name = "Bash"
        already = tool_use_id in self._bg_tool_ids
        self._bg_tool_ids.add(tool_use_id)
        self._last_bg_tool_id = tool_use_id
        inp = dict(tool_input or self._tool_inputs_by_id.get(tool_use_id) or {})
        if title and not inp.get("command"):
            cmd = title
            for prefix in (
                "Starting background:", "Starting background",
                "Running:",
            ):
                if cmd.startswith(prefix):
                    cmd = cmd[len(prefix):].strip()
                    break
            if cmd:
                inp.setdefault("command", cmd)
        inp["run_in_background"] = True
        self._tool_inputs_by_id[tool_use_id] = {
            **(self._tool_inputs_by_id.get(tool_use_id) or {}),
            **inp,
        }
        self._tool_names_by_id[tool_use_id] = "Bash"
        self._tool_ids_emitted.add(tool_use_id)
        if already:
            return
        send_notification("message", {
            "type": "tool_use",
            "id": tool_use_id,
            "name": "Bash",
            "input": self._tool_inputs_by_id[tool_use_id],
            "background": True,
        })

    def _bind_terminal_to_bg_tool(self, terminal_id: str, tool_use_id: str) -> None:
        if not terminal_id or not tool_use_id:
            return
        if tool_use_id not in self._bg_tool_ids:
            return
        if terminal_id in self._terminal_bg:
            return
        cmd = (self._tool_inputs_by_id.get(tool_use_id) or {}).get("command", "")
        task_id = f"acp-term-{terminal_id}"
        self._terminal_bg[terminal_id] = {
            "task_id": task_id,
            "tool_use_id": tool_use_id,
            "cmd": cmd,
        }
        self._emit_system("task_started", {
            "task_id": task_id,
            "tool_use_id": tool_use_id,
        })
        self.file_log(
            f"bg terminal bound term={terminal_id} tool={tool_use_id} "
            f"task={task_id}")
        slot = self._terminals.get(terminal_id) or {}
        reader = slot.get("reader")
        if slot.get("exit_status") is not None and (
                reader is None or reader.done()):
            self._emit_bg_terminal_complete(terminal_id)

    def _emit_bg_terminal_complete(self, terminal_id: str) -> None:
        """ACP process exit → one Claude task_notification."""
        info = self._terminal_bg.pop(terminal_id, None)
        if not info:
            return
        task_id = info.get("task_id") or f"acp-term-{terminal_id}"
        tool_use_id = info.get("tool_use_id") or f"bg-{task_id}"
        if self._should_skip_bg_notify(task_id, tool_use_id):
            return
        slot = self._terminals.get(terminal_id) or {}
        out = (slot.get("stdout") or "") + (slot.get("stderr") or "")
        es = slot.get("exit_status") or {}
        code = es.get("exitCode")
        if code is None and es.get("signal"):
            status = "failed"
        elif code is None or int(code) == 0:
            status = "completed"
        else:
            status = "failed"
        output_file = self._write_bg_output_file("acp-bg-", out or "")
        raw = (info.get("cmd") or tool_use_id or task_id or "").strip()
        summary = self._clip_bg_summary(raw, code)
        self._emit_bg_finished(
            task_id, tool_use_id, status, summary, output_file)

    async def handle_cancel_loop(self, req_id: Optional[int],
                                  params: dict) -> None:
        """Cancel all client-side schedule backups and clear the loop banner."""
        keys = list(self._client_schedule_tasks.keys())
        for key in keys:
            self._cancel_client_schedule(key)
        self._emit_loop_scheduled(None)
        self.file_log(f"cancel_loop: cleared {len(keys)} client schedule(s)")
        send_result(req_id, {"ok": True, "cancelled": len(keys)})

    async def handle_initialize(self, req_id: Optional[int],
                                 params: dict) -> None:
        self.model = self.normalize_model(params.get("model") or self.model)
        # Capture effort before first agent spawn (Grok CLI flag is spawn-time).
        self.effort = self.normalize_effort(params.get("effort"))
        self._agent_exited = False
        if self.effort:
            self.file_log(f"initialize: effort={self.effort!r}")
        self.cwd = params.get("cwd") or self.cwd
        if self.cwd and os.path.isdir(self.cwd):
            try:
                os.chdir(self.cwd)
            except OSError:
                pass
        self._view_id = params.get("view_id")
        # Optional vision MCP tool — default from class (Grok=on); host may
        # override via mcp_enable_read_image (settings auto/true/false).
        if "mcp_enable_read_image" in params:
            self._mcp_enable_read_image = bool(params.get("mcp_enable_read_image"))
        else:
            self._mcp_enable_read_image = bool(
                getattr(self, "MCP_ENABLE_READ_IMAGE", False))
        # Plugin permission rules — same payload Claude bridge receives.
        self.permission_mode = params.get("permission_mode") or "default"
        raw_allowed = params.get("allowed_tools") or []
        self.allowed_tools = [
            str(t) for t in raw_allowed if isinstance(t, str) and t.strip()
        ]
        self._reload_auto_allow_patterns()
        self.agent_mode = self.permission_mode_to_agent_mode(
            self.permission_mode)
        self.file_log(
            f"permissions: mode={self.permission_mode!r} "
            f"allowed_tools={self.allowed_tools} "
            f"auto_patterns={len(self._auto_allow_patterns)}")
        resume_id = params.get("resume")
        fork_session = bool(params.get("fork_session", False))
        system_prompt = params.get("system_prompt") or ""
        additional_dirs = params.get("additional_dirs") or []

        try:
            # kimi-code 0.36: AskUser Q1+ only via elicitation/create.
            # request_permission handleQuestion drops every question after q0.
            client_caps: Dict[str, Any] = {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            }
            if getattr(self, "BACKEND_NAME", "") == "kimi":
                client_caps["elicitation"] = {"form": {}}
            init_request = {
                "protocolVersion": 1,
                "clientCapabilities": client_caps,
                "clientInfo": {
                    "name": self.CLIENT_NAME,
                    "version": self.CLIENT_VERSION,
                },
            }
            init_result = await self._send_acp("initialize", init_request) or {}
            negotiated = init_result.get("protocolVersion", 1)
            if negotiated != 1:
                self.log(f"agent negotiated protocolVersion={negotiated} "
                         f"(client requested 1); proceeding")
            self.negotiated_protocol_version = negotiated
            self.agent_capabilities = init_result.get("agentCapabilities", {}) or {}
            self._auth_methods = init_result.get("authMethods") or []
            self._init_meta = init_result.get("_meta") or {}

            await self.after_agent_initialize(init_result)

            mcp_servers = self._collect_mcp_servers()
            self.file_log(
                f"_collect_mcp_servers → {len(mcp_servers)} server(s): "
                f"{json.dumps(mcp_servers)[:600]}")

            can_load = bool(self.agent_capabilities.get("loadSession"))
            loaded = False
            if resume_id and not fork_session and can_load:
                loaded = await self._try_load_session(resume_id, mcp_servers)

            if not loaded:
                await self._create_session(
                    mcp_servers,
                    system_prompt=system_prompt,
                    additional_dirs=additional_dirs,
                    resume_failed=bool(resume_id and not fork_session),
                )
                if resume_id and not fork_session:
                    self._resume_fallback = True
                if fork_session and resume_id:
                    self.log(f"fork from {resume_id}: ACP has no fork; "
                             f"opened new session {self.session_id}")

            await self.apply_mode()
            await self.apply_model()

            send_result(req_id, {
                "status": "initialized",
                "ok": True,
                "backend": self.BACKEND_NAME,
                "session_id": self.session_id,
                "sessionId": self.session_id,
                "agent": init_result.get("agentInfo", {}),
                "agent_capabilities": self.agent_capabilities,
                "protocol_version": negotiated,
                "mcp_servers": [s.get("name") for s in mcp_servers],
                "agents": [],
                "streaming": True,
                "resumed": self._resumed,
                "resume_fallback": self._resume_fallback,
                "edit_mode": self.agent_mode,
                "modes": self._available_modes,
                "models": self._available_models,
                "effort": self.effort or None,
                "model": self.model,
            })
            if self._resume_fallback:
                send_notification("message", {
                    "type": "system",
                    "subtype": "init",
                    "data": {
                        "message": (
                            "Could not load prior ACP session; started fresh. "
                            "UI history is intact but the agent has no prior turns."
                        ),
                    },
                })
        except Exception as e:
            send_error(req_id, -32000,
                       f"{self.BACKEND_NAME} initialize failed: {e}")

    async def _try_load_session(self, resume_id: str,
                                 mcp_servers: list) -> bool:
        load_params: Dict[str, Any] = {
            "sessionId": resume_id,
            "cwd": self.cwd,
        }
        if mcp_servers:
            load_params["mcpServers"] = mcp_servers
        self._loading_session = True
        try:
            result = await self._send_acp("session/load", load_params) or {}
            self.session_id = (
                result.get("sessionId")
                or result.get("session_id")
                or resume_id
            )
            self._ingest_session_result(result)
            self._resumed = True
            self.log(f"session/load ok: {self.session_id}")
            return True
        except Exception as e:
            self.log(f"session/load failed for {resume_id!r}: {e}")
            return False
        finally:
            # Trailing session/update after the load RPC is still replay.
            # Clearing immediately lets those adopt as a live ⚙ and steal ◎.
            def _end_load():
                self._loading_session = False
            try:
                asyncio.get_running_loop().call_later(0.4, _end_load)
            except Exception:
                self._loading_session = False

    async def _create_session(self, mcp_servers: list, *,
                               system_prompt: str = "",
                               additional_dirs: Optional[list] = None,
                               resume_failed: bool = False) -> None:
        new_params: Dict[str, Any] = {"cwd": self.cwd}
        if mcp_servers:
            new_params["mcpServers"] = mcp_servers
        if additional_dirs:
            new_params["additionalDirectories"] = list(additional_dirs)
        meta = self.build_session_meta(
            system_prompt=system_prompt, resume_failed=resume_failed)
        if meta:
            new_params["_meta"] = meta

        new_result = await self._send_acp("session/new", new_params) or {}
        self.session_id = (
            new_result.get("sessionId") or new_result.get("session_id")
        )
        if not self.session_id:
            raise RuntimeError(
                f"session/new returned no sessionId: {new_result!r}")
        self._ingest_session_result(new_result)
        self._resumed = False

    def _ingest_session_result(self, result: dict) -> None:
        modes = result.get("modes") or {}
        if modes.get("availableModes"):
            self._available_modes = modes["availableModes"]
            self.file_log(
                f"session modes advertised: "
                f"{self._advertised_mode_ids()}")
        if modes.get("currentModeId"):
            # Prefer agent-reported mode, then re-resolve host permission intent
            cur = str(modes["currentModeId"])
            want = self.permission_mode_to_agent_mode(self.permission_mode)
            self.agent_mode = want or cur
            # Clamp to advertised list
            self.agent_mode = self._resolve_set_mode_id()
        models = result.get("models") or {}
        if models.get("availableModels"):
            self._available_models = models["availableModels"]
        if models.get("currentModelId") and not self.model:
            self.model = models["currentModelId"]

    def _collect_mcp_servers(self) -> list:
        """MCP servers for ACP session/new (Grok, Kimi, …).

        Always injects sublime (editor tools). Also injects **irr** (codebase
        search) when the `irr` binary is available — same stdio shape Claude
        uses from ~/.claude.json.

        Grok/Kimi McpServer untagged enum requires:
          {name, type:"stdio", command, args?, env: [{name,value}, ...] | []}
        A dict env {} is rejected with Invalid params.
        """
        servers: List[dict] = []
        bridge_dir = os.path.dirname(os.path.abspath(__file__))
        plugin_dir = os.path.dirname(bridge_dir)
        mcp_server_path = os.path.join(plugin_dir, "mcp", "server.py")
        if os.path.exists(mcp_server_path):
            args = [mcp_server_path]
            if self._view_id is not None:
                args.append(f"--view-id={self._view_id}")
            if self._mcp_enable_read_image:
                args.append("--enable-read-image")
            servers.append({
                "name": "sublime",
                "type": "stdio",
                "command": sys.executable,
                "args": args,
                "env": [],
            })
        else:
            self.file_log(f"MCP server missing: {mcp_server_path}")

        irr = self._collect_irr_mcp_server()
        if irr:
            servers.append(irr)
            self.file_log(
                f"MCP irr: {irr.get('command')} {' '.join(irr.get('args') or [])}")
        return servers

    def _collect_irr_mcp_server(self) -> Optional[dict]:
        """stdio MCP for irr (semantic/code search). None if unavailable.

        Binary: PATH `irr`, else ~/.nimble/bin/irr.
        Index db (--db), first hit wins:
          1) env SUBLIME_CLAUDE_IRR_DB / IRR_MCP_DB
          2) Claude ~/.claude.json mcpServers.irr --db
          3) cwd/.irr or parent project .irr
          4) ~/.irr-pil/db/pil-core (common multi-project index)
        Disable with env SUBLIME_CLAUDE_IRR_MCP=0.
        """
        if os.environ.get("SUBLIME_CLAUDE_IRR_MCP", "1").strip() in (
                "0", "false", "off", "no"):
            return None
        irr_bin = (
            shutil.which("irr")
            or os.path.expanduser("~/.nimble/bin/irr")
        )
        if not irr_bin or not os.path.isfile(irr_bin):
            if not shutil.which("irr"):
                self.file_log("MCP irr: binary not found — skip")
                return None
            irr_bin = shutil.which("irr")

        db = (
            (os.environ.get("SUBLIME_CLAUDE_IRR_DB") or "").strip()
            or (os.environ.get("IRR_MCP_DB") or "").strip()
            or self._irr_db_from_claude_json()
            or self._irr_db_near_cwd()
            or self._irr_db_default()
        )
        if not db:
            self.file_log("MCP irr: no index db found — skip")
            return None
        if not os.path.isdir(db):
            self.file_log(f"MCP irr: db not a directory {db!r} — skip")
            return None

        return {
            "name": "irr",
            "type": "stdio",
            "command": irr_bin,
            "args": ["mcp", "--db", db],
            "env": [],
        }

    @staticmethod
    def _irr_db_from_claude_json() -> str:
        """Parse --db from Claude Code user mcpServers.irr if present."""
        path = os.path.expanduser("~/.claude.json")
        if not os.path.isfile(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entry = (data.get("mcpServers") or {}).get("irr") or {}
            args = entry.get("args") or []
            if not isinstance(args, list):
                return ""
            for i, a in enumerate(args):
                if a == "--db" and i + 1 < len(args):
                    return str(args[i + 1]).strip()
                if isinstance(a, str) and a.startswith("--db="):
                    return a.split("=", 1)[1].strip()
        except Exception:
            pass
        return ""

    def _irr_db_near_cwd(self) -> str:
        """Walk cwd→parents for a .irr index directory."""
        start = (self.cwd or os.getcwd() or "").strip() or os.getcwd()
        try:
            cur = os.path.abspath(start)
        except Exception:
            return ""
        for _ in range(8):
            cand = os.path.join(cur, ".irr")
            if os.path.isdir(cand):
                return cand
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        return ""

    @staticmethod
    def _irr_db_default() -> str:
        cand = os.path.expanduser("~/.irr-pil/db/pil-core")
        return cand if os.path.isdir(cand) else ""

    def _is_agent_busy_error(self, e: BaseException) -> bool:
        msg = str(e).lower()
        return (
            "agent_busy" in msg
            or "another turn" in msg
            or "turn is active" in msg
            or "cannot launch a new turn" in msg
        )

    async def _cancel_agent_turn(
        self,
        *,
        reason: str = "",
        wait_s: float = 2.0,
        settle_s: float = 0.3,
        force_local: bool = True,
        orphan_ok: bool = True,
    ) -> None:
        """session/cancel + wait until local prompt future settles.

        Kimi rejects a new session/prompt while its agent-side turn is still
        active (``turn.agent_busy`` / "another turn is already in progress").

        Important: after host interrupt we often force the *local* prompt
        future done while the agent turn is still live (auto-continue, slow
        cancel). Next user message must still send session/cancel even when
        ``_prompt_fut`` is already None — otherwise Esc → type fails with
        agent_busy forever.
        """
        fut = self._prompt_fut
        active = fut is not None and not fut.done()
        has_query = self._query_req_id is not None
        if not active and not has_query and not orphan_ok:
            return
        self._prompt_cancelled = True
        self._cancel_in_flight = True
        if self.session_id is not None:
            try:
                await self._notify_acp(
                    "session/cancel", {"sessionId": self.session_id})
                self.file_log(
                    f"cancel_agent_turn: session/cancel ({reason})"
                    f"{'' if active or has_query else ' [orphan agent turn]'}")
            except Exception as e:
                self.log(f"session/cancel failed ({reason}): {e}")
        fut = self._prompt_fut
        if fut is not None and not fut.done():
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout=wait_s)
            except (asyncio.TimeoutError, Exception):
                pass
            if force_local and not fut.done():
                fut.set_result({"stopReason": "cancelled"})
                self.file_log(
                    f"cancel_agent_turn: forced local fut ({reason})")
        # Agent-side turn teardown lag (Kimi turn IDs / subagents)
        if settle_s > 0:
            try:
                await asyncio.sleep(settle_s)
            except Exception:
                pass

    async def handle_query(self, req_id: Optional[int],
                            params: dict) -> None:
        if self.session_id is None:
            send_error(req_id, -32000, "session not initialized")
            return
        # A new query must not overlap an agent turn (Kimi: turn.agent_busy).
        if self._query_req_id is not None and self._query_req_id != req_id:
            self.file_log(
                f"query: superseding in-flight req {self._query_req_id}")
            await self._cancel_agent_turn(
                reason="supersede", wait_s=2.0, settle_s=0.5)
        elif self._prompt_fut is not None and not self._prompt_fut.done():
            await self._cancel_agent_turn(
                reason="stale_prompt", wait_s=2.0, settle_s=0.5)
        elif self._cancel_in_flight:
            # Just interrupted — one more cancel for orphan agent turn + settle.
            # Do not kill the process; session/cancel only.
            await self._cancel_agent_turn(
                reason="post_interrupt", wait_s=2.0, settle_s=0.8,
                force_local=True, orphan_ok=True)
        prompt = params.get("prompt") or params.get("text") or ""
        images = params.get("images") or []
        if not isinstance(images, list):
            images = []
        prompt_blocks = self._build_prompt_blocks(prompt, images)
        self._query_req_id = req_id
        self._prompt_cancelled = False
        self._cancel_in_flight = False
        turn_t0 = time.time()
        try:
            result = None
            last_err: Optional[BaseException] = None
            # Busy retry: cancel leaves agent laggy; up to 3 attempts
            for attempt in range(3):
                try:
                    result = await self._send_prompt(prompt_blocks) or {}
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if (not self._is_agent_busy_error(e)
                            or self._prompt_cancelled):
                        raise
                    settle = 0.6 + attempt * 0.8
                    self.file_log(
                        f"query: agent_busy attempt {attempt + 1}/3 "
                        f"settle={settle:.1f}s: {e}")
                    await self._cancel_agent_turn(
                        reason=f"busy_retry_{attempt + 1}",
                        wait_s=2.0 + attempt,
                        settle_s=settle,
                        force_local=True,
                        orphan_ok=True,
                    )
                    self._prompt_cancelled = False
                    self._cancel_in_flight = False
            if last_err is not None and result is None:
                raise last_err
            result = result or {}
            stop_reason = result.get("stopReason", "end_turn")
            cancelled = (
                self._prompt_cancelled
                or stop_reason in ("cancelled", "canceled", "interrupted")
            )
            usage = self.usage_from_prompt_result(result)
            duration_ms = max(0, int((time.time() - turn_t0) * 1000))
            if usage:
                send_notification("message",
                                  {"type": "turn_usage", "usage": usage})
            send_notification("message", {
                "type": "result",
                "session_id": self.session_id or "",
                "duration_ms": duration_ms,
                "is_error": False,
                "num_turns": 1,
                "total_cost_usd": 0,
                "stop_reason": "interrupted" if cancelled else stop_reason,
                "usage": usage or {},
            })
            if cancelled:
                send_result(req_id, {"status": "interrupted",
                                     "stopReason": stop_reason})
            else:
                send_result(req_id, {
                    "status": "complete",
                    "stopReason": stop_reason})
        except Exception as e:
            if self._prompt_cancelled:
                duration_ms = max(0, int((time.time() - turn_t0) * 1000))
                send_notification("message", {
                    "type": "result",
                    "session_id": self.session_id or "",
                    "duration_ms": duration_ms,
                    "is_error": False,
                    "num_turns": 1,
                    "total_cost_usd": 0,
                    "stop_reason": "interrupted",
                })
                send_result(req_id, {"status": "interrupted"})
            else:
                send_error(req_id, -32000,
                           f"{self.BACKEND_NAME} query failed: {e}")
        finally:
            if self._query_req_id == req_id:
                self._query_req_id = None
            self._prompt_cancelled = False
            # Leave _cancel_in_flight set after interrupt so the NEXT query
            # still session/cancel+settles (Kimi agent_busy). Cleared when
            # that next query actually starts sending.
            self._prompt_fut = None
            self._prompt_acp_id = None

    def _prompt_caps(self) -> dict:
        return (self.agent_capabilities or {}).get("promptCapabilities") or {}

    def _prompt_supports_images(self) -> bool:
        return bool(self._prompt_caps().get("image"))

    def _prompt_supports_embedded(self) -> bool:
        return bool(self._prompt_caps().get("embeddedContext"))

    def _image_b64(self, img: dict) -> tuple:
        """Return (mime, base64_data) only — never put a filesystem path on the wire."""
        import base64 as _b64
        mime = (img.get("mime_type") or img.get("mimeType") or "image/png")
        data = img.get("data") or ""
        if data:
            return mime, data
        # Optional: load bytes from a local path the *plugin* already has, but
        # still only emit base64 (no uri/path in the ACP prompt).
        path = (img.get("path") or "").strip()
        if path and os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    data = _b64.b64encode(f.read()).decode("ascii")
                if not mime or mime == "image/png":
                    low = path.lower()
                    if low.endswith((".jpg", ".jpeg")):
                        mime = "image/jpeg"
                    elif low.endswith(".gif"):
                        mime = "image/gif"
                    elif low.endswith(".webp"):
                        mime = "image/webp"
                return mime, data
            except OSError as e:
                self.file_log(f"image load failed: {e}")
        return mime, ""

    def _build_prompt_blocks(self, prompt, images: list) -> list:
        """Build ACP ContentBlock[] — images as base64 only, never file paths.

        Grok will otherwise invent an assets/ path and call read_file on the
        PNG (text fs API) → FAILED. Vision is one multimodal image block.
        https://agentclientprotocol.com/protocol/v1/content
        """
        if isinstance(prompt, list):
            blocks = [b for b in prompt if isinstance(b, dict)]
            text = ""
        else:
            text = prompt if isinstance(prompt, str) else str(prompt or "")
            blocks = []

        if not images:
            if not blocks:
                blocks = [{"type": "text", "text": text}]
            elif text:
                blocks.append({"type": "text", "text": text})
            return blocks

        caps = self._prompt_caps()
        use_image_cap = bool(caps.get("image"))
        n_img = 0

        for img in images:
            if not isinstance(img, dict):
                continue
            mime, data = self._image_b64(img)
            if not data:
                self.file_log("query: skipped image with no base64 data")
                continue
            # Never set uri/path/resource_link for images.
            blocks.append({
                "type": "image",
                "mimeType": mime or "image/png",
                "data": data,
            })
            n_img += 1

        self.file_log(
            f"query: images→blocks image={n_img} (base64 only, no paths) "
            f"caps={caps} image_cap={use_image_cap}")

        if text or not any(b.get("type") == "text" for b in blocks):
            blocks.append({"type": "text", "text": text or ""})
        return blocks

    async def _send_prompt(self, prompt_blocks: list) -> Any:
        """session/prompt with a tracked future so interrupt can unblock us."""
        await self._spawn()
        assert self.proc is not None and self.proc.stdin is not None
        rid = self._acp_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        self._prompt_fut = fut
        self._prompt_acp_id = rid
        params = {"sessionId": self.session_id, "prompt": prompt_blocks}
        # Log without dumping multi-MB base64 image payloads
        def _summarize_block(b: dict) -> dict:
            t = b.get("type")
            if t == "text":
                return {"type": "text", "text": (b.get("text") or "")[:200]}
            if t == "image":
                return {
                    "type": "image",
                    "mimeType": b.get("mimeType"),
                    "data_len": len(b.get("data") or ""),
                    # never log/send path; uri must stay empty
                    "has_uri": bool(b.get("uri")),
                }
            if t == "resource":
                res = b.get("resource") or {}
                return {
                    "type": "resource",
                    "mimeType": res.get("mimeType"),
                    "blob_len": len(res.get("blob") or ""),
                    "uri": res.get("uri"),
                }
            if t == "resource_link":
                return {
                    "type": "resource_link",
                    "uri": b.get("uri"),
                    "name": b.get("name"),
                    "mimeType": b.get("mimeType"),
                }
            return {"type": t}
        log_params = {
            "sessionId": self.session_id,
            "prompt": [
                _summarize_block(b)
                for b in (prompt_blocks or [])
                if isinstance(b, dict)
            ],
        }
        line = json.dumps({
            "jsonrpc": "2.0", "id": rid,
            "method": "session/prompt", "params": params,
        })
        self.file_log(
            f"→ acp session/prompt (id={rid}): "
            f"{json.dumps(log_params)[:800]} (wire_len={len(line)})")
        async with self._get_acp_write_lock():
            self.proc.stdin.write((line + "\n").encode())
            await self.proc.stdin.drain()
        exit_task = None
        try:
            if self.proc is not None:
                exit_task = asyncio.create_task(self.proc.wait())
                done, _pend = await asyncio.wait(
                    {fut, exit_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if fut not in done:
                    rc = self.proc.returncode
                    self.file_log(
                        f"agent exited during session/prompt id={rid} "
                        f"returncode={rc}")
                    raise RuntimeError(
                        f"agent process exited during prompt (returncode={rc})")
            result = await fut
            try:
                self.file_log(
                    f"← acp session/prompt (id={rid}) result: "
                    f"{json.dumps(result)[:800]}")
            except Exception:
                self.file_log(
                    f"← acp session/prompt (id={rid}) result: {result!r}")
            return result
        finally:
            if exit_task is not None and not exit_task.done():
                exit_task.cancel()
            self.pending.pop(rid, None)
            if self._prompt_fut is fut:
                self._prompt_fut = None
            if self._prompt_acp_id == rid:
                self._prompt_acp_id = None

    async def handle_interrupt(self, req_id: Optional[int],
                                params: dict) -> None:
        """Cancel the in-flight ACP turn.

        Grok expects session/cancel as a JSON-RPC *notification* (no id).
        Sending it as a request returns Method not found and never unblocks
        the prompt. After notify, session/prompt resolves with
        stopReason=cancelled — handle_query maps that to interrupted.

        Idempotent: extra Esc presses must NOT re-send session/cancel after
        the turn already ended (Grok logs ChatStateActor dead / channel_dropped).

        Kimi: cancel alone is not enough if we force the local future too
        early — agent keeps turn.agent_busy. Wait longer before force; next
        query also re-settles via _cancel_agent_turn.
        """
        fut = self._prompt_fut
        active = fut is not None and not fut.done()
        has_query = self._query_req_id is not None
        # Idle — nothing to cancel (don't poke Grok).
        if not active and not has_query:
            self.file_log("interrupt: idle (no in-flight prompt)")
            send_result(req_id, {"status": "interrupted"})
            return
        # Cancel already in progress / done for this turn — no second notify.
        if self._cancel_in_flight and not active:
            self.file_log("interrupt: already cancelled; skip session/cancel")
            send_result(req_id, {"status": "interrupted"})
            return

        # Kill client-side terminals so terminal/wait_for_exit unblocks.
        for tid in list(self._terminals):
            try:
                await self._terminal_close(tid)
            except Exception:
                pass

        # Cancel + wait (longer than old 0.35s force — Kimi turn teardown).
        await self._cancel_agent_turn(
            reason="interrupt", wait_s=1.5, settle_s=0.2, force_local=True)

        # Unblock any permission waiters so they don't keep the turn alive.
        for pid, pfut in list(self.pending_permissions.items()):
            if pfut and not pfut.done():
                pfut.set_result({"kind": "denied-interactively-by-user"})
            self.pending_permissions.pop(pid, None)
        # Unblock ask_user waiters (None → outcome "cancelled").
        for qid, qfut in list(self.pending_questions.items()):
            if qfut and not qfut.done():
                qfut.set_result(None)
            self.pending_questions.pop(qid, None)
        # Unblock plan approval (None → rejected / stay in plan).
        for pid, pfut in list(self.pending_plan_approvals.items()):
            if pfut and not pfut.done():
                pfut.set_result(None)
            self.pending_plan_approvals.pop(pid, None)
        send_result(req_id, {"status": "interrupted"})

    async def handle_set_model(self, req_id: Optional[int],
                                params: dict) -> None:
        self.model = self.normalize_model(params.get("model"))
        if "effort" in params:
            self.effort = self.normalize_effort(params.get("effort"))
        try:
            await self.apply_model()
            send_result(req_id, {
                "ok": True,
                "model": self.model,
                "effort": self.effort or None,
            })
        except Exception as e:
            send_error(req_id, -32000, f"set_model failed: {e}")

    async def handle_set_permission_mode(self, req_id: Optional[int],
                                          params: dict) -> None:
        mode = params.get("mode") or "default"
        self.permission_mode = mode
        self.agent_mode = self.permission_mode_to_agent_mode(mode)
        # Refresh patterns in case user managed auto-allows while idle.
        self._reload_auto_allow_patterns()
        try:
            await self.apply_mode()
            send_result(req_id, {
                "ok": True,
                "mode": mode,
                "edit_mode": self.agent_mode,
            })
        except Exception as e:
            send_error(req_id, -32000, f"set_permission_mode failed: {e}")

    async def handle_plan_response(self, req_id: Optional[int],
                                    params: dict) -> None:
        """Resolve shared plan future, then map mode for the ACP agent."""
        from base import resolve_plan_response
        payload = resolve_plan_response(self, params)
        approved = (
            payload.get("approved") if isinstance(payload, dict) else payload
        )
        if self.session_id:
            try:
                if approved is True:
                    self.agent_mode = (
                        self.permission_mode_to_agent_mode("acceptEdits")
                        or self.agent_mode
                        or "auto"
                    )
                    self._in_plan_mode = False
                else:
                    self.agent_mode = (
                        self.permission_mode_to_agent_mode("plan") or "plan"
                    )
                    self._in_plan_mode = True
                await self.apply_mode()
            except Exception as e:
                self.log(f"plan_response mode switch failed: {e}")
        send_result(req_id, {"ok": True, "approved": approved})

    # ── Message-round rewind (Grok x.ai/rewind/* + disk truncate) ─────

    def _grok_session_dir(self) -> Optional[str]:
        """Locate ~/.grok/sessions/<encoded_cwd>/<session_id>/."""
        sid = self.session_id
        if not sid:
            return None
        root = os.path.expanduser("~/.grok/sessions")
        if not os.path.isdir(root):
            return None
        # Preferred: cwd-encoded path used by Grok Build.
        if self.cwd:
            enc = self.cwd.replace("/", "%2F")
            cand = os.path.join(root, enc, sid)
            if os.path.isdir(cand):
                return cand
        # Fallback: scan for session id directory.
        try:
            for name in os.listdir(root):
                cand = os.path.join(root, name, sid)
                if os.path.isdir(cand):
                    return cand
        except OSError:
            pass
        return None

    async def handle_rewind_points(self, req_id: Optional[int],
                                    params: dict) -> None:
        """List rewind points for the current Grok ACP session."""
        if not self.session_id:
            send_error(req_id, -32000, "session not initialized")
            return
        try:
            result = await self._send_acp(
                "_x.ai/rewind/points",
                {"sessionId": self.session_id},
            ) or {}
            points = result.get("rewind_points") or result.get("points") or []
            if not isinstance(points, list):
                points = []
            # Normalize for plugin UI.
            out = []
            for p in points:
                if not isinstance(p, dict):
                    continue
                idx = p.get("prompt_index")
                if idx is None:
                    idx = p.get("promptIndex")
                try:
                    idx = int(idx)
                except (TypeError, ValueError):
                    continue
                preview = (
                    p.get("prompt_preview")
                    or p.get("promptPreview")
                    or p.get("prompt_text")
                    or ""
                )
                out.append({
                    "prompt_index": idx,
                    "prompt_preview": str(preview),
                    "created_at": p.get("created_at") or p.get("createdAt") or "",
                    "has_file_changes": bool(
                        p.get("has_file_changes")
                        or p.get("hasFileChanges")
                        or (p.get("num_file_snapshots") or 0)
                    ),
                    "num_file_snapshots": int(
                        p.get("num_file_snapshots")
                        or p.get("numFileSnapshots")
                        or 0
                    ),
                })
            out.sort(key=lambda x: x["prompt_index"])
            send_result(req_id, {
                "ok": True,
                "session_id": self.session_id,
                "points": out,
                "leaf_response_id": (
                    result.get("leaf_response_id")
                    or result.get("leafResponseId")
                ),
            })
        except Exception as e:
            send_error(req_id, -32000, f"rewind_points failed: {e}")

    async def handle_rewind_execute(self, req_id: Optional[int],
                                     params: dict) -> None:
        """Conversation-only rewind to a prompt_index.

        Never restores project file snapshots. Truncates Grok *session*
        transcript files (chat_history / rewind_points / updates) so the next
        session/load forgets later turns. ACP ``_x.ai/rewind/execute`` is
        best-effort (often success:false with no error) — session truncate is
        the reliable path.
        """
        if not self.session_id:
            send_error(req_id, -32000, "session not initialized")
            return
        try:
            target = params.get("prompt_index")
            if target is None:
                target = params.get("target_prompt_index")
            target = int(target)
        except (TypeError, ValueError):
            send_error(req_id, -32602, "prompt_index required (int)")
            return
        # Product scope: conversation only — ignore mode/restore_files from host.
        mode = "conversation_only"

        disk = await asyncio.to_thread(self._client_rewind_conversation, target)
        acp_result: dict = {}
        try:
            acp_params = {
                "sessionId": self.session_id,
                "target_prompt_index": target,
                "mode": mode,
            }
            leaf = params.get("expected_leaf_response_id") or params.get(
                "leaf_response_id")
            if leaf:
                acp_params["expected_leaf_response_id"] = leaf
            # Grok ACP execute often hangs or returns success:false; never
            # block the bridge forever — session truncate is the real work.
            acp_result = await asyncio.wait_for(
                self._send_acp("_x.ai/rewind/execute", acp_params),
                timeout=12.0,
            ) or {}
            self.file_log(
                f"rewind/execute target={target} mode={mode} "
                f"acp={str(acp_result)[:300]}")
        except asyncio.TimeoutError:
            self.file_log(
                f"rewind/execute ACP TIMEOUT (session still cut) target={target}")
            acp_result = {"success": False, "error": "acp timeout"}
        except Exception as e:
            self.file_log(f"rewind/execute ACP error (session still cut): {e}")
            acp_result = {"success": False, "error": str(e)}

        draft = (disk or {}).get("draft_prompt") or ""
        if not draft and isinstance(acp_result, dict):
            draft = acp_result.get("prompt_text") or ""
        send_result(req_id, {
            "ok": True,
            "prompt_index": target,
            "draft_prompt": draft,
            "mode": mode,
            "disk": disk or {},
            "acp": acp_result,
            "session_id": self.session_id,
        })

    @staticmethod
    def _chat_row_text(o: dict) -> str:
        content = o.get("content") or ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text") or "")
            return "".join(parts)
        return ""

    @staticmethod
    def _user_query_body(text: str) -> str:
        if "<user_query>" not in text:
            return ""
        try:
            body = text.split("<user_query>", 1)[1]
            return body.split("</user_query>", 1)[0].strip()
        except IndexError:
            return text.strip()

    def _client_rewind_conversation(self, target_prompt_index: int) -> dict:
        """Truncate Grok *session* history to before target_prompt_index.

        Conversation only — never writes project files / file_snapshots.
        Cut key is real ``prompt_index`` (not Nth user_query). Updates are
        stream-cut at the first line with ``_meta.promptIndex >= target``.
        """
        sdir = self._grok_session_dir()
        if not sdir:
            return {"error": "session dir not found", "draft_prompt": ""}

        chat_path = os.path.join(sdir, "chat_history.jsonl")
        rp_path = os.path.join(sdir, "rewind_points.jsonl")
        up_path = os.path.join(sdir, "updates.jsonl")
        draft = ""
        cut_at = None
        kept_rp = 0
        dropped_rp = 0
        kept_up = 0
        dropped_up = 0

        # rewind_points: keep prompt_index < target; draft from matching row
        if os.path.isfile(rp_path):
            kept_lines = []
            with open(rp_path, "r", encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        o = json.loads(raw)
                    except json.JSONDecodeError:
                        kept_lines.append(raw)
                        continue
                    try:
                        idx = int(o.get("prompt_index"))
                    except (TypeError, ValueError):
                        kept_lines.append(raw)
                        continue
                    if idx < target_prompt_index:
                        kept_lines.append(raw)
                        kept_rp += 1
                    else:
                        dropped_rp += 1
                        if idx == target_prompt_index:
                            draft = (
                                o.get("prompt_preview")
                                or o.get("prompt_text")
                                or draft
                            )
            with open(rp_path, "w", encoding="utf-8") as f:
                for line in kept_lines:
                    f.write(line + "\n")

        # chat_history: cut at first row whose prompt_index >= target
        if os.path.isfile(chat_path):
            lines = open(chat_path, "r", encoding="utf-8").read().splitlines()
            for i, line in enumerate(lines):
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    pi = o.get("prompt_index")
                    if pi is None:
                        pi = o.get("promptIndex")
                    pi = int(pi) if pi is not None else None
                except (TypeError, ValueError):
                    pi = None
                if pi is None or pi < target_prompt_index:
                    continue
                cut_at = i
                text = self._chat_row_text(o)
                if not draft:
                    draft = self._user_query_body(text) or draft
                break
            # Fallback: no prompt_index fields (rare / heavily compacted) —
            # treat target as Nth real <user_query> only if it fits.
            if cut_at is None:
                user_turns = 0
                for i, line in enumerate(lines):
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if o.get("type") != "user" or o.get("synthetic_reason"):
                        continue
                    text = self._chat_row_text(o)
                    if "<user_query>" not in text:
                        continue
                    if user_turns == target_prompt_index:
                        cut_at = i
                        if not draft:
                            draft = self._user_query_body(text)
                        break
                    user_turns += 1
            if cut_at is not None:
                with open(chat_path, "w", encoding="utf-8") as f:
                    for line in lines[:cut_at]:
                        f.write(line + "\n")

        # updates.jsonl: stream-cut at first _meta.promptIndex >= target
        # (later tool_call rows lack promptIndex and must still drop).
        if os.path.isfile(up_path):
            kept_u: list = []
            cutting = False
            with open(up_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    raw = line.rstrip("\n")
                    if cutting:
                        dropped_up += 1
                        continue
                    pi = self._updates_line_prompt_index(raw)
                    if pi is not None and pi >= target_prompt_index:
                        cutting = True
                        dropped_up += 1
                        continue
                    kept_u.append(raw)
                    kept_up += 1
            with open(up_path, "w", encoding="utf-8") as f:
                for line in kept_u:
                    f.write(line + "\n")

        self.file_log(
            f"client rewind conversation target={target_prompt_index} "
            f"cut_at={cut_at} draft_len={len(draft or '')} "
            f"rp_keep={kept_rp} rp_drop={dropped_rp} "
            f"up_keep={kept_up} up_drop={dropped_up} dir={sdir}")
        return {
            "session_dir": sdir,
            "draft_prompt": draft or "",
            "cut_at": cut_at,
            "files_restored": [],  # conversation only — never project files
            "kept_rp": kept_rp,
            "dropped_rp": dropped_rp,
            "kept_updates": kept_up,
            "dropped_updates": dropped_up,
        }

    @staticmethod
    def _updates_line_prompt_index(line: str):
        """Return _meta.promptIndex from an updates.jsonl line, or None."""
        if "promptIndex" not in line and "prompt_index" not in line:
            return None
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            return None
        u = (o.get("params") or {}).get("update") or {}
        meta = u.get("_meta") or {}
        pi = meta.get("promptIndex")
        if pi is None:
            pi = meta.get("prompt_index")
        if pi is None:
            pi = u.get("promptIndex")
        try:
            return int(pi) if pi is not None else None
        except (TypeError, ValueError):
            return None

    async def handle_shutdown(self, req_id: Optional[int],
                               params: dict) -> None:
        self.running = False
        for tid in list(self._terminals):
            try:
                await self._terminal_close(tid)
            except Exception:
                pass
        if self.proc is not None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
        send_result(req_id, {"ok": True})

    # ── ACP agent→client REQUEST handling ──────────────────────────────

    _ACP_REQUEST_HANDLERS = {
        "session/request_permission": "_acp_request_permission",
        "elicitation/create": "_acp_elicitation_create",
        # Grok Build asks the user via an xAI extension (not a plain tool result).
        "_x.ai/ask_user_question": "_acp_ask_user_question",
        "x.ai/ask_user_question": "_acp_ask_user_question",
        "_x.ai/exit_plan_mode": "_acp_exit_plan_mode",
        "x.ai/exit_plan_mode": "_acp_exit_plan_mode",
        # Grok scheduler fire → client injects the prompt as a new turn.
        "x.ai/scheduled_task_inject_prompt": "_acp_scheduled_task_inject",
        "_x.ai/scheduled_task_inject_prompt": "_acp_scheduled_task_inject",
        "fs/read_text_file": "_acp_fs_read",
        "fs/write_text_file": "_acp_fs_write",
        "terminal/create": "_acp_terminal_create",
        "terminal/output": "_acp_terminal_output",
        "terminal/wait_for_exit": "_acp_terminal_wait",
        "terminal/kill": "_acp_terminal_kill",
        "terminal/release": "_acp_terminal_release",
    }

    async def _dispatch_acp_request(self, rid: int, method: Optional[str],
                                     params: dict) -> None:
        handler_name = self._ACP_REQUEST_HANDLERS.get(method or "")
        if not handler_name:
            await self._send_acp_response(rid, error={
                "code": -32601,
                "message": f"Method not supported: {method}"})
            return
        try:
            result = await getattr(self, handler_name)(params)
            await self._send_acp_response(rid, result=result or {})
        except FileNotFoundError as e:
            await self._send_acp_response(rid, error={
                "code": -32000, "message": str(e)})
        except Exception as e:
            self.log(f"ACP {method} error: {e}")
            await self._send_acp_response(rid, error={
                "code": -32000, "message": str(e)})

    async def _send_acp_response(self, rid: int, *, result: Any = None,
                                  error: Optional[dict] = None) -> None:
        if self.proc is None or self.proc.stdin is None:
            return
        env: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            env["error"] = error
        else:
            env["result"] = result if result is not None else {}
        line = json.dumps(env) + "\n"
        # Keep response logs short — full fs/read payloads are huge.
        if error is not None:
            self.file_log(f"→ acp RESP id={rid} error: {json.dumps(error)[:300]}")
        else:
            preview = json.dumps(env.get("result"))[:200]
            self.file_log(f"→ acp RESP id={rid} ok: {preview}")
        async with self._get_acp_write_lock():
            if self.proc is None or self.proc.stdin is None:
                return
            try:
                self.proc.stdin.write(line.encode())
                await self.proc.stdin.drain()
            except Exception as e:
                self.file_log(f"→ acp RESP id={rid} write failed: {e}")

    def _reload_auto_allow_patterns(self) -> None:
        """Load autoAllowedMcpTools (+ permissions.allow) from project settings."""
        patterns: List[str] = []
        try:
            from settings import load_project_settings  # type: ignore
            settings = load_project_settings(self.cwd) or {}
            raw = settings.get("autoAllowedMcpTools") or []
            if isinstance(raw, list):
                patterns = [str(p) for p in raw if p]
        except Exception as e:
            self.log(f"load auto-allow patterns failed: {e}")
        self._auto_allow_patterns = patterns

    def _parse_permission_pattern(self, pattern: str):
        """Parse 'Tool' or 'Tool(specifier)' → (tool_name, specifier|None)."""
        if "(" in pattern and pattern.endswith(")"):
            i = pattern.index("(")
            return pattern[:i], pattern[i + 1:-1]
        return pattern, None

    def _match_permission_pattern(self, tool_name: str, tool_input: dict,
                                   pattern: str) -> bool:
        """Match tool use against an auto-allow pattern (Claude/plugin shape)."""
        import fnmatch
        parsed_tool, specifier = self._parse_permission_pattern(pattern)
        if not fnmatch.fnmatch(tool_name, parsed_tool):
            return False
        if specifier is None:
            return True
        if tool_name == "Bash":
            command = tool_input.get("command") or ""
            if not command:
                return False
            if specifier.endswith(":*"):
                prefix = specifier[:-2]
                return command.strip().startswith(prefix) or any(
                    w.startswith(prefix) for w in command.replace("|", " ")
                    .replace("&&", " ").split()
                )
            if any(c in specifier for c in "*?["):
                return fnmatch.fnmatch(command, specifier)
            return command == specifier or specifier in command.split()
        if tool_name in ("Read", "Write", "Edit"):
            file_path = tool_input.get("file_path") or ""
            if not file_path:
                return False
            if specifier.endswith("/"):
                return file_path.startswith(specifier) or (
                    os.path.dirname(file_path) + "/" == specifier)
            if any(c in specifier for c in "*?["):
                return fnmatch.fnmatch(file_path, specifier)
            if specifier.endswith(":*"):
                return file_path.startswith(specifier[:-2])
            return file_path == specifier or (
                os.path.dirname(file_path) == os.path.dirname(specifier.rstrip("/")))
        if tool_name == "Skill":
            return (tool_input.get("skill") or "") == specifier
        match_value = (
            tool_input.get("pattern")
            or tool_input.get("url")
            or tool_input.get("command")
            or tool_input.get("path")
            or tool_input.get("query")
            or ""
        )
        if not match_value:
            return False
        if specifier.endswith(":*"):
            return str(match_value).startswith(specifier[:-2])
        if any(c in specifier for c in "*?["):
            return fnmatch.fnmatch(str(match_value), specifier)
        return str(match_value) == specifier

    def _tool_is_readonly(self, tool_call: dict, tool_name: str) -> bool:
        """True for research/read tools (Grok marks these via kind / _meta)."""
        if tool_name in self.PLAN_READONLY_TOOLS or tool_name in (
                "WebSearch", "WebFetch", "search_tool", "Grep", "Glob", "Read"):
            return True
        meta = (tool_call.get("_meta") or {}).get("x.ai/tool") or {}
        if meta.get("read_only") is True:
            return True
        kind = (
            (tool_call.get("kind") or "")
            or (meta.get("kind") or "")
            or ""
        ).lower()
        if kind in self.READONLY_KINDS:
            return True
        # MCP tools often look like server__tool; treat common search names as RO.
        low = tool_name.lower()
        if any(s in low for s in ("search", "grep", "fetch", "read", "list")):
            if not any(s in low for s in (
                    "write", "edit", "delete", "run", "exec", "bash", "shell")):
                return True
        return False

    def _is_plan_exit_tool(self, tool_name: str, tool_call: Optional[dict] = None) -> bool:
        """True for ExitPlanMode / submit_plan (any agent naming)."""
        if not tool_name:
            tool_name = ""
        if tool_name in self.PLAN_EXIT_TOOLS or tool_name == "ExitPlanMode":
            return True
        low = (tool_name or "").lower().replace("_", "").replace("-", "")
        if low in ("exitplanmode", "submitplan"):
            return True
        if "exitplan" in low or low.endswith("planmode") and "exit" in low:
            return True
        # Title / rawInput hints (Kimi sometimes only sets title)
        tc = tool_call or {}
        title = (tc.get("title") or "")
        if isinstance(title, str):
            tlow = title.lower().replace(" ", "").replace("_", "")
            if "exitplan" in tlow or tlow in ("submitplan", "exitplanmode"):
                return True
        raw = tc.get("rawInput") or {}
        if isinstance(raw, dict):
            for k in ("name", "tool", "variant", "toolName"):
                v = raw.get(k)
                if isinstance(v, str) and self._is_plan_exit_tool(v, {}):
                    return True
        return False

    def _permission_decision(self, tool_name: str,
                              tool_input: dict,
                              tool_call: Optional[dict] = None) -> Optional[bool]:
        """Apply plugin permission rules.

        Returns True (auto-allow), False (auto-deny), or None (ask user).
        """
        mode = self.permission_mode or "default"
        tool_call = tool_call or {}

        # Plan exit always needs the plan approval UI — never auto-allow,
        # even under bypassPermissions (Claude bridge parity).
        if self._is_plan_exit_tool(tool_name, tool_call):
            return None

        # AskUserQuestion is a choice UI, not a Y/N allow. Auto-allowing it
        # always selects the first option (Option A / q0_opt_0).
        if tool_name in (
                "AskUserQuestion", "ask_user", "ask_user_question", "AskUser"):
            return None

        # Full bypass — same as Claude bypassPermissions / --always-approve.
        if mode == "bypassPermissions":
            return True

        # Built-in Sublime MCP tools are always trusted (Claude bridge parity).
        if tool_name.startswith("mcp__sublime__"):
            return True

        # Project / user auto-allow patterns (Always button + permissions.allow).
        # Never pattern-auto-allow plan exit (handled above).
        for pattern in self._auto_allow_patterns:
            if self._match_permission_pattern(tool_name, tool_input, pattern):
                self.file_log(
                    f"permission auto-allow {tool_name} via pattern {pattern!r}")
                return True

        # acceptEdits: auto-approve file edits + read-only research (search/grep
        # /web). Bash and mutating MCP still prompt unless allowlisted.
        if mode in ("acceptEdits", "auto"):
            if tool_name in self.ACCEPT_EDITS_TOOLS:
                return True
            if tool_name in self.allowed_tools:
                return True
            if self._tool_is_readonly(tool_call, tool_name):
                return True
            return None

        # plan: read-only tools ok; mutating tools denied without UI spam.
        if mode == "plan":
            if tool_name in self.PLAN_READONLY_TOOLS:
                return True
            if self._tool_is_readonly(tool_call, tool_name):
                return True
            if tool_name in ("Write", "Edit", "Bash", "NotebookEdit"):
                self.file_log(
                    f"permission auto-deny {tool_name} in plan mode")
                return False
            return None

        # default: prompt for everything (except patterns / sublime above).
        # Bare allowed_tools in default mode still prompt — matches session.py
        # which clears allowed_tools when mode is default.
        return None

    async def _acp_request_permission(self, params: dict) -> dict:
        tool_call = params.get("toolCall") or {}
        options = params.get("options") or []
        # kimi-cli OSS optionIds: approve | approve_for_session | reject
        # (session.py _handle_approval_request). Also kind-based allow_*.
        allow_once = next((o.get("optionId") for o in options
                           if isinstance(o, dict)
                           and (o.get("kind") == "allow_once"
                                or o.get("optionId") in ("approve", "approve_once"))), None)
        allow_always = next((o.get("optionId") for o in options
                             if isinstance(o, dict)
                             and (o.get("kind") == "allow_always"
                                  or o.get("optionId") in (
                                      "approve_for_session", "approve_always"))), None)
        reject_once = next((o.get("optionId") for o in options
                            if isinstance(o, dict)
                            and (o.get("kind") == "reject_once"
                                 or o.get("optionId") in ("reject", "reject_once"))), None)
        reject_always = next((o.get("optionId") for o in options
                              if isinstance(o, dict)
                              and o.get("kind") == "reject_always"), None)
        allow_id = allow_once or allow_always or next(
            (o.get("optionId") for o in options
             if isinstance(o, dict)
             and (o.get("kind") or "").startswith("allow")), None)
        reject_id = reject_once or reject_always or next(
            (o.get("optionId") for o in options
             if isinstance(o, dict)
             and (o.get("kind") or "").startswith("reject")), None)
        # AskUserQuestion is q0_opt_* + q0_skip — do not cancel before that
        # path just because generic approve/reject ids are missing.

        # Prefer Grok's embedded tool name from _meta when present.
        meta_tool = ((tool_call.get("_meta") or {}).get("x.ai/tool") or {})
        if meta_tool.get("name") and not (tool_call.get("rawInput") or {}).get("tool"):
            # Inject so _normalize_tool_name can map it.
            raw = dict(tool_call.get("rawInput") or {})
            raw.setdefault("name", meta_tool["name"])
            tool_call = dict(tool_call)
            tool_call["rawInput"] = raw
            if meta_tool.get("kind") and not tool_call.get("kind"):
                tool_call["kind"] = meta_tool["kind"]

        tool_name = self._normalize_tool_name(tool_call)
        tool_input = self._tool_input_from_update(tool_call, tool_name)
        # Recover rawInput.questions from earlier tool_call_update (permission
        # payload often only has title + truncated content).
        tid = tool_call.get("toolCallId")
        if tid and isinstance(self._tool_inputs_by_id.get(tid), dict):
            prev = self._tool_inputs_by_id[tid]
            for k, v in prev.items():
                tool_input.setdefault(k, v)

        # EnterPlanMode: notify host, allow (Claude bridge parity).
        if tool_name in ("EnterPlanMode", "enter_plan_mode", "EnterPlan"):
            send_notification("plan_mode_enter", {})
            self._in_plan_mode = True
            return {"outcome": {
                "outcome": "selected", "optionId": allow_id,
            }}

        # ExitPlanMode / submit plan: full plan approval UI (never generic Y/N
        # and never auto-allow). Kimi/Grok ACP both hit this path.
        if self._is_plan_exit_tool(tool_name, tool_call):
            self.file_log(
                f"permission → plan approval UI for {tool_name!r} "
                f"input_keys={list(tool_input.keys())}")
            try:
                plan_result = await self._handle_acp_exit_plan_permission(
                    tool_input, tool_call)
            except Exception as e:
                self.file_log(f"exit plan permission UI failed: {e}")
                plan_result = False
            if plan_result is True:
                return {"outcome": {
                    "outcome": "selected", "optionId": allow_id,
                }}
            # Reject or cancel
            return {"outcome": {
                "outcome": "selected", "optionId": reject_id,
            }}

        # Kimi AskUserQuestion: choices are permission options (q0_opt_0…).
        # Never auto-allow — that always picks Option A (first allow_once).
        if self._is_ask_user_permission(tool_name, options, tool_call):
            self.file_log(
                f"permission → question UI for {tool_name!r} "
                f"n_options={len(options)}")
            try:
                return await self._handle_acp_ask_user_permission(
                    tool_call, options, tool_input)
            except Exception as e:
                self.file_log(f"ask_user permission UI failed: {e}")
                return {"outcome": {"outcome": "cancelled"}}

        if allow_id is None or reject_id is None:
            self.file_log(
                f"permission request missing options: {json.dumps(options)[:300]}")
            return {"outcome": {"outcome": "cancelled"}}

        # Always re-read project auto-allows — UI "Always" persists them live.
        self._reload_auto_allow_patterns()
        decision = self._permission_decision(tool_name, tool_input, tool_call)
        self.file_log(
            f"permission {tool_name} mode={self.permission_mode} "
            f"decision={decision!r} input_keys={list(tool_input.keys())}")
        if decision is True:
            return {"outcome": {
                "outcome": "selected", "optionId": allow_id,
            }}
        if decision is False:
            return {"outcome": {
                "outcome": "selected", "optionId": reject_id,
            }}

        # Ask Sublime (Y/N/S/A — Always patterns persist via output.py).
        self.permission_id += 1
        pid = self.permission_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_permissions[pid] = fut
        send_notification("permission_request", {
            "id": pid,
            "tool": tool_name,
            "input": tool_input,
        })
        try:
            answer = await fut
        except Exception:
            return {"outcome": {"outcome": "cancelled"}}
        allowed = isinstance(answer, dict) and answer.get("kind") == "approved"
        always = isinstance(answer, dict) and bool(answer.get("always"))
        if allowed:
            # Prefer allow_always when user chose Always and the agent offered it.
            oid = (allow_always if always and allow_always else allow_id) or allow_id
            return {"outcome": {"outcome": "selected", "optionId": oid}}
        oid = (reject_always if always and reject_always else reject_id) or reject_id
        return {"outcome": {"outcome": "selected", "optionId": oid}}

    def _is_ask_user_permission(
            self, tool_name: str, options: list,
            tool_call: Optional[dict] = None) -> bool:
        """Kimi encodes AskUserQuestion choices as permission optionIds."""
        if tool_name in (
                "AskUserQuestion", "ask_user", "ask_user_question",
                "AskUser"):
            return True
        title = ((tool_call or {}).get("title") or "")
        if isinstance(title, str) and "question" in title.lower():
            return True
        for o in options or []:
            if not isinstance(o, dict):
                continue
            oid = str(o.get("optionId") or "")
            # q0_opt_0 / q1_opt_2 / q0_skip
            if re.match(r"^q\d+_opt_\d+$", oid) or re.match(
                    r"^q\d+_skip$", oid):
                return True
        return False

    async def _acp_elicitation_create(self, params: dict) -> dict:
        """kimi-code AskUser: full question set via elicitation/create form."""
        questions, keys = self._questions_from_elicitation(params)
        if not questions:
            self.file_log("elicitation/create: empty schema; cancel")
            return {"action": "cancel"}
        self.question_id += 1
        qid = self.question_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_questions[qid] = fut
        send_notification("question_request", {
            "id": qid,
            "questions": questions,
        })
        try:
            answers = await fut
        except Exception as e:
            self.file_log(f"elicitation/create cancelled: {e}")
            answers = None
        finally:
            self.pending_questions.pop(qid, None)
        if answers is None:
            return {"action": "cancel"}
        content = self._elicitation_content_from_answers(
            questions, keys, answers)
        if not content:
            return {"action": "cancel"}
        self.file_log(
            f"elicitation/create accept keys={list(content.keys())}")
        return {"action": "accept", "content": content}

    @staticmethod
    def _questions_from_elicitation(params: dict):
        """Map kimi requestedSchema (q0/q1…) into plugin question rows."""
        schema = (params or {}).get("requestedSchema") or {}
        props = schema.get("properties") or {}
        if not isinstance(props, dict) or not props:
            return [], []
        required = schema.get("required") or []
        keys = [k for k in required if k in props]
        for k in sorted(props.keys()):
            if k not in keys:
                keys.append(k)
        msg_lines = [
            ln for ln in str((params or {}).get("message") or "").split("\n")
            if ln.strip()
        ]
        questions = []
        for i, key in enumerate(keys):
            prop = props.get(key) or {}
            if not isinstance(prop, dict):
                continue
            if prop.get("type") == "array":
                items = prop.get("items") or {}
                enums = items.get("anyOf") or items.get("oneOf") or []
                multi = True
            else:
                enums = prop.get("oneOf") or prop.get("anyOf") or []
                multi = False
            options = []
            for e in enums:
                if not isinstance(e, dict):
                    continue
                label = e.get("const") or e.get("title") or ""
                if label == "" and e.get("const") is not None:
                    label = str(e.get("const"))
                options.append({
                    "label": str(label),
                    "description": str(e.get("description") or ""),
                })
            qtext = msg_lines[i] if i < len(msg_lines) else ""
            if not qtext:
                qtext = str(prop.get("title") or "Question?")
            questions.append({
                "question": qtext,
                "header": str(prop.get("title") or ""),
                "options": options,
                "multiSelect": multi,
            })
        return questions, keys

    @staticmethod
    def _elicitation_content_from_answers(
            questions: list, keys: list, answers: dict) -> dict:
        """Plugin answers → {q0: label, q1: [labels]} for kimi form accept."""
        if not isinstance(answers, dict):
            return {}
        content: Dict[str, Any] = {}
        for i, q in enumerate(questions or []):
            if not isinstance(q, dict):
                continue
            key = keys[i] if i < len(keys) else f"q{i}"
            val = None
            for k in (q.get("question") or "", q.get("header") or ""):
                if k and k in answers:
                    val = answers[k]
                    break
            if val is None:
                continue
            allowed = [
                str(o.get("label") or "")
                for o in (q.get("options") or [])
                if isinstance(o, dict)
            ]
            if q.get("multiSelect"):
                raw = list(val) if isinstance(val, (list, tuple)) else [val]
                picked = []
                for item in raw:
                    label = str(item or "")
                    if label in allowed:
                        picked.append(label)
                        continue
                    for ol in allowed:
                        if AcpBridge._labels_match(label, ol):
                            picked.append(ol)
                            break
                # declared order
                picked = [ol for ol in allowed if ol in picked]
                if picked:
                    content[key] = picked
                continue
            label = (
                str(val[0]) if isinstance(val, (list, tuple)) and val
                else str(val or "")
            )
            if label in allowed:
                content[key] = label
                continue
            for ol in allowed:
                if AcpBridge._labels_match(label, ol):
                    content[key] = ol
                    break
        return content

    async def _handle_acp_ask_user_permission(
            self, tool_call: dict, options: list,
            tool_input: dict) -> dict:
        """Show question UI; return selected optionId (Kimi q0_opt_N / skip)."""
        raw = tool_call.get("rawInput") or {}
        if not isinstance(raw, dict):
            raw = {}
        questions = (
            raw.get("questions")
            or (tool_input or {}).get("questions")
            or []
        )
        questions = self._normalize_questions(questions)

        # Fallback: synthesize one question from permission options
        if not questions:
            choices = [
                o for o in (options or [])
                if isinstance(o, dict)
                and str(o.get("kind") or "").startswith("allow")
                and not str(o.get("optionId") or "").startswith("approve")
            ]
            q_text = ""
            for c in (tool_call.get("content") or []):
                if not isinstance(c, dict):
                    continue
                body = c.get("content")
                if isinstance(body, dict) and body.get("text"):
                    q_text = str(body["text"])
                    break
                if c.get("type") == "text" and c.get("text"):
                    q_text = str(c["text"])
                    break
            questions = [{
                "question": q_text or "Choose an option:",
                "header": "",
                "options": [
                    {"label": o.get("name") or o.get("optionId") or "?",
                     "description": ""}
                    for o in choices
                ],
                "multiSelect": False,
            }]

        self.question_id += 1
        qid = self.question_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_questions[qid] = fut
        send_notification("question_request", {
            "id": qid,
            "questions": questions,
        })
        try:
            answers = await fut
        except Exception as e:
            self.file_log(f"ask_user permission cancelled: {e}")
            answers = None
        finally:
            self.pending_questions.pop(qid, None)

        skip_id = next(
            (o.get("optionId") for o in (options or [])
             if isinstance(o, dict) and (
                 "skip" in str(o.get("optionId") or "").lower()
                 or str(o.get("kind") or "").startswith("reject"))),
            None,
        )
        if answers is None:
            if skip_id:
                return {"outcome": {
                    "outcome": "selected", "optionId": skip_id,
                }}
            return {"outcome": {"outcome": "cancelled"}}

        # Kimi ACP handleQuestion only accepts /^q0_opt_(\d+)$/.
        # Anything else (Skip, freeform, q1_opt_*) → tool result
        # "User dismissed the question without answering."
        label = self._first_answer_label(answers, questions)
        oid = self._kimi_q0_option_id(options, questions, label)
        extra = self._kimi_followup_answers(questions, answers)
        if extra:
            # After this permission RPC returns, Kimi resumes with Q0 only.
            # Inject on the next tick so we don't cancel before optionId lands.
            loop.call_later(0.08, lambda t=extra: self._inject_ask_user_followup(t))
        if oid:
            self.file_log(
                f"ask_user permission selected label={label!r} optionId={oid!r}")
            return {"outcome": {"outcome": "selected", "optionId": oid}}

        # Answered but not a listed q0 option (Other / extra questions).
        # Never send freeform optionId — Kimi treats it as dismissed.
        fallback = self._kimi_first_q0_option_id(options)
        if fallback:
            self.file_log(
                f"ask_user unmatched label={label!r} → fallback {fallback}")
            if not extra:
                self._inject_ask_user_followup(
                    self._kimi_followup_answers(questions, answers)
                    or self._kimi_other_followup(questions, answers, label))
            return {"outcome": {"outcome": "selected", "optionId": fallback}}

        if skip_id:
            return {"outcome": {
                "outcome": "selected", "optionId": skip_id,
            }}
        return {"outcome": {"outcome": "cancelled"}}

    @staticmethod
    def _answer_as_label(v) -> str:
        if isinstance(v, (list, tuple)) and v:
            return str(v[0])
        return str(v) if v is not None and str(v) != "" else ""

    @staticmethod
    def _first_answer_label(answers: dict, questions: list) -> str:
        if not isinstance(answers, dict) or not answers:
            return ""
        if questions:
            q0 = questions[0] if isinstance(questions[0], dict) else {}
            for key in (q0.get("question") or "", q0.get("header") or ""):
                if key and key in answers:
                    got = AcpBridge._answer_as_label(answers[key])
                    if got:
                        return got
        for v in answers.values():
            got = AcpBridge._answer_as_label(v)
            if got:
                return got
        return ""

    @staticmethod
    def _is_kimi_q0_opt(option_id: str) -> bool:
        return bool(re.match(r"^q0_opt_\d+$", option_id or ""))

    @staticmethod
    def _labels_match(a: str, b: str) -> bool:
        a = (a or "").strip().lower()
        b = (b or "").strip().lower()
        if not a or not b:
            return False
        if a == b:
            return True
        a2 = re.sub(r"\s*\(recommended\)\s*$", "", a).strip()
        b2 = re.sub(r"\s*\(recommended\)\s*$", "", b).strip()
        if a2 and b2 and a2 == b2:
            return True
        # Description / Other text often contains or is contained by the label.
        if len(a2) >= 8 and (a2 in b2 or b2 in a2):
            return True
        return False

    @staticmethod
    def _kimi_q0_option_id(options: list, questions: list, label: str) -> str:
        """Map a UI answer onto Kimi's /^q0_opt_N$/ permission id.

        Kimi outcomeToQuestionAnswer returns null (dismissed) for any other id.
        """
        label = (label or "").strip()
        if not label:
            return ""
        named = AcpBridge._match_option_id_for_label(options, label)
        if AcpBridge._is_kimi_q0_opt(named):
            return named
        q0 = questions[0] if questions and isinstance(questions[0], dict) else {}
        qopts = q0.get("options") or []
        for i, opt in enumerate(qopts):
            if isinstance(opt, dict):
                olabel = str(opt.get("label") or opt.get("name") or "")
                odesc = str(opt.get("description") or "")
            else:
                olabel, odesc = str(opt), ""
            if (AcpBridge._labels_match(label, olabel)
                    or AcpBridge._labels_match(label, odesc)):
                cand = f"q0_opt_{i}"
                if any(
                    isinstance(o, dict) and o.get("optionId") == cand
                    for o in (options or [])
                ):
                    return cand
        return ""

    @staticmethod
    def _kimi_first_q0_option_id(options: list) -> str:
        for o in options or []:
            if not isinstance(o, dict):
                continue
            oid = str(o.get("optionId") or "")
            if AcpBridge._is_kimi_q0_opt(oid):
                return oid
        return ""

    @staticmethod
    def _kimi_followup_answers(questions: list, answers: dict) -> str:
        """Text for Q1+ (Kimi ACP drops them) or a full recap when useful."""
        if not isinstance(answers, dict) or not answers:
            return ""
        if not questions or len(questions) < 2:
            return ""
        lines = [
            "The user answered AskUserQuestion. ACP only forwards the "
            "first question — do NOT treat this as dismissed. Honor every "
            "answer below:",
        ]
        for q in questions:
            if not isinstance(q, dict):
                continue
            header = q.get("header") or ""
            qtext = q.get("question") or header or "Question"
            val = ""
            for key in (q.get("question") or "", header):
                if key and key in answers:
                    val = AcpBridge._answer_as_label(answers[key])
                    if val:
                        break
            if not val:
                continue
            prefix = f"{header}: " if header and header != qtext else ""
            lines.append(f"- {prefix}{qtext}: {val}" if prefix else f"- {qtext}: {val}")
        if len(lines) <= 1:
            return ""
        lines.append(
            "If a tool result said the user dismissed or only includes "
            "the first choice, ignore that and use this list.")
        return "\n".join(lines)

    @staticmethod
    def _kimi_other_followup(questions: list, answers: dict, label: str) -> str:
        q0 = ""
        if questions and isinstance(questions[0], dict):
            q0 = questions[0].get("question") or questions[0].get("header") or ""
        return (
            "The user answered AskUserQuestion with custom text "
            f"(not a listed option){': ' + q0 if q0 else ''}: {label}. "
            "Do NOT treat this as dismissed."
        )

    def _inject_ask_user_followup(self, text: str) -> None:
        if not text or not str(text).strip():
            return
        send_notification("notification_wake", {
            "wake_prompt": str(text).strip(),
            "display_message": "AskUserQuestion answers",
            "interrupt": True,
        })
        self.file_log(f"ask_user follow-up injected ({len(text)} chars)")

    @staticmethod
    def _match_option_id_for_label(options: list, label: str) -> str:
        label = (label or "").strip()
        if not label:
            return ""
        # Exact name match first
        for o in options or []:
            if not isinstance(o, dict):
                continue
            name = str(o.get("name") or o.get("label") or "").strip()
            if name == label:
                return str(o.get("optionId") or "")
        # Case-insensitive exact / Recommended suffix
        low = label.lower()
        for o in options or []:
            if not isinstance(o, dict):
                continue
            name = str(o.get("name") or o.get("label") or "").strip()
            nlow = name.lower()
            if nlow == low:
                return str(o.get("optionId") or "")
            # Strip "(Recommended)" noise on either side
            n_clean = re.sub(r"\s*\(recommended\)\s*$", "", nlow).strip()
            l_clean = re.sub(r"\s*\(recommended\)\s*$", "", low).strip()
            if n_clean and n_clean == l_clean:
                return str(o.get("optionId") or "")
        return ""

    @staticmethod
    def _find_other_option_id(options: list) -> str:
        """Locate system-added Other / freeform option (Kimi adds one)."""
        for o in options or []:
            if not isinstance(o, dict):
                continue
            name = str(o.get("name") or o.get("label") or "").strip().lower()
            oid = str(o.get("optionId") or "")
            if name in (
                "other", "other...", "other…", "custom",
                "type your own", "something else",
            ):
                return oid
            if name.startswith("other"):
                return oid
            if re.search(r"(^|_)(other|freeform|custom)(_|$)", oid.lower()):
                return oid
        return ""

    def _normalize_questions(self, questions: list) -> list:
        """Normalize Grok/Claude question payloads for the plugin UI.

        Plugin expects: [{question, options:[{label, description?}], multiSelect}]
        Grok may send multi_select or multiSelect.
        """
        out = []
        for q in questions or []:
            if not isinstance(q, dict):
                continue
            opts_in = q.get("options") or []
            opts = []
            for o in opts_in:
                if isinstance(o, dict):
                    opts.append({
                        "label": o.get("label") or o.get("name") or str(o),
                        "description": o.get("description") or "",
                    })
                else:
                    opts.append({"label": str(o), "description": ""})
            out.append({
                "question": q.get("question") or q.get("header") or "Question?",
                "header": q.get("header") or "",
                "options": opts,
                "multiSelect": bool(
                    q.get("multiSelect", q.get("multi_select", False))),
            })
        return out

    def _format_ask_user_answers(self, answers: dict) -> dict:
        """Normalize plugin answers → Grok AskUserQuestionExtResponse shape.

        Grok expects (internally-tagged, snake_case outcomes):
          {outcome: "accepted", answers: {q: [label, ...]}, partial_answers: {}}

        Answer values are ALWAYS lists of strings (single-select has one element;
        multi-select has many; freeform-only uses ["Other"] / free text).
        """
        norm: Dict[str, list] = {}
        if not isinstance(answers, dict):
            return norm
        for k, v in answers.items():
            key = str(k)
            if isinstance(v, (list, tuple)):
                labels = [str(x) for x in v if x is not None and str(x) != ""]
            elif v is None:
                labels = []
            else:
                labels = [str(v)]
            if labels:
                norm[key] = labels
        return norm

    async def _acp_scheduled_task_inject(self, params: dict) -> dict:
        """Grok fires a scheduled task: inject prompt as a new session turn.

        Wire: x.ai/scheduled_task_inject_prompt { sessionId, prompt, ... }
        Plugin path: notification_wake → Session.query (same as Claude cron).
        """
        prompt = (
            params.get("prompt")
            or params.get("text")
            or params.get("message")
            or ""
        )
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(
                "x.ai/scheduled_task_inject_prompt: missing or empty prompt")
        sid = params.get("sessionId") or params.get("session_id") or ""
        if self.session_id and sid and sid != self.session_id:
            self.file_log(
                f"scheduled_task_inject: sessionId mismatch "
                f"{sid!r} vs {self.session_id!r} (still injecting)")
        fire = self._parse_fire_at(
            params.get("next_fire_at") or params.get("nextFireAt")
        )
        # Update loop banner for recurring schedules.
        self._emit_loop_scheduled(fire)
        display = "↻ " + prompt.strip().split("\n", 1)[0][:60]
        send_notification("notification_wake", {
            "wake_prompt": prompt,
            "display_message": display,
        })
        self.file_log(
            f"scheduled_task_inject → wake ({len(prompt)} chars): "
            f"{prompt[:100]!r}")
        return {}

    async def _acp_ask_user_question(self, params: dict) -> dict:
        """Handle Grok `_x.ai/ask_user_question` → plugin question UI.

        Request shape (observed):
          {sessionId, toolCallId, questions:[{question, options, multiSelect}], mode}

        Response outcomes (Grok AskUserQuestionExtResponse):
          accepted | chat_about_this | skip_interview | cancelled
        Accepted payload:
          {outcome: "accepted",
           answers: {questionText: [selectedLabel, ...]},
           partial_answers: {}}
        """
        questions = self._normalize_questions(params.get("questions") or [])
        self.file_log(
            f"ask_user_question: {len(questions)} question(s) "
            f"mode={params.get('mode')!r} toolCallId={params.get('toolCallId')}")
        if not questions:
            return {
                "outcome": "accepted",
                "answers": {},
                "partial_answers": {},
            }

        # Emit a tool_use so the transcript shows the ask (Claude parity).
        tool_call_id = params.get("toolCallId") or f"ask_{self.permission_id + 1}"
        send_notification("message", {
            "type": "tool_use",
            "id": tool_call_id,
            "name": "ask_user",
            "input": {"questions": questions},
        })

        self.question_id += 1
        qid = self.question_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_questions[qid] = fut
        send_notification("question_request", {
            "id": qid,
            "questions": questions,
        })
        try:
            answers = await fut
        except Exception as e:
            self.file_log(f"ask_user_question cancelled/error: {e}")
            answers = None
        finally:
            self.pending_questions.pop(qid, None)

        if answers is None:
            # User cancelled / interrupted the question UI.
            send_notification("message", {
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": "User cancelled",
                "is_error": True,
            })
            return {"outcome": "cancelled"}

        # Plugin returns {question_text: answer_label_or_list}.
        norm = self._format_ask_user_answers(answers)

        summary = "; ".join(
            f"{k}: {', '.join(v)}" for k, v in norm.items())
        send_notification("message", {
            "type": "tool_result",
            "tool_use_id": tool_call_id,
            "content": summary or "answered",
            "is_error": False,
        })
        self.file_log(f"ask_user_question answers: {json.dumps(norm)[:400]}")
        return {
            "outcome": "accepted",
            "answers": norm,
            "partial_answers": {},
        }

    @staticmethod
    def _resolve_grok_plan_path(session_id: str, cwd: str) -> str:
        """Canonical Grok plan path: ~/.grok/sessions/<enc_cwd>/<sid>/plan.md.

        cwd is URL-encoded with literal %2F segments.
        """
        if not session_id or not cwd:
            return ""
        enc_cwd = cwd.replace("/", "%2F")
        return os.path.join(
            os.path.expanduser("~/.grok/sessions"),
            enc_cwd, session_id, "plan.md",
        )

    def _find_kimi_plan_file(self, session_id: str = "") -> str:
        """Newest plan under ~/.kimi-code/sessions/.../plans/*.md."""
        import glob
        root = os.path.expanduser("~/.kimi-code/sessions")
        if not os.path.isdir(root):
            return ""
        sid = (session_id or self.session_id or "").strip()
        if sid:
            cands = glob.glob(os.path.join(
                root, "*", sid, "agents", "*", "plans", "*.md"))
        else:
            cands = glob.glob(os.path.join(
                root, "*", "session_*", "agents", "*", "plans", "*.md"))
        cands = [p for p in cands if os.path.isfile(p)]
        return max(cands, key=os.path.getmtime) if cands else ""

    @staticmethod
    def _plan_unified_diff(before: str, after: str, *, max_chars: int = 12000) -> str:
        """Unified diff of plan before approval UI vs after user edits."""
        import difflib
        if (before or "") == (after or ""):
            return ""
        lines = list(difflib.unified_diff(
            (before or "").splitlines(),
            (after or "").splitlines(),
            fromfile="plan (proposed)",
            tofile="plan (current)",
            lineterm="",
        ))
        if not lines:
            return ""
        diff = "\n".join(lines)
        if len(diff) > max_chars:
            return diff[:max_chars] + "\n… (diff truncated)"
        return diff

    @staticmethod
    def _format_plan_user_feedback(
        *,
        approved: bool,
        user_notes: str = "",
        plan_before: str = "",
        plan_after: str = "",
        plan_path: str = "",
    ) -> str:
        """Embed plan path / diff / body in ExtResponse feedback.

        Used for both approve and request_changes so the agent sees user
        edits immediately (Grok surfaces feedback as review comments /
        "The user said: …"). Diff is always included when non-empty.
        """
        parts = []
        notes = (user_notes or "").strip()
        if notes:
            parts.append(notes)
        elif not approved:
            parts.append("Plan rejected — revise or stop")

        after = plan_after or ""
        before = plan_before or ""
        if plan_path:
            parts.append(f"Plan file: {plan_path}")

        diff = AcpBridge._plan_unified_diff(before, after)
        if diff:
            parts.append(
                "## Plan diff (proposed → current)\n```diff\n"
                + diff
                + "\n```"
            )
        elif approved and (before or after):
            parts.append("## Plan diff\n(no changes from proposed plan)")

        # Always include current body on reject; on approve include when
        # there was a diff or notes so the agent has the final plan text.
        include_body = (not approved) or bool(diff) or bool(notes)
        if include_body:
            if after.strip():
                body = (
                    after if len(after) <= 16000
                    else after[:16000] + "\n… (plan truncated)"
                )
                parts.append("## Current plan\n" + body)
            elif before.strip() and not approved:
                body = (
                    before if len(before) <= 16000
                    else before[:16000] + "\n… (plan truncated)"
                )
                parts.append("## Proposed plan (unchanged on disk)\n" + body)
        return "\n\n".join(parts)

    @staticmethod
    def _exit_plan_ext_response(approved, feedback: str = ""):
        """Grok ExitPlanModeExtResponse — outcome-tagged (like AskUser).

        Confirmed via ACP probe against grok agent stdio:
          {"outcome": "approved"}  → PlanReady / "User has approved your plan…"
          {"approved": true}       → WRONG; maps to revise text

        Outcomes (snake_case, internally tagged on `outcome`):
          approved | request_changes | abandoned
        Optional `feedback` only for request_changes (revision notes) or
        approve-with-comments.
        """
        fb = (feedback or "").strip()
        if approved is True:
            resp = {"outcome": "approved"}
            if fb:
                resp["feedback"] = fb
            return resp
        if approved is False:
            # Explicit reject → request changes (stay in plan mode).
            return {
                "outcome": "request_changes",
                "feedback": fb or "Plan rejected — revise or stop",
            }
        # Cancel / dismiss without approve → abandon plan mode.
        return {"outcome": "abandoned"}

    def _ensure_plan_on_disk(self, plan_content: str, plan_path: str) -> str:
        """Write plan_content to disk if not already present; return path or fallback."""
        if not plan_content:
            return plan_path or ""
        if plan_path:
            try:
                os.makedirs(os.path.dirname(plan_path), exist_ok=True)
                if not os.path.isfile(plan_path):
                    with open(plan_path, "w", encoding="utf-8") as f:
                        f.write(plan_content)
                return plan_path
            except Exception as e:
                self.file_log(f"exit_plan_mode: plan file write failed: {e}")
        fallback = os.path.join(self.cwd or os.getcwd(), ".grok-plan.md")
        try:
            with open(fallback, "w", encoding="utf-8") as f:
                f.write(plan_content)
            return fallback
        except Exception as e:
            self.file_log(f"exit_plan_mode: fallback plan write failed: {e}")
            return ""

    async def _handle_acp_exit_plan_permission(
            self, tool_input: dict, tool_call: Optional[dict] = None) -> bool:
        """session/request_permission for ExitPlanMode → plan approval UI.

        Kimi (and any ACP agent without a dedicated exit_plan_mode method) hits
        this instead of the generic Y/N permission. Returns True if approved.
        """
        tool_call = tool_call or {}
        plan_content = (
            (tool_input or {}).get("plan")
            or (tool_input or {}).get("planContent")
            or (tool_input or {}).get("content")
            or (tool_input or {}).get("markdown")
            or ""
        )
        if not isinstance(plan_content, str):
            plan_content = str(plan_content or "")
        # Prefer full plan from rawInput when present
        raw = tool_call.get("rawInput") or {}
        if isinstance(raw, dict) and not plan_content:
            for k in ("plan", "planContent", "content", "markdown", "body"):
                if raw.get(k):
                    plan_content = str(raw.get(k) or "")
                    break

        plan_path = (
            (tool_input or {}).get("planFilePath")
            or (tool_input or {}).get("plan_file")
            or (tool_input or {}).get("file_path")
            or ""
        )
        # Kimi: path is often only on display / locations, not rawInput
        if not plan_path:
            display = tool_call.get("display") or {}
            if isinstance(display, dict):
                plan_path = display.get("path") or ""
        if not plan_path:
            for loc in (tool_call.get("locations") or []):
                if isinstance(loc, dict) and loc.get("path"):
                    plan_path = str(loc["path"])
                    break
        # Prefer real on-disk plan: Kimi fancy names, then Grok plan.md
        if not plan_path or not os.path.isfile(plan_path):
            kimi = self._find_kimi_plan_file(self.session_id or "")
            if kimi:
                plan_path = kimi
            elif not plan_path:
                plan_path = self._resolve_grok_plan_path(
                    self.session_id or "", self.cwd or "")
        if plan_content:
            plan_path = self._ensure_plan_on_disk(plan_content, plan_path)
        elif plan_path and os.path.isfile(plan_path):
            try:
                with open(plan_path, "r", encoding="utf-8", errors="replace") as f:
                    plan_content = f.read()
            except Exception:
                pass

        approval_input = {
            "plan": plan_content,
            "planFilePath": plan_path or "",
            "allowedPrompts": (tool_input or {}).get("allowedPrompts") or [],
        }
        self._in_plan_mode = True
        send_notification("plan_mode_enter", {})  # ensure host knows we're in plan
        result = await self.request_plan_approval(approval_input, timeout=3600)
        approved = bool(isinstance(result, dict) and result.get("approved") is True)
        if approved:
            self._in_plan_mode = False
            try:
                self.agent_mode = (
                    self.permission_mode_to_agent_mode("acceptEdits")
                    or self.agent_mode or "auto"
                )
                await self.apply_mode()
            except Exception as e:
                self.file_log(f"plan approve mode switch failed: {e}")
        else:
            self._in_plan_mode = True
            try:
                self.agent_mode = (
                    self.permission_mode_to_agent_mode("plan") or "plan"
                )
                await self.apply_mode()
            except Exception as e:
                self.file_log(f"plan reject mode switch failed: {e}")
        self.file_log(
            f"acp ExitPlanMode permission approved={approved!r} "
            f"plan_chars={len(plan_content)} path={plan_path!r}")
        return approved

    async def _acp_exit_plan_mode(self, params: dict) -> dict:
        """Grok `_x.ai/exit_plan_mode` → same plan UI as Claude ExitPlanMode."""
        plan_content = params.get("planContent") or params.get("plan") or ""
        tool_call_id = params.get("toolCallId") or ""
        self.file_log(
            f"exit_plan_mode: toolCallId={tool_call_id!r} "
            f"plan_chars={len(plan_content)}")

        if tool_call_id and tool_call_id not in self._tool_ids_emitted:
            # session/update often already opened this id — don't second-paint
            self._tool_ids_emitted.add(tool_call_id)
            self._tool_names_by_id[tool_call_id] = "ExitPlanMode"
            send_notification("message", {
                "type": "tool_use",
                "id": tool_call_id,
                "name": "ExitPlanMode",
                "input": {"plan": plan_content[:2000]},
            })

        plan_path = self._resolve_grok_plan_path(
            self.session_id or "", self.cwd or "")
        plan_path = self._ensure_plan_on_disk(plan_content, plan_path)

        tool_input = {
            "plan": plan_content,
            "planFilePath": plan_path,
            "allowedPrompts": params.get("allowedPrompts") or [],
        }
        self._in_plan_mode = True
        result = await self.request_plan_approval(tool_input, timeout=3600)

        if not result:
            approved = None
            plan_text = ""
            if plan_path and os.path.isfile(plan_path):
                try:
                    with open(plan_path, "r", encoding="utf-8", errors="replace") as f:
                        plan_text = f.read()
                except Exception:
                    plan_text = plan_content
            else:
                plan_text = plan_content
        else:
            approved = result.get("approved")
            plan_text = result.get("plan") or plan_content
            if result.get("planFilePath"):
                plan_path = result["planFilePath"]

        # Prefer on-disk plan at response time (user may have saved edits).
        if plan_path and os.path.isfile(plan_path):
            try:
                with open(plan_path, "r", encoding="utf-8", errors="replace") as f:
                    disk = f.read()
                if disk:
                    plan_text = disk
            except Exception:
                pass

        # Optional freeform notes from plugin.
        user_feedback = ""
        if isinstance(result, dict):
            user_feedback = (
                result.get("feedback")
                or result.get("comments")
                or result.get("message")
                or ""
            )
            if not isinstance(user_feedback, str):
                user_feedback = str(user_feedback or "")

        # Approve + reject: embed plan path / diff / body in feedback so the
        # agent sees user edits without re-reading plan.md.
        if approved is True or approved is False:
            user_feedback = self._format_plan_user_feedback(
                approved=bool(approved),
                user_notes=user_feedback,
                plan_before=plan_content,
                plan_after=plan_text,
                plan_path=plan_path or "",
            )

        resp = self._exit_plan_ext_response(approved, user_feedback)
        ok = approved is True
        if ok:
            summary = (
                (user_feedback[:800] + "…") if len(user_feedback) > 800
                else (user_feedback or "Plan approved — implement")
            )
        elif approved is False:
            # Plugin transcript: keep short; full plan+diff is in ExtResponse feedback.
            summary = (user_feedback[:800] + "…") if len(user_feedback) > 800 else (
                user_feedback or "Plan rejected — revise or stop"
            )
        else:
            summary = "Continue planning"
        if tool_call_id:
            send_notification("message", {
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": summary,
                "is_error": not ok,
            })
        self.file_log(
            f"exit_plan_mode result approved={approved!r} "
            f"plan_chars={len(plan_text)} resp={json.dumps(resp)[:200]}")
        return resp

    _IMAGE_EXTS = (
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
        ".tif", ".tiff", ".heic", ".heif", ".ico",
    )
    # Auto-captured screenshots (screencapture, Playwright, etc.) often land
    # as real files; agent then read_file → fs/read_text_file. ACP has no
    # binary fs method, so we re-encode pixels as a data URL in the text
    # response. Cap ≈ xAI vision / common host limits.
    _IMAGE_READ_MAX_BYTES = 5 * 1024 * 1024

    @staticmethod
    def _image_mime_from_bytes(head: bytes, path: str = "") -> Optional[str]:
        """Detect image MIME from magic bytes, else common extensions."""
        if len(head) >= 8 and head[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if len(head) >= 3 and head[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if len(head) >= 6 and head[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "image/webp"
        if len(head) >= 2 and head[:2] == b"BM":
            return "image/bmp"
        if len(head) >= 4 and head[:4] in (b"II*\x00", b"MM\x00*"):
            return "image/tiff"
        low = (path or "").lower()
        for ext, mime in (
            (".png", "image/png"), (".jpg", "image/jpeg"),
            (".jpeg", "image/jpeg"), (".gif", "image/gif"),
            (".webp", "image/webp"), (".bmp", "image/bmp"),
            (".tif", "image/tiff"), (".tiff", "image/tiff"),
            (".ico", "image/x-icon"),
            (".heic", "image/heic"), (".heif", "image/heif"),
        ):
            if low.endswith(ext):
                return mime
        return None

    @staticmethod
    def _is_kimi_plan_file(path: str) -> bool:
        """True for Kimi Code plan-mode scratch files under session workdir.

        EnterPlanMode picks a plan id then fs/read_text_file the plan path
        before it exists. ENOENT there must be empty content, not a hard
        error — otherwise EnterPlanMode fails immediately (seen in
        kimi_bridge.log: plans/mantis-moon-knight-swamp-thing.md).
        """
        if not path:
            return False
        norm = path.replace("\\", "/")
        if "/.kimi-code/sessions/" not in norm:
            return False
        # .../agents/<name>/plans/<id>.md  (main or subagent)
        return "/plans/" in norm and norm.rstrip("/").endswith(".md")

    async def _acp_fs_read(self, params: dict) -> dict:
        """fs/read_text_file — UTF-8 text; images as short path metadata.

        Grok's read_file still marks PNGs failed ("Cannot read binary file")
        and tool_output_error if we dump multi-MB base64 into the text FS
        result. Real vision is read_image (MCP) or media tools with a path.
        """
        path = params.get("path") or ""
        if not path or not os.path.isabs(path):
            raise ValueError(
                f"fs/read_text_file requires an absolute path; got {path!r}")
        line = params.get("line")
        limit = params.get("limit")
        max_chars = self.fs_read_max_chars if self.fs_read_max_chars > 0 else (
            2 * 1024 * 1024)

        # Kimi EnterPlanMode: plan file is read before write — empty is OK.
        if not os.path.exists(path) and self._is_kimi_plan_file(path):
            self.file_log(
                f"fs/read_text_file: missing kimi plan file {path!r} → empty")
            return {"content": ""}

        low = path.lower()
        by_ext = any(low.endswith(ext) for ext in self._IMAGE_EXTS)

        def _read() -> str:
            with open(path, "rb") as bf:
                head = bf.read(512)
            mime = self._image_mime_from_bytes(head, path)
            if by_ext or mime:
                return self._fs_read_image_as_text(path, mime_hint=mime)
            # NUL ⇒ not text (archives, wasm, …) — don't UTF-8-mangle
            if b"\x00" in head:
                size = os.path.getsize(path)
                raise ValueError(
                    f"fs/read_text_file: binary file {path!r} ({size} bytes); "
                    f"not UTF-8 text")
            with open(path, "r", encoding="utf-8", errors="strict") as f:
                if line is None and limit is None:
                    chunk = f.read(max_chars + 1)
                    if len(chunk) > max_chars:
                        raise ValueError(
                            f"fs/read_text_file: {path!r} exceeds "
                            f"{max_chars} chars; re-read with line/limit "
                            f"to page")
                    return chunk
                lines = f.readlines()
            start = max(0, (line or 1) - 1)
            end = start + limit if limit else len(lines)
            content = "".join(lines[start:end])
            if len(content) > max_chars:
                raise ValueError(
                    f"fs/read_text_file: page is {len(content)} chars "
                    f"(max {max_chars}); reduce limit")
            return content

        content = await asyncio.to_thread(_read)
        return {"content": content}

    def _fs_read_image_as_text(
            self, path: str, mime_hint: Optional[str] = None) -> str:
        """Short image metadata for text FS (no multi-MB base64).

        Grok rejects base64 dumps as tool_output_error / binary. Point the
        agent at read_image for pixels; keep path for image_edit etc.
        """
        import struct
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(64)
        mime = mime_hint or self._image_mime_from_bytes(head, path) or "image/png"
        w = h = 0
        if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
            w, h = struct.unpack(">II", head[16:24])
        dim = f"{w}x{h}" if w and h else "unknown"
        if self._mcp_enable_read_image:
            vision = (
                f"Do not use read_file for pixels. Call use_tool with "
                f"tool_name=\"sublime__read_image\" and "
                f"tool_input={{\"path\": {path!r}}} "
                f"(search_tool query=\"read_image\" first if unknown). "
                f"Or pass the path to image_edit / image_gen."
            )
        else:
            vision = (
                "read_image MCP is not enabled for this session "
                "(path is usable by media tools that accept file paths)."
            )
        note = (
            f"Image file ({mime}), {size} bytes, dimensions≈{dim}.\n"
            f"Path: {path}\n"
            f"{vision}"
        )
        self.file_log(
            f"fs/read_text_file: image {path!r} → path note "
            f"(mime={mime}, size={size}, no base64)")
        return note

    async def _acp_fs_write(self, params: dict) -> dict:
        path = params.get("path") or ""
        if not path or not os.path.isabs(path):
            raise ValueError(
                f"fs/write_text_file requires an absolute path; got {path!r}")
        content = params.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        max_chars = self.fs_write_max_chars if self.fs_write_max_chars > 0 else (
            2 * 1024 * 1024)
        if len(content) > max_chars:
            raise ValueError(
                f"fs/write_text_file: content is {len(content)} chars "
                f"(max {max_chars})")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        def _write() -> None:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

        await asyncio.to_thread(_write)
        return {}

    def _normalize_terminal_cmd(self, cmd: str, args: list) -> tuple:
        """Return (cmd, args, use_shell) for terminal spawn.

        Grok packs full shell lines into `command` (often `/bin/bash -lc '…'`).
        Login shells (`-l`) can hang on interactive profile prompts when run
        without a TTY, so strip login mode to plain `-c`.
        """
        if args:
            return cmd, list(args), False
        if not isinstance(cmd, str):
            return str(cmd), [], False
        # Rewrite `/bin/bash -lc 'script'` / `bash -lc "script"` → non-login.
        import re
        m = re.match(
            r"^(/bin/bash|bash|(/bin/)?zsh|(/bin/)?sh)\s+-l([c])\s+(.*)$",
            cmd.strip(), re.DOTALL)
        if m:
            shell = m.group(1)
            script = m.group(5)
            # Keep as a shell line so quoting inside the script is preserved.
            return f"{shell} -{m.group(4)} {script}", [], True
        use_shell = (
            " " in cmd
            or cmd.startswith("/bin/bash")
            or cmd.startswith("bash")
        )
        return cmd, [], use_shell

    def _kill_terminal_proc(self, proc) -> None:
        """Kill process and its group (pipelines under bash -c)."""
        if proc is None or proc.returncode is not None:
            return
        pid = proc.pid
        try:
            # start_new_session=True → kill whole group.
            os.killpg(pid, 15)  # SIGTERM
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        # Escalate after a beat if still alive (done by waiters).

    @staticmethod
    def _exit_status_from_code(code: Optional[int]) -> dict:
        """Map subprocess returncode → ACP TerminalExitStatus.

        ACP schema: exitCode is uint32 | null (minimum 0). Python reports
        signal deaths as negative codes (e.g. -15 for SIGTERM). Returning
        negatives makes Grok fail with: failed to deserialize response.
        Spec: exitCode null when terminated by signal.
        """
        if code is None:
            return {"exitCode": 0, "signal": None}
        if code < 0:
            sig_num = -code
            try:
                import signal as _signal
                sig_name = _signal.Signals(sig_num).name
            except (ValueError, AttributeError):
                sig_name = f"SIG{sig_num}"
            return {"exitCode": None, "signal": sig_name}
        return {"exitCode": int(code), "signal": None}

    def _host_emit_tool_use(
            self, tool_id: str, name: str, tool_input: dict = None,
            background: bool = False) -> None:
        """Push a tool row to the Sublime host (Claude message protocol)."""
        if not tool_id or not name:
            return
        send_notification("message", {
            "type": "tool_use",
            "id": tool_id,
            "name": name,
            "input": tool_input or {},
            "background": bool(background),
        })

    def _host_emit_tool_result(
            self, tool_id: str, content: str = "",
            is_error: bool = False) -> None:
        if not tool_id:
            return
        send_notification("message", {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": content or "",
            "is_error": bool(is_error),
        })

    def _should_synth_terminal_ui(self) -> bool:
        """True when Kimi is using terminal/* without session/update tool_call.

        When tool_call is also streaming, synthesizing would double-paint Bash.
        """
        last = float(getattr(self, "_last_session_tool_ts", 0) or 0)
        return (time.time() - last) > 1.5

    async def _acp_terminal_create(self, params: dict) -> dict:
        # After cancel, refuse new shells so the agent cannot keep spawning
        # work while the prompt is winding down — only while host prompt lives.
        host_prompt_live = (
            self._prompt_fut is not None and not self._prompt_fut.done())
        if self._prompt_cancelled and host_prompt_live:
            raise ValueError("terminal/create rejected: turn cancelled")
        cmd = params.get("command")
        if not cmd:
            raise ValueError("terminal/create requires command")
        args_in = params.get("args") or []
        cwd = params.get("cwd") or self.cwd
        env_in = params.get("env") or []
        env = os.environ.copy()
        for e in env_in:
            if isinstance(e, dict) and "name" in e:
                env[e["name"]] = e.get("value", "")
            elif isinstance(e, (list, tuple)) and len(e) == 2:
                env[str(e[0])] = str(e[1])
        if isinstance(env_in, dict):
            for k, v in env_in.items():
                env[str(k)] = str(v)
        # Non-interactive plain text: agent UIs are not a TTY — colored
        # output shows as raw ESC sequences. Force mono even if parent
        # shell / agent env has TERM=xterm-256color or FORCE_COLOR=1.
        apply_plain_terminal_env(env)
        # ACP outputByteLimit: honor request but hard-cap so one terminal
        # cannot pin unbounded memory.
        max_out = max(4096, self.terminal_output_max_bytes)
        raw_lim = params.get("outputByteLimit")
        try:
            limit = int(raw_lim) if raw_lim is not None else max_out
        except (TypeError, ValueError):
            limit = max_out
        if limit <= 0 or limit > max_out:
            limit = max_out
        cmd, args, use_shell = self._normalize_terminal_cmd(cmd, args_in)
        # stdin=DEVNULL: inherited bridge stdin is a JSON-RPC pipe; children
        # that read stdin hang forever. start_new_session: killpg on timeout.
        common = dict(
            cwd=cwd, env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if use_shell:
            proc = await asyncio.create_subprocess_shell(cmd, **common)
        else:
            proc = await asyncio.create_subprocess_exec(cmd, *args, **common)
        tid = "term_" + uuidlib.uuid4().hex[:10]
        # Kimi: command=/bin/bash args=["-c", real] even when use_shell=False
        cmd_show = self._script_from_terminal_params(cmd, args_in)
        if isinstance(cmd_show, str) and len(cmd_show) > 240:
            cmd_show = cmd_show[:239] + "…"
        slot: Dict[str, Any] = {
            "proc": proc, "stdout": "", "stderr": "",
            "limit": limit, "truncated": False, "exit_status": None,
            "cmd": (cmd_show or cmd)[:200],
        }
        self._terminals[tid] = slot
        self._mark_terminal_bg(tid, slot)
        self.file_log(
            f"terminal/create {tid} pid={proc.pid} shell={use_shell} "
            f"bg={bool(slot.get('bg'))}")

        # Kimi often runs tools ONLY via terminal/* with zero session/update
        # tool_call — host UI then shows empty "waiting" while agent is busy.
        # Synthesize Bash rows when no recent session tool stream.
        if self._should_synth_terminal_ui():
            host_id = f"term-ui-{tid}"
            slot["host_tool_id"] = host_id
            self._host_emit_tool_use(
                host_id, "Bash",
                {"command": cmd_show or str(cmd)[:240]},
                background=False,
            )
            self.file_log(f"synth host Bash for {tid} (no session tool_call)")

        async def drain(stream, key):
            buf = []
            total = 0
            try:
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    if total >= slot["limit"]:
                        slot["truncated"] = True
                        continue
                    if total + len(chunk) > slot["limit"]:
                        slot["truncated"] = True
                        remaining = slot["limit"] - total
                        if remaining > 0:
                            buf.append(
                                chunk[:remaining].decode("utf-8", "replace"))
                            total += remaining
                        continue
                    buf.append(chunk.decode("utf-8", "replace"))
                    total += len(chunk)
                    # Incremental: Kimi polls terminal/output while running;
                    # publishing only in finally left every poll empty.
                    slot[key] = strip_ansi("".join(buf))
            finally:
                # Plain text for agent + plugin UI (no raw ESC sequences).
                slot[key] = strip_ansi("".join(buf))

        async def wait_and_close():
            try:
                await asyncio.gather(
                    drain(proc.stdout, "stdout"),
                    drain(proc.stderr, "stderr"),
                    return_exceptions=True)
                code = await proc.wait()
                slot["exit_status"] = self._exit_status_from_code(code)
            except asyncio.CancelledError:
                self._kill_terminal_proc(proc)
                code = None
                try:
                    code = await asyncio.wait_for(proc.wait(), timeout=1.0)
                except Exception:
                    try:
                        os.killpg(proc.pid, 9)
                        code = await asyncio.wait_for(proc.wait(), timeout=0.5)
                    except Exception:
                        code = -9
                slot["exit_status"] = self._exit_status_from_code(
                    code if code is not None else -15)
                raise
            except Exception as e:
                self.file_log(f"terminal {tid} reader error: {e}")
                slot["exit_status"] = {"exitCode": 1, "signal": None}
            finally:
                # Close synthetic host Bash row if we opened one
                hid = slot.get("host_tool_id")
                if hid:
                    try:
                        out = (
                            (slot.get("stdout") or "")
                            + (slot.get("stderr") or "")
                        )
                        es = slot.get("exit_status") or {}
                        code = es.get("exitCode")
                        is_err = code is not None and int(code) != 0
                        if len(out) > 6000:
                            out = out[:6000] + "\n…[truncated]"
                        self._host_emit_tool_result(
                            hid, out or f"exit {code}", is_error=is_err)
                    except Exception as e:
                        self.file_log(f"synth tool_result {tid}: {e}")
                # Claude-compatible wake when host is already idle after end_turn
                try:
                    self._emit_bg_terminal_complete(tid)
                except Exception as e:
                    self.file_log(f"bg terminal complete emit failed: {e}")

        slot["reader"] = asyncio.create_task(wait_and_close())
        return {"terminalId": tid}

    async def _acp_terminal_output(self, params: dict) -> dict:
        tid = params.get("terminalId") or ""
        slot = self._terminals.get(tid)
        if not slot:
            # Already released/killed (e.g. on interrupt) — empty output,
            # cancelled exit. Matches terminal/wait_for_exit soft handling.
            self.file_log(
                f"terminal/output {tid} unknown (released); return cancelled")
            return {
                "output": "",
                "truncated": False,
                "exitStatus": {"exitCode": None, "signal": "SIGTERM"},
            }
        out = (slot.get("stdout") or "") + (slot.get("stderr") or "")
        return {"output": out, "truncated": bool(slot["truncated"]),
                "exitStatus": slot.get("exit_status")}

    def _mark_terminal_bg(self, tid: str, slot: dict) -> None:
        """⚙ only for this execute's explicit detach or native kimi detached."""
        eid = self._take_pending_execute_id()
        inp = (self._tool_inputs_by_id.get(eid) or {}) if eid else {}
        explicit = bool(
            eid and (
                inp.get("run_in_background") is True
                or inp.get("detached") is True
                or eid in self._bg_tool_ids
            )
        )
        native = None
        if hasattr(self, "_kimi_detached_meta"):
            try:
                native = self._kimi_detached_meta(str(slot.get("cmd") or ""))
            except Exception:
                native = None
        if not explicit and not native:
            if hasattr(self, "_schedule_kimi_detached_probe"):
                try:
                    self._schedule_kimi_detached_probe(tid)
                except Exception:
                    pass
            return
        if not eid:
            eid = f"term-bg-{tid}"
        slot["bg"] = True
        slot["tool_use_id"] = eid
        if eid not in self._bg_tool_ids:
            self._register_bg_tool(
                eid, inp if inp else {"command": slot.get("cmd")})
        self._bind_terminal_to_bg_tool(tid, eid)
        if native and hasattr(self, "_link_terminal_to_kimi_task"):
            try:
                self._link_terminal_to_kimi_task(tid, eid, native)
            except Exception:
                pass

    async def _acp_terminal_wait(self, params: dict) -> dict:
        tid = params.get("terminalId") or ""
        slot = self._terminals.get(tid)
        if not slot:
            # Already released/killed (e.g. on interrupt) — report cancelled.
            return {"exitCode": None, "signal": "SIGTERM"}
        # kimi-code AcpTerminalProcess: exitCode ?? -1. A null exit is
        # "killed", then ProcessTask fails and terminal/release kills the
        # still-running command. wait_for_exit MUST stay pending until the
        # process actually exits. Dispatch is create_task so this does not
        # block the ACP reader. session/prompt already returned for
        # run_in_background; ⚙ clears on real exit via wait_and_close.
        # if slot.get("bg") and slot.get("exit_status") is None:
        #     return {"exitCode": None, "signal": None}
        reader = slot.get("reader")
        timeout = self.terminal_wait_timeout_s
        if reader is not None and not reader.done():
            try:
                if timeout and timeout > 0:
                    await asyncio.wait_for(
                        asyncio.shield(reader), timeout=timeout)
                else:
                    # ACP: wait until exit; agent aborts via terminal/kill.
                    await asyncio.shield(reader)
            except asyncio.TimeoutError:
                self.file_log(
                    f"terminal/wait_for_exit {tid} TIMEOUT after {timeout}s "
                    f"cmd={slot.get('cmd')!r}")
                self._kill_terminal_proc(slot.get("proc"))
                # Give reader a moment to collect exit status after kill.
                try:
                    await asyncio.wait_for(asyncio.shield(reader), timeout=2.0)
                except Exception:
                    if not reader.done():
                        reader.cancel()
                        try:
                            await reader
                        except Exception:
                            pass
                if not slot.get("exit_status"):
                    # Optional client timeout: still ACP-valid (no negative).
                    slot["exit_status"] = {
                        "exitCode": None, "signal": "SIGTERM",
                    }
            except asyncio.CancelledError:
                return {"exitCode": None, "signal": "SIGTERM"}
            except Exception as e:
                self.file_log(f"terminal/wait_for_exit {tid} error: {e}")
        elif reader is not None and reader.done():
            # Re-raise nothing — just pick up exit_status.
            try:
                reader.result()
            except Exception:
                pass
        # Terminal may have been closed during wait.
        slot = self._terminals.get(tid) or slot
        es = slot.get("exit_status") or {
            "exitCode": None, "signal": "SIGTERM",
        }
        # Sanitize any legacy negative codes still sitting on the slot.
        code = es.get("exitCode")
        if isinstance(code, int) and code < 0:
            es = self._exit_status_from_code(code)
            slot["exit_status"] = es
        self.file_log(
            f"terminal/wait_for_exit {tid} → "
            f"exitCode={es.get('exitCode')} signal={es.get('signal')}")
        # Ensure Claude bg wake even if bind raced with process exit
        try:
            self._emit_bg_terminal_complete(tid)
        except Exception as e:
            self.file_log(f"wait_for_exit bg complete: {e}")
        return {"exitCode": es.get("exitCode"), "signal": es.get("signal")}

    async def _acp_terminal_kill(self, params: dict) -> dict:
        tid = params.get("terminalId") or ""
        slot = self._terminals.get(tid)
        if slot:
            self._kill_terminal_proc(slot.get("proc"))
            self.file_log(f"terminal/kill {tid}")
        return {}

    async def _acp_terminal_release(self, params: dict) -> dict:
        tid = params.get("terminalId") or ""
        await self._terminal_close(tid)
        return {}

    async def _terminal_close(self, tid: str) -> None:
        slot = self._terminals.pop(tid, None)
        if not slot:
            return
        self._kill_terminal_proc(slot.get("proc"))
        reader = slot.get("reader")
        if reader and not reader.done():
            reader.cancel()
            try:
                await asyncio.wait_for(reader, timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        # Hard kill if still alive after cancel.
        proc = slot.get("proc")
        if proc and proc.returncode is None:
            try:
                os.killpg(proc.pid, 9)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.file_log(f"terminal/release {tid}")


def run_bridge(bridge: AcpBridge) -> None:
    """Entry point helper for agent-specific main modules."""
    try:
        asyncio.run(bridge.run_stdin_loop())
    except KeyboardInterrupt:
        pass
