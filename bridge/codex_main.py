#!/usr/bin/env python3
"""
Bridge process between Sublime Text and Codex CLI (app-server).
Translates between our JSON-RPC protocol and Codex's app-server protocol.
Both speak JSON-RPC 2.0 over stdio, so this is mainly a message translator.
"""
import asyncio
import json
import os
import sys
import shutil
import time
from pathlib import Path
from typing import Any, Optional


# ── Logging: stderr → plugin console; also append to shared bridge log file ──

sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from logger import get_bridge_logger
    _file_log = get_bridge_logger()
except Exception:
    _file_log = None


def log(msg: str) -> None:
    line = f"[codex-bridge] {msg}"
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    if _file_log is not None:
        try:
            _file_log.info(line)
        except Exception:
            pass  # benign: file logging is best-effort


from rpc_helpers import send, send_error, send_result, send_notification
from base import BaseBridge


# ── Command → Tool Conversions ──────────────────────────────────────────

import re

def _detect_read_command(cmd: str) -> Optional[dict]:
    """Detect file-read commands and return simplified tool params."""
    if not cmd:
        return None
    s = cmd.strip()
    # sed -n 'N,Mp' FILE  or  sed -n Np FILE
    m = re.match(r"sed\s+-n\s+'?(\d+)(?:,(\d+))?p'?\s+(\S+)\s*$", s)
    if m:
        start, end, path = m.group(1), m.group(2), m.group(3)
        start = int(start)
        if end:
            return {"_tool": "Read", "path": path, "offset": start, "limit": int(end) - start + 1}
        return {"_tool": "Read", "path": path, "offset": start, "limit": 1}
    # nl -ba FILE | sed -n 'N,Mp'
    m = re.match(r"nl\s+-ba\s+(\S+)\s*\|\s*sed\s+-n\s+'?(\d+),(\d+)p'?\s*$", s)
    if m:
        path, start, end = m.group(1), int(m.group(2)), int(m.group(3))
        return {"_tool": "Read", "path": path, "offset": start, "limit": end - start + 1}
    # cat FILE
    m = re.match(r"cat\s+(\S+)\s*$", s)
    if m:
        return {"_tool": "Read", "path": m.group(1)}
    # head -n N FILE  or  head -N FILE
    m = re.match(r"head\s+(?:-n\s+)?-?(\d+)\s+(\S+)\s*$", s)
    if m:
        return {"_tool": "Read", "path": m.group(2), "limit": int(m.group(1))}
    # tail -n N FILE
    m = re.match(r"tail\s+(?:-n\s+)?-?(\d+)\s+(\S+)\s*$", s)
    if m:
        return {"_tool": "Read", "path": m.group(2), "limit": int(m.group(1))}
    # rg [flags] PATTERN [PATHS...] → Grep hint
    m = re.match(r'rg\s+(?:-[a-zA-Z]+\s+)*(?:\'([^\']+)\'|"([^"]+)"|(\S+))\s*(.*)', s)
    if m:
        pattern = m.group(1) or m.group(2) or m.group(3)
        paths = m.group(4).strip() if m.group(4) else ""
        result = {"_tool": "Grep", "pattern": pattern}
        if paths:
            result["path"] = paths
        return result
    return None


# ── Codex Bridge ────────────────────────────────────────────────────────

class CodexBridge(BaseBridge):
    BACKEND_NAME = "codex"

    def __init__(self):
        super().__init__()
        self.codex_proc: Optional[asyncio.subprocess.Process] = None
        self.thread_id: Optional[str] = None
        self.turn_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self.codex_request_counter = 1

        # Map our permission_id → codex server-request id
        # NOTE: pending_approvals stores codex request IDs (int), not Futures —
        # different from the BaseBridge default. We override the handlers below.
        self.pending_approvals: dict[int, Any] = {}
        # Override pending_questions too (BaseBridge uses Future, we use ids)
        self.pending_questions = {}
        self.permission_counter = 0

        # Track turn timing
        self._turn_start_time: float = 0
        self._last_usage: Optional[dict] = None

        # Accumulate command output from outputDelta events
        self._command_output: dict[str, list[str]] = {}

    def log(self, msg):  # type: ignore[override]
        log(msg)  # delegate to module-level log() which also writes to file

    # ── Codex subprocess management ─────────────────────────────────

    async def start_codex(self, cwd: str, config_overrides: list[str] = None) -> None:
        """Spawn codex app-server as subprocess."""
        codex_path = shutil.which("codex") or "codex"
        cmd = [codex_path, "app-server"]
        if config_overrides:
            for c in config_overrides:
                cmd.extend(["-c", c])

        log(f"Starting: {' '.join(cmd)}")
        self.codex_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

    async def send_to_codex(self, msg: dict) -> None:
        """Send JSON-RPC message to codex app-server."""
        if not self.codex_proc or not self.codex_proc.stdin:
            return
        data = json.dumps(msg) + "\n"
        self.codex_proc.stdin.write(data.encode())
        await self.codex_proc.stdin.drain()

    async def codex_request(self, method: str, params: dict = None) -> int:
        """Send a request to codex, return the request id."""
        req_id = self.codex_request_counter
        self.codex_request_counter += 1
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        await self.send_to_codex(msg)
        return req_id

    async def codex_respond(self, req_id: Any, result: Any) -> None:
        """Send a JSON-RPC response back to codex (for server-requests)."""
        await self.send_to_codex({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": result,
        })

    # ── Handle requests from Sublime ────────────────────────────────

    # Dispatch handled by BaseBridge.handle_request via _dispatch_table

    async def handle_initialize(self, req_id: int, params: dict) -> None:
        """Initialize: spawn codex app-server, create thread."""
        cwd = params.get("cwd", os.getcwd())
        model = params.get("model")
        permission_mode = params.get("permission_mode", "default")
        view_id = params.get("view_id", "")

        # Build config overrides
        config = []
        # Map Claude model names to codex default, or use as-is
        claude_models = ("opus", "sonnet", "haiku", "claude")
        if not model or any(m in model.lower() for m in claude_models):
            model = "gpt-5.3-codex"
        config.append(f'model="{model}"')

        # Map permission modes to codex approval_policy
        # Valid: untrusted, on-failure, on-request, granular, never
        perm_map = {
            "bypassPermissions": "never",
            "acceptEdits": "on-failure",
            "auto": "on-failure",
            "default": "on-request",
        }
        codex_policy = perm_map.get(permission_mode, "on-request")
        config.append(f'approval_policy="{codex_policy}"')

        # Full filesystem access to avoid shared memory / file lock permission errors
        config.append('sandbox_mode="danger-full-access"')

        # Configure Sublime MCP server so Codex can use editor tools
        mcp_server_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "mcp", "server.py"
        )
        if os.path.exists(mcp_server_path):
            args = [mcp_server_path]
            if view_id:
                args.append(f"--view-id={view_id}")
            # Pass as TOML config overrides
            config.append(f'mcp_servers.sublime.command="{sys.executable}"')
            args_toml = "[" + ", ".join(f'"{a}"' for a in args) + "]"
            config.append(f'mcp_servers.sublime.args={args_toml}')
            log(f"MCP server: {sys.executable} {' '.join(args)}")

        # Start codex app-server and its reader loops
        await self.start_codex(cwd, config)
        asyncio.create_task(self._read_codex())
        asyncio.create_task(self._read_codex_stderr())

        # Send initialize to codex
        init_id = await self.codex_request("initialize", {
            "clientInfo": {"name": "sublime-claude", "version": "1.0"},
        })

        # Wait for initialize response
        init_result = await self._wait_for_response(init_id)
        if init_result is None:
            send_error(req_id, -32000, "Codex initialize timed out")
            return

        # Start a thread
        thread_params = {"cwd": cwd}
        system_prompt = params.get("system_prompt")
        if system_prompt:
            thread_params["developerInstructions"] = system_prompt

        # Resume if session_id provided (must be valid UUID)
        resume_id = params.get("resume")
        if resume_id:
            import re as _re
            if not _re.match(r'^[0-9a-fA-F\-]{36}$', resume_id):
                log(f"Invalid thread ID for resume (not UUID): {resume_id}")
                send_error(req_id, -32000, f"Cannot resume: invalid thread ID '{resume_id}'")
                return
            thread_params["threadId"] = resume_id
            start_id = await self.codex_request("thread/resume", thread_params)
        else:
            start_id = await self.codex_request("thread/start", thread_params)

        # Wait for thread response
        thread_result = await self._wait_for_response(start_id)
        if self.thread_id is None and thread_result:
            thread = thread_result.get("thread", thread_result)
            self.thread_id = thread.get("id") or thread_result.get("threadId")

        if not self.thread_id:
            err_msg = f"Failed to {'resume' if resume_id else 'start'} codex thread"
            log(err_msg)
            send_error(req_id, -32000, err_msg)
            return

        self.session_id = self.thread_id
        log(f"Initialized: thread_id={self.thread_id}")

        send_result(req_id, {
            "session_id": self.session_id,
            "mcp_servers": [],
            "agents": [],
        })

    async def handle_query(self, req_id: int, params: dict) -> None:
        """Start a turn with the given prompt."""
        prompt = params.get("prompt", "")
        if not self.thread_id:
            send_error(req_id, -32000, "No active thread")
            return

        self._turn_start_time = time.time()
        # Store query req_id — respond when turn completes (not immediately)
        self._query_req_id = req_id

        # Build input
        user_input = [{"type": "text", "text": prompt}]

        # Add images if provided (format: {"mime_type": str, "data": base64_str})
        images = params.get("images", [])
        for img in images:
            if isinstance(img, dict):
                mime = img.get("mime_type", "image/png")
                data = img.get("data", "")
                user_input.append({"type": "image", "url": f"data:{mime};base64,{data}"})
            elif isinstance(img, str):
                if img.startswith("/"):
                    user_input.append({"type": "localImage", "path": img})
                else:
                    user_input.append({"type": "image", "url": img})

        await self.codex_request("turn/start", {
            "threadId": self.thread_id,
            "input": user_input,
        })

    async def handle_interrupt(self, req_id, params=None) -> None:
        """Interrupt current turn."""
        if self.thread_id and self.turn_id:
            await self.codex_request("turn/interrupt", {
                "threadId": self.thread_id,
                "turnId": self.turn_id,
            })
        # Complete the pending query RPC as interrupted
        if self._query_req_id is not None:
            send_result(self._query_req_id, {"status": "interrupted"})
            self._query_req_id = None
        send_result(req_id, {"status": "interrupted"})

    async def _handle_permission_response(self, req_id, params: dict) -> None:
        """Override BaseBridge: forward permission response to codex (not Future-based)."""
        perm_id = params.get("id")
        allow = params.get("allow", False)

        codex_req_id = self.pending_approvals.pop(perm_id, None)
        if codex_req_id is None:
            send_result(req_id, {"ok": False, "error": "No pending approval"})
            return

        decision = "accept" if allow else "decline"
        await self.codex_respond(codex_req_id, {"decision": decision})
        send_result(req_id, {"ok": True})

    async def _handle_question_response(self, req_id, params: dict) -> None:
        """Override BaseBridge: forward question response to codex (not Future-based)."""
        q_id = params.get("id")
        answers = params.get("answers", {})

        codex_req_id = self.pending_questions.pop(q_id, None)
        if codex_req_id is None:
            send_result(req_id, {"ok": False})
            return

        # Translate to codex format: {questionId: {answers: [str]}}
        codex_answers = {k: {"answers": [v] if isinstance(v, str) else v} for k, v in answers.items()}
        await self.codex_respond(codex_req_id, {"answers": codex_answers})
        send_result(req_id, {"ok": True})

    async def handle_shutdown(self, req_id, params=None) -> None:
        """Shut down codex process."""
        self.running = False
        if self.codex_proc:
            try:
                self.codex_proc.stdin.close()
                await asyncio.wait_for(self.codex_proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                self.codex_proc.kill()
        send_result(req_id, {"ok": True})

    # ── Handle messages from Codex ──────────────────────────────────

    async def handle_codex_message(self, msg: dict) -> None:
        """Process a JSON-RPC message from codex app-server."""
        # Is it a server-request (has id + method)?
        if "id" in msg and "method" in msg:
            await self.handle_codex_server_request(msg)
            return

        # Is it a response to our request?
        if "id" in msg and ("result" in msg or "error" in msg):
            req_id = msg["id"]
            if "error" in msg:
                log(f"Codex error response for req {req_id}: {msg['error']}")
            if req_id in self._pending_responses:
                self._pending_responses[req_id].set_result(msg.get("result"))
            return

        # Must be a notification
        method = msg.get("method", "")
        params = msg.get("params", {})

        if method == "thread/started":
            thread = params.get("thread", {})
            self.thread_id = thread.get("id") or params.get("threadId")

        elif method == "turn/started":
            turn = params.get("turn", params)
            self.turn_id = turn.get("id") or turn.get("turnId")

        elif method == "turn/completed":
            if self.turn_id is not None:  # Guard against double-fire
                self._complete_turn(is_error=bool(params.get("turn", {}).get("error")))

        elif method == "item/agentMessage/delta":
            send_notification("message", {
                "type": "text",
                "text": params.get("delta", ""),
            })

        elif method == "item/reasoning/summaryTextDelta":
            send_notification("message", {
                "type": "thinking",
                "thinking": params.get("delta", ""),
            })

        elif method == "item/started":
            self._handle_item_started(params)

        elif method == "item/completed":
            self._handle_item_completed(params)

        elif method == "item/commandExecution/outputDelta":
            delta = params.get("delta", "")
            item_id = params.get("itemId", "")
            if delta and item_id:
                if item_id not in self._command_output:
                    self._command_output[item_id] = []
                self._command_output[item_id].append(delta)

        elif method == "thread/tokenUsage/updated":
            self._last_usage = params

        elif method == "codex/event/task_complete":
            # Codex-specific turn completion (alongside or instead of turn/completed)
            if self.turn_id is not None:
                self._complete_turn(is_error=False)

        elif method == "error":
            log(f"Codex error: {params}")

        elif method.startswith("codex/event/"):
            pass  # Ignore other codex-specific events

        # else: ignore unknown notifications

    def _handle_item_started(self, params: dict) -> None:
        """Translate item/started to tool_use notification."""
        item = params.get("item", {})
        item_type = item.get("type", "")
        item_id = item.get("id", "")

        if item_type == "commandExecution":
            # Prefer clean command from commandActions, fall back to full command
            actions = item.get("commandActions", [])
            cmd = actions[0].get("command", "") if actions else ""
            if not cmd:
                cmd = item.get("command", "")

            # Detect file-read/grep commands and convert to simplified tool
            detected = _detect_read_command(cmd)
            if detected:
                tool_name = detected.pop("_tool", "Read")
                if tool_name == "Read":
                    input_data = {"file_path": detected["path"]}
                    if detected.get("offset"):
                        input_data["offset"] = detected["offset"]
                    if detected.get("limit"):
                        input_data["limit"] = detected["limit"]
                elif tool_name == "Grep":
                    input_data = {"pattern": detected["pattern"]}
                    if detected.get("path"):
                        input_data["path"] = detected["path"]
                else:
                    input_data = detected
                send_notification("message", {
                    "type": "tool_use",
                    "id": item_id,
                    "name": tool_name,
                    "input": input_data,
                })
            else:
                send_notification("message", {
                    "type": "tool_use",
                    "id": item_id,
                    "name": "Bash",
                    "input": {"command": cmd},
                })
        elif item_type == "fileChange":
            # Codex sends changes[] with {path, kind:{type: add|update|delete}, diff}
            changes = item.get("changes", [])
            first = changes[0] if changes else {}
            filepath = first.get("path", item.get("filePath", ""))
            kind = (first.get("kind") or {}).get("type", "update")
            tool_name = "Write" if kind == "add" else "Edit"
            input_data = {"file_path": filepath}
            diff = first.get("diff")
            if diff:
                input_data["unified_diff"] = diff
            send_notification("message", {
                "type": "tool_use",
                "id": item_id,
                "name": tool_name,
                "input": input_data,
            })
        elif item_type == "mcpToolCall":
            tool_name = item.get("toolName", "")
            server = item.get("serverLabel", "")
            # Match Claude's MCP tool naming: mcp__server__tool
            if server and tool_name:
                name = f"mcp__{server}__{tool_name}"
            elif tool_name:
                name = tool_name
            else:
                name = server or "mcp"
            send_notification("message", {
                "type": "tool_use",
                "id": item_id,
                "name": name,
                "input": item.get("arguments", {}),
            })

    def _handle_item_completed(self, params: dict) -> None:
        """Translate item/completed to tool_result notification."""
        item = params.get("item", {})
        item_type = item.get("type", "")
        item_id = item.get("id", "")

        if item_type == "commandExecution":
            exit_code = item.get("exitCode", 0)
            output = item.get("output", "")
            if not output and item_id in self._command_output:
                output = "".join(self._command_output.pop(item_id))
            else:
                self._command_output.pop(item_id, None)
            send_notification("message", {
                "type": "tool_result",
                "tool_use_id": item_id,
                "content": output[:2000] if output else "(no output)",
                "is_error": exit_code != 0,
            })
        elif item_type == "fileChange":
            send_notification("message", {
                "type": "tool_result",
                "tool_use_id": item_id,
                "content": "File updated",
                "is_error": False,
            })
        elif item_type == "mcpToolCall":
            result = item.get("result", "")
            send_notification("message", {
                "type": "tool_result",
                "tool_use_id": item_id,
                "content": str(result)[:2000] if result else "(no result)",
                "is_error": bool(item.get("error")),
            })

    def _complete_turn(self, is_error: bool = False) -> None:
        """Send turn result notification and deferred query response."""
        duration = time.time() - self._turn_start_time if self._turn_start_time else 0
        result_msg = {
            "type": "result",
            "session_id": self.session_id,
            "duration_ms": int(duration * 1000),
            "is_error": is_error,
            "total_cost_usd": 0,
        }
        if self._last_usage:
            result_msg["usage"] = self._last_usage
        send_notification("message", result_msg)
        # Respond to the deferred query request — triggers _on_done in session.py
        if self._query_req_id is not None:
            send_result(self._query_req_id, {
                "ok": True,
                "session_id": self.session_id,
                "is_error": is_error,
                "duration_ms": int(duration * 1000),
            })
            self._query_req_id = None
        self.turn_id = None

    async def handle_codex_server_request(self, msg: dict) -> None:
        """Handle a request FROM codex TO us (approvals, user input)."""
        method = msg.get("method", "")
        params = msg.get("params", {})
        codex_req_id = msg["id"]

        if method in ("item/commandExecution/requestApproval", "execCommandApproval"):
            self.permission_counter += 1
            perm_id = self.permission_counter
            self.pending_approvals[perm_id] = codex_req_id

            command = params.get("command", "")
            # Also check commandActions for parsed info
            actions = params.get("commandActions", [])

            send_notification("permission_request", {
                "id": perm_id,
                "tool": "Bash",
                "input": {"command": command, "description": command[:80]},
            })

        elif method in ("item/fileChange/requestApproval", "applyPatchApproval"):
            self.permission_counter += 1
            perm_id = self.permission_counter
            self.pending_approvals[perm_id] = codex_req_id

            send_notification("permission_request", {
                "id": perm_id,
                "tool": "Edit",
                "input": {"file_path": params.get("grantRoot", ""), "reason": params.get("reason", "")},
            })

        elif method == "item/tool/requestUserInput":
            self.permission_counter += 1
            q_id = self.permission_counter
            self.pending_questions[q_id] = codex_req_id

            questions = params.get("questions", [])
            send_notification("question_request", {
                "id": q_id,
                "questions": questions,
            })

        elif method == "mcpServer/elicitation/request":
            # MCP tool approval (action-based response schema)
            log(f"Auto-accepting MCP elicitation: server={params.get('serverName')} kind={(params.get('_meta') or {}).get('codex_approval_kind')}")
            await self.codex_respond(codex_req_id, {"action": "accept", "content": {}})

        else:
            # Unknown server request - auto-accept (decision-based for most)
            log(f"Auto-accepting unknown server request: method={method} params={str(params)[:300]}")
            await self.codex_respond(codex_req_id, {"decision": "accept"})

    # ── Response tracking ───────────────────────────────────────────

    _pending_responses: dict[int, asyncio.Future] = {}

    async def _wait_for_response(self, req_id: int, timeout: float = 30) -> Any:
        """Wait for a response to a request we sent to codex."""
        future = asyncio.get_event_loop().create_future()
        self._pending_responses[req_id] = future
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            log(f"Timeout waiting for response to request {req_id}")
            return None
        finally:
            self._pending_responses.pop(req_id, None)

    # ── Main loop ───────────────────────────────────────────────────

    async def run(self) -> None:
        """Main loop: read from both stdin (Sublime) and codex stdout."""
        log("Bridge starting")

        loop = asyncio.get_event_loop()

        # Set up stdin reader (from Sublime/rpc.py)
        sublime_reader = asyncio.StreamReader(limit=1024 * 1024 * 100)
        protocol = asyncio.StreamReaderProtocol(sublime_reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        # Only start sublime reader — codex readers start after spawn
        await self._read_sublime(sublime_reader)

    async def _read_sublime(self, reader: asyncio.StreamReader) -> None:
        """Read JSON-RPC messages from Sublime."""
        while self.running:
            try:
                line = await reader.readline()
                if not line:
                    break
                req = json.loads(line.decode())
                asyncio.create_task(self.handle_request(req))
            except json.JSONDecodeError as e:
                send_error(None, -32700, f"Parse error: {e}")
            except Exception as e:
                log(f"Sublime reader error: {e}")
                send_error(None, -32000, str(e))

    async def _read_codex(self) -> None:
        """Read JSON-RPC messages from codex app-server stdout."""
        while self.running and self.codex_proc:
            try:
                line = await self.codex_proc.stdout.readline()
                if not line:
                    if self.running:
                        log("Codex process stdout closed")
                        if self.turn_id is not None:
                            self._complete_turn(is_error=True)
                    break
                msg = json.loads(line.decode())
                await self.handle_codex_message(msg)
            except json.JSONDecodeError:
                pass  # Skip non-JSON lines
            except Exception as e:
                log(f"Codex reader error: {e}")

    async def _read_codex_stderr(self) -> None:
        """Forward codex stderr to our stderr for debugging."""
        while self.running and self.codex_proc:
            try:
                line = await self.codex_proc.stderr.readline()
                if not line:
                    break
                log(f"codex: {line.decode().rstrip()}")
            except Exception:
                break


async def main():
    bridge = CodexBridge()
    # Fix: _pending_responses should be per-instance, not class-level
    bridge._pending_responses = {}
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())
