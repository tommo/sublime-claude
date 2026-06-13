"""dsr bridge — drives the `dsr acp` subprocess and translates its
JSON-RPC `session/update` notifications into the `message` events that
sublime-claude's front-end consumes.

Architecture:
  Sublime plugin ──stdin/stdout JSON-RPC──> dsr_main.py (this bridge)
                                          │
                                          └──stdin/stdout NDJSON──> dsr acp

The bridge owns one ACP session per Sublime view's lifetime. Each
`query` request becomes a `session/prompt`; the streamed `session/update`
notifications become `message` notifications back to Sublime in the
same shape the Claude / Copilot bridges emit.

ACP coverage:
  Outbound (we → dsr):  initialize, session/new, session/prompt, session/cancel
  Inbound (dsr → us):   session/update (agent_message_chunk, agent_thought_chunk,
                          user_message_chunk, tool_call, tool_call_update,
                          plan, current_mode_update, available_commands_update),
                        session/request_permission (→ native Sublime UI),
                        fs/read_text_file, fs/write_text_file,
                        terminal/create + output + wait_for_exit + kill + release.

Permission gating now flows through ACP's request/response: dsr runs in
`--edit-mode=review`, emits `session/request_permission`, the bridge routes
it to Sublime's native permission UI, and forwards the user's choice back.
"""
import asyncio
import json
import os
import shutil
import sys
import uuid as uuidlib
from typing import Any, Dict, Optional, Tuple

# Ensure the bridge directory is on sys.path so `from base import ...` works
# when the plugin spawns us. Also add the plugin root so we can pull in
# shared modules like `settings` for MCP-server config loading.
_BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_BRIDGE_DIR)
sys.path.insert(0, _BRIDGE_DIR)
sys.path.insert(0, _PLUGIN_DIR)

from base import BaseBridge   # noqa: E402
from rpc_helpers import send_notification, send_result, send_error  # noqa: E402


DSR_BIN = os.environ.get("DSR_BIN") or shutil.which("dsr") or "dsr"
DSR_LOG_PATH = "/tmp/dsr_bridge.log"


def _file_log(msg: str) -> None:
    """Mirror bridge log lines to disk so we can diagnose without Sublime console."""
    try:
        with open(DSR_LOG_PATH, "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


class DsrBridge(BaseBridge):
    BACKEND_NAME = "dsr"

    def __init__(self) -> None:
        super().__init__()
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.session_id: Optional[str] = None
        self.next_acp_id: int = 0
        # outbound id → asyncio.Future
        self.pending: Dict[int, asyncio.Future] = {}
        # Background task reading dsr's stdout
        self.reader_task: Optional[asyncio.Task] = None
        self.model: str = "deepseek-v4-pro"
        self.cwd: str = os.getcwd()
        # Permission gating routed through the native Sublime UI now that the
        # bridge implements ACP session/request_permission. dsr edit modes per
        # `dsr acp --help`: review|auto|yolo|plan. `review` asks for non-
        # allowlisted commands → emits session/request_permission, which we
        # answer via the plugin's native permission UI. `yolo` bypasses all
        # gating (the original first-slice default).
        self.edit_mode: str = "review"
        self._view_id: Optional[Any] = None
        # Negotiated by initialize → stored for capability gating later.
        self.agent_capabilities: Dict[str, Any] = {}
        self.negotiated_protocol_version: int = 1
        # Terminal subprocesses spawned via ACP terminal/create.
        # id → {"proc": Process, "stdout": str, "stderr": str, "limit": int,
        #        "truncated": bool, "exit_status": dict|None, "reader": Task}
        self._terminals: Dict[str, Dict[str, Any]] = {}

    # ── Subprocess lifecycle ───────────────────────────────────────────

    async def _spawn(self) -> None:
        if self.proc is not None:
            return
        args = [DSR_BIN, "acp",
                "--edit-mode=" + self.edit_mode,
                "--model=" + self.model]
        # Rotate the log per spawn so each new session starts clean.
        try:
            with open(DSR_LOG_PATH, "w") as f:
                f.write(f"# dsr-bridge log — spawned {args}, cwd={self.cwd}\n")
        except Exception:
            pass
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd)
        self.reader_task = asyncio.create_task(self._read_dsr_stdout())
        # Also capture dsr's stderr to the same file (for MCP-launch errors etc.)
        asyncio.create_task(self._read_dsr_stderr())

    async def _read_dsr_stderr(self) -> None:
        if self.proc is None or self.proc.stderr is None:
            return
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            try:
                _file_log("[dsr stderr] " + line.decode(errors="replace").rstrip())
            except Exception:
                pass

    async def _read_dsr_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode().strip())
            except json.JSONDecodeError:
                continue
            has_id = "id" in msg
            method = msg.get("method")
            # Three JSON-RPC message shapes, distinguished by presence of id/method:
            #   id + no method  → response to an outbound request we sent.
            #   no id + method  → notification (e.g. session/update).
            #   id + method     → REQUEST from the agent to us (ACP client-side
            #                     methods: session/request_permission, fs/*, terminal/*).
            if has_id and method is None:
                fut = self.pending.pop(msg["id"], None)
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(
                            msg["error"].get("message", "acp error")))
                    else:
                        fut.set_result(msg.get("result"))
                continue
            params = msg.get("params", {})
            if has_id:
                # Agent → client REQUEST. Dispatch off the main reader so a slow
                # handler (e.g. permission UI awaiting the user) can't block
                # the read loop.
                asyncio.create_task(self._dispatch_acp_request(
                    msg["id"], method, params))
                continue
            # NOTIFICATION (no id).
            if method == "session/update":
                self._forward_update(params)

    def _acp_id(self) -> int:
        self.next_acp_id += 1
        return self.next_acp_id

    async def _send_acp(self, method: str, params: dict) -> Any:
        await self._spawn()
        assert self.proc is not None and self.proc.stdin is not None
        rid = self._acp_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        env = {"jsonrpc": "2.0", "id": rid,
               "method": method, "params": params}
        line = json.dumps(env)
        _file_log(f"→ acp {method} (id={rid}): {line[:800]}")
        self.proc.stdin.write((line + "\n").encode())
        await self.proc.stdin.drain()
        result = await fut
        try:
            _file_log(f"← acp {method} (id={rid}) result: {json.dumps(result)[:800]}")
        except Exception:
            _file_log(f"← acp {method} (id={rid}) result: {result!r}")
        return result

    # ── ACP session/update → Sublime `message` notifications ───────────

    def _forward_update(self, params: dict) -> None:
        upd = params.get("update", {})
        kind = upd.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = (upd.get("content") or {}).get("text", "")
            if text:
                send_notification("message",
                                  {"type": "text_delta", "text": text})
        elif kind == "agent_thought_chunk":
            text = (upd.get("content") or {}).get("text", "")
            if text:
                send_notification("message",
                                  {"type": "thinking", "thinking": text})
        elif kind == "tool_call":
            send_notification("message", {
                "type": "tool_use",
                "id": upd.get("toolCallId"),
                "name": self._normalize_tool_name(upd),
                "input": self._normalize_tool_input(upd.get("rawInput") or {}),
            })
        elif kind == "tool_call_update":
            usage = upd.get("dsr.usage")
            if usage is not None:
                send_notification("message",
                                  {"type": "turn_usage", "usage": usage})
            status = upd.get("status")
            tool_name = self._normalize_tool_name(upd)
            # NOTE: don't re-emit tool_use with background=True for
            # `in_progress`. Claude's SDK-side flow uses `background` to mean
            # "this tool outlives the turn; wait for a task_notification before
            # marking done" — but dsr never emits task_notification, so the
            # tool would get stuck at ⚙ forever (plugin's _on_msg_tool_result
            # short-circuits when matched.status == "background"). Transient
            # in_progress just means "running, will complete shortly"; the
            # original tool_use already rendered, and the completed/failed
            # update below resolves it.
            if status in ("completed", "failed"):
                # If the agent only sent the diff in the RESULT (not in
                # rawInput), re-emit a tool_use with the diff so the Edit
                # formatter can render it. The plugin patches the existing
                # tool_use by tool_use_id, so this updates in place.
                diff_input = self._extract_diff_input(upd) if tool_name in ("Edit", "Write") else None
                if diff_input:
                    enriched = self._normalize_tool_input(upd.get("rawInput") or {})
                    enriched.update(diff_input)
                    send_notification("message", {
                        "type": "tool_use",
                        "id": upd.get("toolCallId"),
                        "name": tool_name,
                        "input": enriched,
                    })
                text = self._extract_tool_content(upd, tool_name)
                send_notification("message", {
                    "type": "tool_result",
                    "tool_use_id": upd.get("toolCallId"),
                    "content": text,
                    "is_error": (status == "failed"),
                })
        elif kind == "user_message_chunk":
            # The agent is echoing user content back (e.g. an injected message).
            # Render as text so the user sees it in the conversation.
            text = (upd.get("content") or {}).get("text", "")
            if text:
                send_notification("message",
                                  {"type": "text_delta", "text": text})
        elif kind == "plan":
            # Plan-mode output: list of step descriptions. Render inline as
            # markdown-flavored text — there's no dedicated plan UI on the
            # Sublime side yet (could become one later).
            entries = upd.get("entries") or upd.get("steps") or []
            if entries:
                lines = ["\n\n**Plan:**\n"]
                for i, e in enumerate(entries):
                    if isinstance(e, dict):
                        content = e.get("content") or e.get("text") or ""
                        status = e.get("status") or ""
                        mark = {"completed": "✔", "in_progress": "▸",
                                "pending": "○"}.get(status, "•")
                        lines.append(f"{mark} {content}\n")
                    else:
                        lines.append(f"• {e}\n")
                send_notification("message",
                                  {"type": "text", "text": "".join(lines)})
        elif kind == "current_mode_update":
            # Mode changes (e.g. plan ↔ default). Surface in status bar via a
            # system event the plugin already routes.
            mode = upd.get("currentModeId") or upd.get("modeId") or ""
            send_notification("message", {
                "type": "system",
                "subtype": "mode_update",
                "data": {"mode": mode},
            })
        elif kind == "available_commands_update":
            # The set of slash-commands the agent currently offers. Surface as
            # a system event; UI integration is a follow-up (could feed
            # auto-complete in the input area).
            cmds = upd.get("availableCommands") or upd.get("commands") or []
            send_notification("message", {
                "type": "system",
                "subtype": "available_commands",
                "data": {"commands": cmds},
            })

    # ACP `kind` → sublime tool-name canonical form expected by
    # tool_formatters.TOOL_FORMATTERS. We prefer the canonical name so the
    # inline detail (file_path / command / pattern) renders just like a
    # Claude Code session. ACP ToolKind variants per spec:
    # read, edit, write, execute, search, glob, fetch, delete, move, think,
    # other.
    _KIND_TO_NAME = {
        "read":    "Read",
        "edit":    "Edit",
        "write":   "Write",
        "execute": "Bash",
        "search":  "Grep",
        "glob":    "Glob",
        "fetch":   "WebFetch",
        "delete":  "Bash",
        "move":    "Bash",
        "think":   "Thinking",
        "other":   "",   # falls through to title / kind verbatim
    }

    # dsr's actual tool names → Claude canonical names. dsr names match its
    # `dsr tools` listing — read_file, run_command, todo_write, etc. Without
    # this map they'd render as bare snake_case strings instead of going
    # through the dedicated formatter (which strips file_path / command etc.
    # into a one-liner).
    _DSR_TOOL_TO_CANONICAL = {
        # Filesystem / shell
        "read_file":         "Read",
        "edit_file":         "Edit",
        "write_file":        "Write",
        "multi_edit":        "Edit",          # closest formatter (Claude has no MultiEdit-specific one)
        "list_directory":    "Glob",
        "search_files":      "Glob",
        "search_content":    "Grep",
        "run_command":       "Bash",
        "run_background":    "Bash",
        # Web
        "web_fetch":         "WebFetch",
        "web_search":        "WebSearch",
        # Planning / TODO
        "todo_write":        "TodoWrite",
        "ask_choice":        "ask_user",
        "submit_plan":       "ExitPlanMode",
        "mark_step_complete":"TaskUpdate",
        "revise_plan":       "TaskUpdate",
        # Agents / skills
        "spawn_subagent":    "Task",
        "run_skill":         "Skill",
        # Sublime MCP — already canonical
        "sublime_eval":      "sublime_eval",
        "find_file":         "find_file",
        "get_window_summary":"get_window_summary",
        "get_symbols":       "get_symbols",
        "goto_symbol":       "goto_symbol",
        "read_view":         "read_view",
        # Terminal MCP — match sublime's namespaced names so the
        # mcp__sublime__terminal_* formatters fire.
        "terminal_run":      "mcp__sublime__terminal_run",
        "terminal_read":     "mcp__sublime__terminal_read",
        "terminal_list":     "mcp__sublime__terminal_list",
        "terminal_close":    "mcp__sublime__terminal_read",
    }

    # dsr / ACP camelCase rawInput keys → sublime formatter snake_case keys.
    # Both ACP's content-diff keys (oldText/newText) and Edit's classic
    # oldString/newString are normalized to old_string/new_string so the Edit
    # formatter renders a proper diff regardless of which shape the agent uses.
    _INPUT_KEY_MAP = {
        "filePath":      "file_path",
        "filepath":      "file_path",
        "path":          "file_path",
        "oldString":     "old_string",
        "newString":     "new_string",
        "oldText":       "old_string",
        "newText":       "new_string",
        "unifiedDiff":   "unified_diff",
        "notebookPath":  "notebook_path",
        "subagentType":  "subagent_type",
        "replaceAll":    "replace_all",
        # Common pass-throughs (already snake_case in most agents, listed for
        # clarity — Python dict.get falls back to the key unchanged).
        # command, pattern, query, url, content, file_path, old_string,
        # new_string, todos, subject, status, taskId, skill — all preserved.
    }

    @classmethod
    def _normalize_tool_name(cls, upd: dict) -> str:
        # 1. Prefer rawInput.tool / rawInput.name (literal name the agent
        #    embedded). Then canonicalize via the dsr→Claude mapping so the
        #    Claude tool_formatters dispatch fires.
        raw = upd.get("rawInput") or {}
        if isinstance(raw, dict):
            for key in ("tool", "name", "toolName"):
                v = raw.get(key)
                if isinstance(v, str) and v.strip():
                    name = v.strip()
                    return cls._DSR_TOOL_TO_CANONICAL.get(name, name)
        # 2. Try ACP kind mapping for agents that don't embed a literal name.
        mapped = cls._KIND_TO_NAME.get((upd.get("kind") or "").lower())
        if mapped:
            return mapped
        # 3. Fall back to title or kind verbatim — at least something visible.
        return upd.get("title") or upd.get("kind") or "tool"

    @classmethod
    def _normalize_tool_input(cls, raw: Any) -> dict:
        if not isinstance(raw, dict):
            return {}
        out: dict = {}
        for k, v in raw.items():
            out[cls._INPUT_KEY_MAP.get(k, k)] = v
        return out

    @staticmethod
    def _extract_tool_content(upd: dict, tool_name: str = "") -> str:
        """Pull plain text out of an ACP tool_call_update for sublime's
        tool_result rendering. Falls back to rawOutput when no content blocks
        are present. For Bash, prefer stdout (matches Claude formatter)."""
        out: list = []
        for block in (upd.get("content") or []):
            if not isinstance(block, dict):
                continue
            # ACP shape: {"type": "content", "content": {"type": "text", "text": "..."}}
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
        # Bash-shaped result: prefer stdout (then stderr) so the Claude
        # _format_bash_result head/tail trimming has clean input.
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

    @staticmethod
    def _extract_diff_input(upd: dict) -> Optional[dict]:
        """If a tool_call_update carries a `diff` content block, return a partial
        input dict {file_path, old_string, new_string} so the Claude Edit
        formatter can render a proper diff at result time (some agents only
        ship the diff in the result, not in rawInput)."""
        for block in (upd.get("content") or []):
            if not isinstance(block, dict):
                continue
            inner = block.get("content") if block.get("type") == "content" else block
            if isinstance(inner, dict) and inner.get("type") == "diff":
                out: dict = {}
                if inner.get("path"):     out["file_path"]  = inner["path"]
                if inner.get("oldText") is not None: out["old_string"] = inner["oldText"]
                if inner.get("newText") is not None: out["new_string"] = inner["newText"]
                if out:
                    return out
        return None

    # ── BaseBridge overrides ───────────────────────────────────────────

    async def handle_initialize(self, req_id: Optional[int],
                                 params: dict) -> None:
        # Sublime tells us model + cwd here.
        self.model = params.get("model") or self.model
        self.cwd = params.get("cwd") or self.cwd
        # Stash view_id so the built-in sublime MCP server can route eval
        # calls back to the right session, matching the Claude bridge.
        self._view_id = params.get("view_id")
        try:
            # Declare what we (the client) support so the agent knows it can
            # call our fs/* and terminal/* methods, and which auth methods we
            # offer. Without this, an agent that gates updates on capabilities
            # may stay quiet (e.g. skip plan streaming).
            init_request = {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
                "clientInfo": {
                    "name": "sublime-claude",
                    "version": "0.1",
                },
            }
            init_result = await self._send_acp("initialize", init_request) or {}
            # Validate & record what we negotiated. ACP server returns the
            # version it agreed on (must match what we sent, or lower).
            negotiated = init_result.get("protocolVersion", 1)
            if negotiated != 1:
                self.log(f"agent negotiated protocolVersion={negotiated} "
                         f"(client requested 1); proceeding")
            self.negotiated_protocol_version = negotiated
            self.agent_capabilities = init_result.get("agentCapabilities", {}) or {}
            new_params: Dict[str, Any] = {"cwd": self.cwd}
            mcp_servers = self._collect_mcp_servers()
            _file_log(f"_collect_mcp_servers → {len(mcp_servers)} server(s): "
                      f"{json.dumps(mcp_servers)[:600]}")
            if mcp_servers:
                new_params["mcpServers"] = mcp_servers
            new_result = await self._send_acp("session/new", new_params)
            self.session_id = new_result["sessionId"]
            send_result(req_id, {
                "ok": True,
                "backend": "dsr",
                "agent": init_result.get("agentInfo", {}),
                "agent_capabilities": self.agent_capabilities,
                "protocol_version": negotiated,
                "sessionId": self.session_id,
                "mcp_servers": [s.get("name") for s in mcp_servers],
                "streaming": True})
        except Exception as e:
            send_error(req_id, -32000, f"dsr initialize failed: {e}")

    def _collect_mcp_servers(self) -> list:
        """Build the ACP `session/new.mcpServers` list.

        Mirrors codex_main.py: inject only the built-in `sublime` stdio
        server so the agent can call editor tools. Other MCP servers are
        the agent's own concern — dsr loads its own config, not Claude's.
        """
        bridge_dir = os.path.dirname(os.path.abspath(__file__))
        plugin_dir = os.path.dirname(bridge_dir)
        mcp_server_path = os.path.join(plugin_dir, "mcp", "server.py")
        if not os.path.exists(mcp_server_path):
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
        prompt = params.get("prompt") or params.get("text") or ""
        if isinstance(prompt, str):
            prompt_blocks = [{"type": "text", "text": prompt}]
        else:
            prompt_blocks = prompt
        self._query_req_id = req_id
        try:
            result = await self._send_acp("session/prompt", {
                "sessionId": self.session_id,
                "prompt": prompt_blocks})
            stop_reason = (result or {}).get("stopReason", "end_turn")
            # MUST send "result" notification BEFORE send_result so the
            # plugin's _on_msg_result calls output.meta() and flips
            # current.working=False — without this the spinner stays on
            # and the session never re-enters input mode.
            send_notification("message", {
                "type": "result",
                "session_id": self.session_id or "",
                "duration_ms": 0,
                "is_error": False,
                "num_turns": 1,
                "total_cost_usd": 0,
                "stop_reason": stop_reason,
            })
            send_result(req_id, {
                "status": "complete",
                "stopReason": stop_reason})
        except Exception as e:
            send_error(req_id, -32000, f"dsr query failed: {e}")
        finally:
            self._query_req_id = None

    async def handle_interrupt(self, req_id: Optional[int],
                                params: dict) -> None:
        if self.session_id is not None:
            try:
                await self._send_acp("session/cancel",
                                      {"sessionId": self.session_id})
            except Exception:
                pass
        if self._query_req_id is not None:
            send_result(self._query_req_id, {"status": "interrupted"})
            self._query_req_id = None
        send_result(req_id, {"status": "interrupted"})

    async def handle_shutdown(self, req_id: Optional[int],
                               params: dict) -> None:
        self.running = False
        # Best-effort release of any terminal subprocesses we created.
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

    # ── ACP agent→client REQUEST handling ──────────────────────────────────
    # These methods reply to the agent over the same JSON-RPC channel as
    # our outbound requests. Spec: https://agentclientprotocol.com/protocol/v1/schema
    # We implement the baseline + optional fs and terminal capabilities we
    # advertised in `initialize`.

    _ACP_REQUEST_HANDLERS = {
        "session/request_permission": "_acp_request_permission",
        "fs/read_text_file":          "_acp_fs_read",
        "fs/write_text_file":         "_acp_fs_write",
        "terminal/create":            "_acp_terminal_create",
        "terminal/output":            "_acp_terminal_output",
        "terminal/wait_for_exit":     "_acp_terminal_wait",
        "terminal/kill":              "_acp_terminal_kill",
        "terminal/release":           "_acp_terminal_release",
    }

    async def _dispatch_acp_request(self, rid: int, method: Optional[str],
                                     params: dict) -> None:
        handler_name = self._ACP_REQUEST_HANDLERS.get(method or "")
        if not handler_name:
            await self._send_acp_response(rid, error={
                "code": -32601, "message": f"Method not supported: {method}"})
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
        self.proc.stdin.write((json.dumps(env) + "\n").encode())
        try:
            await self.proc.stdin.drain()
        except Exception:
            pass

    # ── session/request_permission → native Sublime UI ─────────────────────
    # ACP request shape:  {sessionId, toolCall, options: [PermissionOption]}
    # ACP option shape:   {optionId, name, kind: allow_once|allow_always|
    #                                          reject_once|reject_always}
    # ACP response shape: {outcome: {outcome:"selected", optionId} |
    #                              {outcome:"cancelled"}}
    async def _acp_request_permission(self, params: dict) -> dict:
        tool_call = params.get("toolCall") or {}
        options = params.get("options") or []
        # Translate to the plugin's existing permission_request shape (which
        # is allow/deny only). Find the first "allow_*" and "reject_*" options
        # so we can map plugin allow→that optionId, plugin deny→that optionId.
        allow_id = next((o.get("optionId") for o in options
                         if isinstance(o, dict)
                         and (o.get("kind") or "").startswith("allow")), None)
        reject_id = next((o.get("optionId") for o in options
                          if isinstance(o, dict)
                          and (o.get("kind") or "").startswith("reject")), None)
        if allow_id is None or reject_id is None:
            # Spec-noncompliant request — refuse safely.
            return {"outcome": {"outcome": "cancelled"}}
        # Bridge handshake: send a permission_request notification, wait for
        # the plugin's permission_response RPC via the future in
        # pending_permissions (resolved by base._handle_permission_response).
        self.permission_id += 1
        pid = self.permission_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_permissions[pid] = fut
        send_notification("permission_request", {
            "id": pid,
            "tool": self._normalize_tool_name(tool_call),
            "input": self._normalize_tool_input(tool_call.get("rawInput") or {}),
        })
        try:
            answer = await fut
        except Exception:
            return {"outcome": {"outcome": "cancelled"}}
        allowed = isinstance(answer, dict) and answer.get("kind") == "approved"
        return {"outcome": {"outcome": "selected",
                             "optionId": allow_id if allowed else reject_id}}

    # ── fs/read_text_file ──────────────────────────────────────────────────
    # ACP: {sessionId, path, line?, limit?}; returns {content}.
    # Paths are absolute per spec. Optional 1-based line + limit window.
    async def _acp_fs_read(self, params: dict) -> dict:
        path = params.get("path") or ""
        if not path or not os.path.isabs(path):
            raise ValueError(f"fs/read_text_file requires an absolute path; got {path!r}")
        line = params.get("line")
        limit = params.get("limit")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if line is None and limit is None:
                return {"content": f.read()}
            lines = f.readlines()
        start = max(0, (line or 1) - 1)
        end = start + limit if limit else len(lines)
        return {"content": "".join(lines[start:end])}

    # ── fs/write_text_file ─────────────────────────────────────────────────
    # ACP: {sessionId, path, content}; returns {}.
    async def _acp_fs_write(self, params: dict) -> dict:
        path = params.get("path") or ""
        if not path or not os.path.isabs(path):
            raise ValueError(f"fs/write_text_file requires an absolute path; got {path!r}")
        content = params.get("content", "")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {}

    # ── terminal/* ─────────────────────────────────────────────────────────
    # Minimal in-process terminal: spawn the requested command, collect
    # stdout/stderr up to outputByteLimit, expose to the agent. Not a TTY —
    # ACP doesn't mandate one and a pipe is sufficient for typical agent
    # usage (run a command, read its output).
    async def _acp_terminal_create(self, params: dict) -> dict:
        cmd = params.get("command")
        if not cmd:
            raise ValueError("terminal/create requires command")
        args = params.get("args") or []
        cwd = params.get("cwd") or self.cwd
        env_in = params.get("env") or []
        env = os.environ.copy()
        for e in env_in:
            if isinstance(e, dict) and "name" in e:
                env[e["name"]] = e.get("value", "")
        limit = int(params.get("outputByteLimit") or 1_000_000)
        proc = await asyncio.create_subprocess_exec(
            cmd, *args, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        tid = "term_" + uuidlib.uuid4().hex[:10]
        slot: Dict[str, Any] = {
            "proc": proc, "stdout": "", "stderr": "",
            "limit": limit, "truncated": False, "exit_status": None,
        }
        self._terminals[tid] = slot

        async def drain(stream, key):
            buf = []
            total = 0
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                if total + len(chunk) > slot["limit"]:
                    slot["truncated"] = True
                    remaining = slot["limit"] - total
                    if remaining > 0:
                        buf.append(chunk[:remaining].decode("utf-8", "replace"))
                        total += remaining
                    continue
                buf.append(chunk.decode("utf-8", "replace"))
                total += len(chunk)
            slot[key] = "".join(buf)

        async def wait_and_close():
            await asyncio.gather(drain(proc.stdout, "stdout"),
                                  drain(proc.stderr, "stderr"))
            code = await proc.wait()
            slot["exit_status"] = {"exitCode": code, "signal": None}

        slot["reader"] = asyncio.create_task(wait_and_close())
        return {"terminalId": tid}

    async def _acp_terminal_output(self, params: dict) -> dict:
        tid = params.get("terminalId") or ""
        slot = self._terminals.get(tid)
        if not slot:
            raise ValueError(f"unknown terminalId: {tid}")
        out = (slot.get("stdout") or "") + (slot.get("stderr") or "")
        return {"output": out, "truncated": bool(slot["truncated"]),
                "exitStatus": slot.get("exit_status")}

    async def _acp_terminal_wait(self, params: dict) -> dict:
        tid = params.get("terminalId") or ""
        slot = self._terminals.get(tid)
        if not slot:
            raise ValueError(f"unknown terminalId: {tid}")
        reader = slot.get("reader")
        if reader is not None:
            await reader
        es = slot.get("exit_status") or {}
        return {"exitCode": es.get("exitCode"), "signal": es.get("signal")}

    async def _acp_terminal_kill(self, params: dict) -> dict:
        tid = params.get("terminalId") or ""
        slot = self._terminals.get(tid)
        if slot:
            try:
                slot["proc"].terminate()
            except ProcessLookupError:
                pass
        return {}

    async def _acp_terminal_release(self, params: dict) -> dict:
        tid = params.get("terminalId") or ""
        await self._terminal_close(tid)
        return {}

    async def _terminal_close(self, tid: str) -> None:
        slot = self._terminals.pop(tid, None)
        if not slot:
            return
        proc = slot.get("proc")
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        reader = slot.get("reader")
        if reader and not reader.done():
            reader.cancel()


def main() -> None:
    bridge = DsrBridge()
    try:
        asyncio.run(bridge.run_stdin_loop())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
