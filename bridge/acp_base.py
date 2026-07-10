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
import sys
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
        self.cwd: str = os.getcwd()
        self.agent_mode: str = ""
        self._view_id: Optional[Any] = None
        self.agent_capabilities: Dict[str, Any] = {}
        self.negotiated_protocol_version: int = 1
        self._terminals: Dict[str, Dict[str, Any]] = {}
        # toolCallIds already shown as tool_use (avoid duplicate ☐ rows).
        self._tool_ids_emitted: set = set()
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
        # Client-side backup timers when host does not inject scheduled prompts.
        # task_id (or toolCallId) → asyncio.Task
        self._client_schedule_tasks: Dict[str, Any] = {}
        # Serialize writes to agent stdin — concurrent create_task handlers
        # (permission + terminal + fs) would otherwise interleave JSON lines.
        self._acp_write_lock: Optional[asyncio.Lock] = None
        # Hard cap so a hung child never freezes the turn forever.
        self.terminal_wait_timeout_s: float = float(
            os.environ.get("SUBLIME_CLAUDE_TERM_TIMEOUT", "120") or 120)
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
    })
    # Read-only tools safe in plan mode without prompting.
    PLAN_READONLY_TOOLS = frozenset({
        "Read", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite",
        "search_tool", "update_goal",
        "read_image", "mcp__sublime__read_image",
        "x_keyword_search", "x_semantic_search", "x_user_search",
        "x_thread_fetch",
        "scheduler_list", "CronList",
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

    async def apply_model(self) -> None:
        """Push self.model to the live session. Default: session/set_model."""
        if not self.session_id or not self.model:
            return
        try:
            result = await self._send_acp("session/set_model", {
                "sessionId": self.session_id,
                "modelId": self.model,
            }) or {}
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

    async def apply_mode(self) -> None:
        """Push self.agent_mode to the live session via session/set_mode."""
        if not self.session_id or not self.agent_mode:
            return
        try:
            await self._send_acp("session/set_mode", {
                "sessionId": self.session_id,
                "modeId": self.agent_mode,
            })
        except Exception as e:
            self.log(f"session/set_mode({self.agent_mode}) failed: {e}")

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
        return self.LOG_PATH

    def file_log(self, msg: str) -> None:
        try:
            with open(self.log_path(), "a") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    # ── Subprocess lifecycle ───────────────────────────────────────────

    async def _spawn(self) -> None:
        if self.proc is not None:
            return
        args = self.agent_argv()
        try:
            with open(self.log_path(), "w") as f:
                f.write(f"# {self.BACKEND_NAME}-bridge — {args}, cwd={self.cwd}\n")
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
                break
            if not line:
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
                if kind in (
                    "tool_call", "tool_call_update", "current_mode_update",
                    "scheduled_task_created", "scheduled_task_fired",
                    "scheduled_task_deleted",
                ):
                    self.file_log(f"← acp update {kind}: {json.dumps(params)[:400]}")
                self._forward_update(params)
            elif method and "mcp" in method.lower():
                # Surface MCP lifecycle (servers_updated, init_progress, …)
                self.file_log(
                    f"← acp {method}: {json.dumps(params)[:600]}")
            elif method in (
                "x.ai/session/update", "_x.ai/session/update",
            ):
                # Grok may nest schedule lifecycle under x.ai/session/update.
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

    def _forward_update(self, params: dict) -> None:
        if self._loading_session:
            upd = params.get("update", {})
            kind = upd.get("sessionUpdate")
            if kind == "current_mode_update":
                self._handle_mode_update(upd)
            elif kind == "available_commands_update":
                self._handle_commands_update(upd)
            return

        upd = params.get("update", {})
        kind = upd.get("sessionUpdate")
        # After user interrupt: drop *new* tool starts so ☐ rows don't appear
        # post-[interrupted]. Still accept tool_call_update completions so
        # already-open rows can settle.
        if self._prompt_cancelled and kind == "tool_call":
            self.file_log(
                f"drop tool_call after cancel: "
                f"{(upd.get('title') or upd.get('toolCallId') or '')!r}")
            return
        if kind == "agent_message_chunk":
            if self._prompt_cancelled:
                return  # no more assistant stream after cancel
            text = (upd.get("content") or {}).get("text", "")
            if text:
                send_notification("message",
                                  {"type": "text_delta", "text": text})
        elif kind == "agent_thought_chunk":
            if self._prompt_cancelled:
                return
            text = (upd.get("content") or {}).get("text", "")
            if text:
                send_notification("message",
                                  {"type": "thinking", "thinking": text})
        elif kind == "tool_call":
            tool_name = self._normalize_tool_name(upd)
            tool_input = self._tool_input_from_update(upd, tool_name)
            tid = upd.get("toolCallId")
            self._tool_ids_emitted.add(tid)
            if tid:
                if tool_name and tool_name != "tool":
                    self._tool_names_by_id[tid] = tool_name
                if tool_input:
                    prev = self._tool_inputs_by_id.get(tid) or {}
                    self._tool_inputs_by_id[tid] = {**prev, **tool_input}
            send_notification("message", {
                "type": "tool_use",
                "id": tid,
                "name": tool_name,
                "input": tool_input,
            })
        elif kind == "tool_call_update":
            usage = self.usage_from_tool_update(upd)
            if usage is not None:
                send_notification("message",
                                  {"type": "turn_usage", "usage": usage})
            status = upd.get("status")
            tid = upd.get("toolCallId")
            tool_name = self._normalize_tool_name(upd)
            # Completed updates often strip title/_meta → name becomes "tool".
            # Recover the name we saw on the open tool_call / earlier update.
            if (not tool_name or tool_name == "tool") and tid:
                tool_name = self._tool_names_by_id.get(tid) or tool_name or "tool"
            elif tid and tool_name and tool_name != "tool":
                self._tool_names_by_id[tid] = tool_name
            # Grok: bare tool_call then richer update. Emit tool_use at most
            # once per id (plugin upserts); re-emitting created a second ☐
            # that never received tool_result → last row stuck pending.
            enriched = self._tool_input_from_update(upd, tool_name)
            if tid and enriched:
                prev = self._tool_inputs_by_id.get(tid) or {}
                self._tool_inputs_by_id[tid] = {**prev, **enriched}
            if tid not in self._tool_ids_emitted:
                if enriched or upd.get("rawInput") or upd.get("locations"):
                    self._tool_ids_emitted.add(tid)
                    send_notification("message", {
                        "type": "tool_use",
                        "id": tid,
                        "name": tool_name,
                        "input": enriched,
                    })
            elif enriched and status not in ("completed", "failed"):
                # Enrich open row only (same id → output.tool upserts).
                send_notification("message", {
                    "type": "tool_use",
                    "id": tid,
                    "name": tool_name,
                    "input": enriched,
                })
            # Do not re-emit background=True on in_progress — Claude's UI
            # waits for task_notification which most ACP agents never send.
            if status in ("completed", "failed"):
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
                    })
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
                send_notification("message", {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": text,
                    "is_error": is_error,
                })
                self._tool_ids_emitted.discard(tid)
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
        elif kind == "user_message_chunk":
            # Agents (notably Grok) re-broadcast the user prompt. The plugin
            # already renders ◎ <prompt> — do not double-print as text_delta.
            pass
        elif kind == "plan":
            # Grok/ACP "plan" session updates (todo-style entries). Do not dump
            # a **Plan:** text block into the transcript — the plugin already
            # has a Tasks UI (TodoWrite / Task*), and external trackers
            # (kanban etc.) own real planning. Echoing here is noise.
            pass
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

        # 3) title only when it looks like a tool id, not human description prose.
        # Bare: "spawn_subagent", "list_dir". Decorated: "Read `/path`".
        # Do NOT use first word of "Smoke-test subagent harness" / "Get task output".
        title = upd.get("title")
        if isinstance(title, str) and title.strip():
            t = title.strip()
            mapped = self._map_agent_tool_id(t)
            if mapped:
                return mapped
            first = t.split()[0].strip("`'\"*")
            mapped = self._map_agent_tool_id(first)
            if mapped:
                return mapped
            # snake_case / lowercase machine id without map entry
            if first and ("_" in first or first.islower()) and first.isascii():
                return first

        mapped = KIND_TO_NAME.get((upd.get("kind") or "").lower())
        if mapped:
            return mapped
        return "tool"

    def _normalize_tool_input(self, raw: Any, tool_name: str = "") -> dict:
        """Map agent rawInput → Claude formatter keys only."""
        if not isinstance(raw, dict):
            return {}
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

    def _tool_input_from_update(self, upd: dict, tool_name: str = "") -> dict:
        """Claude-formatter-ready input from rawInput + locations + title."""
        out = self._normalize_tool_input(upd.get("rawInput") or {}, tool_name)
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
        # Task: Grok puts the human description in title ("Smoke-test subagent harness")
        if tool_name == "Task" and isinstance(title, str):
            t = title.strip()
            if t and t not in self.TOOL_TO_CANONICAL and "_" not in t.split()[0]:
                # Skip bare tool ids / snake_case titles
                if not out.get("description") and t.lower() not in (
                        "task", "spawn_subagent", "spawn subagent"):
                    out["description"] = t
            if not out.get("subagent_type"):
                st = out.get("subagentType") or out.get("type") or ""
                if st:
                    out["subagent_type"] = st
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
        }

    async def handle_initialize(self, req_id: Optional[int],
                                 params: dict) -> None:
        self.model = self.normalize_model(params.get("model") or self.model)
        self.cwd = params.get("cwd") or self.cwd
        if self.cwd and os.path.isdir(self.cwd):
            try:
                os.chdir(self.cwd)
            except OSError:
                pass
        self._view_id = params.get("view_id")
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
            init_request = {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
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
        if modes.get("currentModeId"):
            self.agent_mode = modes["currentModeId"]
        models = result.get("models") or {}
        if models.get("availableModels"):
            self._available_models = models["availableModels"]
        if models.get("currentModelId") and not self.model:
            self.model = models["currentModelId"]

    def _collect_mcp_servers(self) -> list:
        """Built-in Sublime MCP server for the agent.

        Grok's McpServer untagged enum requires stdio servers shaped as:
          {name, type:"stdio", command, args?, env: [{name,value}, ...] | []}
        A dict env {} is rejected with Invalid params. DSR/other agents accept
        this shape too (extra `type` is ignored when unused).
        """
        bridge_dir = os.path.dirname(os.path.abspath(__file__))
        plugin_dir = os.path.dirname(bridge_dir)
        mcp_server_path = os.path.join(plugin_dir, "mcp", "server.py")
        if not os.path.exists(mcp_server_path):
            self.file_log(f"MCP server missing: {mcp_server_path}")
            return []
        args = [mcp_server_path]
        if self._view_id is not None:
            args.append(f"--view-id={self._view_id}")
        return [{
            "name": "sublime",
            "type": "stdio",
            "command": sys.executable,
            "args": args,
            "env": [],
        }]

    async def handle_query(self, req_id: Optional[int],
                            params: dict) -> None:
        if self.session_id is None:
            send_error(req_id, -32000, "session not initialized")
            return
        # A new query supersedes any prior in-flight prompt.
        if self._query_req_id is not None and self._query_req_id != req_id:
            self.file_log(
                f"query: superseding in-flight req {self._query_req_id}")
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
            result = await self._send_prompt(prompt_blocks) or {}
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
            self._cancel_in_flight = False
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
        try:
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

        first_cancel = not self._prompt_cancelled
        self._prompt_cancelled = True
        self._cancel_in_flight = True

        # Kill client-side terminals so terminal/wait_for_exit unblocks.
        for tid in list(self._terminals):
            try:
                await self._terminal_close(tid)
            except Exception:
                pass

        # One session/cancel per turn only.
        if first_cancel and self.session_id is not None:
            try:
                await self._notify_acp(
                    "session/cancel", {"sessionId": self.session_id})
            except Exception as e:
                self.log(f"session/cancel notify failed: {e}")
        elif not first_cancel:
            self.file_log("interrupt: skip duplicate session/cancel")

        # Unblock local await if the agent is slow/stuck (e.g. terminal wait).
        # Prefer resolving with cancelled so handle_query takes the normal path.
        fut = self._prompt_fut
        if fut is not None and not fut.done():
            # Brief grace for agent stopReason=cancelled, then force so the
            # plugin query RPC always completes (otherwise UI never idles).
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout=0.35)
            except (asyncio.TimeoutError, Exception):
                pass
            if not fut.done():
                fut.set_result({"stopReason": "cancelled"})
                self.file_log(
                    "interrupt: forced local prompt future to cancelled")
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
        try:
            await self.apply_model()
            send_result(req_id, {"ok": True, "model": self.model})
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

    def _permission_decision(self, tool_name: str,
                              tool_input: dict,
                              tool_call: Optional[dict] = None) -> Optional[bool]:
        """Apply plugin permission rules.

        Returns True (auto-allow), False (auto-deny), or None (ask user).
        """
        mode = self.permission_mode or "default"
        tool_call = tool_call or {}

        # Full bypass — same as Claude bypassPermissions / --always-approve.
        if mode == "bypassPermissions":
            return True

        # Built-in Sublime MCP tools are always trusted (Claude bridge parity).
        if tool_name.startswith("mcp__sublime__"):
            return True

        # Project / user auto-allow patterns (Always button + permissions.allow).
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
        allow_once = next((o.get("optionId") for o in options
                           if isinstance(o, dict)
                           and o.get("kind") == "allow_once"), None)
        allow_always = next((o.get("optionId") for o in options
                             if isinstance(o, dict)
                             and o.get("kind") == "allow_always"), None)
        reject_once = next((o.get("optionId") for o in options
                            if isinstance(o, dict)
                            and o.get("kind") == "reject_once"), None)
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
        if allow_id is None or reject_id is None:
            self.file_log(
                f"permission request missing options: {json.dumps(options)[:300]}")
            return {"outcome": {"outcome": "cancelled"}}

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

    async def _acp_exit_plan_mode(self, params: dict) -> dict:
        """Grok `_x.ai/exit_plan_mode` → same plan UI as Claude ExitPlanMode.

        Reuses base.request_plan_approval (plan_mode_exit + plan_response).
        Maps the bool result to Grok's ExitPlanModeExtResponse shape.
        """
        plan_content = params.get("planContent") or params.get("plan") or ""
        tool_call_id = params.get("toolCallId") or ""
        self.file_log(
            f"exit_plan_mode: toolCallId={tool_call_id!r} "
            f"plan_chars={len(plan_content)}")

        if tool_call_id:
            send_notification("message", {
                "type": "tool_use",
                "id": tool_call_id,
                "name": "ExitPlanMode",
                "input": {"plan": plan_content[:2000]},
            })

        # Canonical Grok plan path: ~/.grok/sessions/<enc_cwd>/<sessionId>/plan.md
        # (cwd is URL-encoded with literal %2F segments). Always ensure planContent
        # is on disk there so post-approve tool execution can read it.
        plan_path = ""
        if self.session_id:
            enc_cwd = (self.cwd or "").replace("/", "%2F")
            plan_path = os.path.join(
                os.path.expanduser("~/.grok/sessions"),
                enc_cwd, self.session_id, "plan.md",
            )
        if plan_content and plan_path:
            try:
                os.makedirs(os.path.dirname(plan_path), exist_ok=True)
                # Don't clobber a newer on-disk plan if agent already wrote it.
                if not os.path.isfile(plan_path):
                    with open(plan_path, "w", encoding="utf-8") as f:
                        f.write(plan_content)
            except Exception as e:
                self.file_log(f"exit_plan_mode: plan file write failed: {e}")
        if not plan_path or not os.path.isfile(plan_path or ""):
            if plan_content:
                plan_path = os.path.join(self.cwd or os.getcwd(), ".grok-plan.md")
                try:
                    with open(plan_path, "w", encoding="utf-8") as f:
                        f.write(plan_content)
                except Exception as e:
                    self.file_log(f"exit_plan_mode: fallback plan write failed: {e}")
                    plan_path = ""

        tool_input = {
            "plan": plan_content,
            "planFilePath": plan_path,
            "allowedPrompts": params.get("allowedPrompts") or [],
        }
        self._in_plan_mode = True
        result = await self.request_plan_approval(tool_input, timeout=3600)

        if not result:
            approved = None
            # Fall back to on-disk plan (saved only), then request snapshot.
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
            # Plugin sends disk-saved plan only (unsaved buffer ignored).
            plan_text = result.get("plan") or plan_content
            if result.get("planFilePath"):
                plan_path = result["planFilePath"]

        ok = approved is True
        # Grok ExitPlanModeExtResponse (2 fields: approved + feedback).
        # Observed in bridge log: {"approved": true, "feedback": ""} still
        # yields tool result "The user wants to revise the plan…" — Grok
        # treats *presence* of feedback (even empty string) as request-changes.
        # On approve: omit feedback entirely (or null). Never send plan body.
        # On reject/cancel: send short feedback text for the revise path.
        if ok:
            summary = "Plan approved — implement"
            resp = {"approved": True}
        elif approved is False:
            summary = "Plan rejected — revise or stop"
            resp = {"approved": False, "feedback": summary}
        else:
            summary = "Continue planning"
            resp = {"approved": False, "feedback": summary}
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
        note = (
            f"Image file ({mime}), {size} bytes, dimensions≈{dim}.\n"
            f"Path: {path}\n"
            f"Do not use read_file for pixels. Call use_tool with "
            f"tool_name=\"sublime__read_image\" and "
            f"tool_input={{\"path\": {path!r}}} "
            f"(search_tool query=\"read_image\" first if unknown). "
            f"Or pass the path to image_edit / image_gen."
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

    async def _acp_terminal_create(self, params: dict) -> dict:
        # After cancel, refuse new shells so the agent cannot keep spawning
        # work while the prompt is winding down.
        if self._prompt_cancelled:
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
        slot: Dict[str, Any] = {
            "proc": proc, "stdout": "", "stderr": "",
            "limit": limit, "truncated": False, "exit_status": None,
            "cmd": cmd[:200],
        }
        self._terminals[tid] = slot
        self.file_log(f"terminal/create {tid} pid={proc.pid} shell={use_shell}")

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
                slot["exit_status"] = {
                    "exitCode": code if code is not None else 0,
                    "signal": None,
                }
            except asyncio.CancelledError:
                self._kill_terminal_proc(proc)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except Exception:
                    try:
                        os.killpg(proc.pid, 9)
                    except Exception:
                        pass
                slot["exit_status"] = {"exitCode": 130, "signal": "SIGTERM"}
                raise
            except Exception as e:
                self.file_log(f"terminal {tid} reader error: {e}")
                slot["exit_status"] = {"exitCode": 1, "signal": None}

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
                "exitStatus": {"exitCode": 130, "signal": "SIGTERM"},
            }
        out = (slot.get("stdout") or "") + (slot.get("stderr") or "")
        return {"output": out, "truncated": bool(slot["truncated"]),
                "exitStatus": slot.get("exit_status")}

    async def _acp_terminal_wait(self, params: dict) -> dict:
        tid = params.get("terminalId") or ""
        slot = self._terminals.get(tid)
        if not slot:
            # Already released/killed (e.g. on interrupt) — report cancelled.
            return {"exitCode": 130, "signal": "SIGTERM"}
        reader = slot.get("reader")
        timeout = self.terminal_wait_timeout_s
        if reader is not None and not reader.done():
            try:
                await asyncio.wait_for(asyncio.shield(reader), timeout=timeout)
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
                if not slot.get("exit_status"):
                    slot["exit_status"] = {
                        "exitCode": 124, "signal": "SIGTERM",
                    }
            except asyncio.CancelledError:
                return {"exitCode": 130, "signal": "SIGTERM"}
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
        es = slot.get("exit_status") or {"exitCode": 130, "signal": "SIGTERM"}
        self.file_log(
            f"terminal/wait_for_exit {tid} → "
            f"exitCode={es.get('exitCode')} signal={es.get('signal')}")
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
