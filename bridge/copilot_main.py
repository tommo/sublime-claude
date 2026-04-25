#!/usr/bin/env python3
"""
Bridge between Sublime Text and GitHub Copilot SDK.
Translates our JSON-RPC protocol to Copilot's event-driven SDK.
"""
import asyncio
import json
import os
import sys
import time
from typing import Any, Optional

# Pre-import event types (avoid per-event import overhead)
try:
    from copilot.generated.session_events import SessionEventType
except ImportError:
    SessionEventType = None


from rpc_helpers import send, send_error, send_result, send_notification

# ── Logging: stderr → plugin console; also append to shared bridge log file ──
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).parent.parent))
try:
    from logger import get_bridge_logger
    _file_log = get_bridge_logger()
except Exception:
    _file_log = None


def log(msg):
    line = f"[copilot-bridge] {msg}"
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    if _file_log is not None:
        try:
            _file_log.info(line)
        except Exception:
            pass  # benign: file logging is best-effort


class CopilotBridge:
    def __init__(self):
        self.client = None
        self.session = None
        self.running = True
        self.session_id = None
        self._query_req_id = None
        self._turn_start_time = 0
        self._session_config = {}
        self.permission_counter = 0
        self.pending_permissions = {}
        self.pending_questions = {}
        self._got_first_delta = False
        self._perm_data_cache = {}  # toolCallId → extra tool_input from permission

    async def handle_request(self, req: dict) -> None:
        method = req.get("method")
        params = req.get("params", {})
        req_id = req.get("id")

        try:
            if method == "initialize":
                await self.handle_initialize(req_id, params)
            elif method == "query":
                await self.handle_query(req_id, params)
            elif method == "interrupt":
                await self.handle_interrupt(req_id)
            elif method == "permission_response":
                await self.handle_permission_response(req_id, params)
            elif method == "question_response":
                await self.handle_question_response(req_id, params)
            elif method == "shutdown":
                await self.handle_shutdown(req_id)
            else:
                send_error(req_id, -32601, f"Unknown method: {method}")
        except Exception as e:
            log(f"Error handling {method}: {e}")
            send_error(req_id, -32000, str(e))

    async def handle_initialize(self, req_id: int, params: dict) -> None:
        from copilot import CopilotClient, PermissionHandler

        cwd = params.get("cwd", os.getcwd())
        model = params.get("model", "claude-sonnet-4-6")
        system_prompt = params.get("system_prompt", "")
        view_id = params.get("view_id", "")
        resume_id = params.get("resume")

        # Map Claude model names to Copilot models
        model_map = {"opus": "claude-opus-4-6", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5"}
        model = model_map.get(model, model)

        self.client = CopilotClient()
        await self.client.start()

        # Build session config
        config = {
            "model": model,
            "streaming": True,
            "on_permission_request": self._handle_permission,
            "working_directory": cwd,
        }
        if system_prompt:
            config["system_message"] = {"content": system_prompt}

        # Load global MCP config and add our sublime server
        import json as _json
        mcp_servers = {}
        global_mcp = os.path.expanduser("~/.copilot/mcp-config.json")
        if os.path.exists(global_mcp):
            try:
                with open(global_mcp) as f:
                    mcp_servers = _json.load(f).get("mcpServers", {})
            except Exception:
                pass
        mcp_server_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "mcp", "server.py"
        )
        if os.path.exists(mcp_server_path):
            mcp_args = [mcp_server_path]
            if view_id:
                mcp_args.append(f"--view-id={view_id}")
            mcp_servers["sublime"] = {"type": "local", "command": sys.executable, "args": mcp_args, "tools": ["*"]}
        if mcp_servers:
            config["mcp_servers"] = mcp_servers

        self._session_config = config
        log(f"Session config: model={model}, resume={resume_id}, mcp={bool(config.get('mcp_servers'))}")
        if resume_id:
            log(f"Resuming session {resume_id}, cwd={cwd}")
            try:
                self.session = await self.client.resume_session(resume_id, config)
            except Exception as e:
                log(f"Resume failed: {e}, falling back to create")
                self.session = await self.client.create_session(config)
        else:
            self.session = await self.client.create_session(config)
        self.session.on(self._on_event)
        self.session_id = getattr(self.session, 'session_id', None) or resume_id or f"copilot-{view_id}"

        log(f"Initialized: model={model}, cwd={cwd}, session_id={self.session_id}")
        send_result(req_id, {
            "session_id": self.session_id,
            "mcp_servers": ["sublime"] if os.path.exists(mcp_server_path) else [],
            "agents": [],
        })

    async def handle_query(self, req_id: int, params: dict) -> None:
        if not self.session:
            send_error(req_id, -32000, "Not initialized")
            return

        prompt = params.get("prompt", "")
        self._query_req_id = req_id
        self._turn_start_time = time.time()
        self._got_first_delta = False

        message_opts = {"prompt": prompt}
        # Images — copilot only supports file attachments, not inline base64
        images = params.get("images", [])
        if images:
            attachments = []
            for img in images:
                if isinstance(img, dict) and img.get("path"):
                    attachments.append({"type": "file", "path": img["path"]})
                elif isinstance(img, str) and img.startswith("/"):
                    attachments.append({"type": "file", "path": img})
                elif isinstance(img, dict) and img.get("data"):
                    # Save base64 to temp file for copilot
                    import base64, tempfile
                    mime = img.get("mime_type", "image/png")
                    ext = mime.split("/")[-1] if "/" in mime else "png"
                    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
                        f.write(base64.b64decode(img["data"]))
                        attachments.append({"type": "file", "path": f.name})
            if attachments:
                message_opts["attachments"] = attachments

        await self.session.send(message_opts)

    async def handle_interrupt(self, req_id: int) -> None:
        if self.session:
            try:
                await self.session.abort()
            except Exception as e:
                log(f"Interrupt error: {e}")
        # Complete the pending query RPC as interrupted
        if self._query_req_id is not None:
            send_result(self._query_req_id, {"status": "interrupted"})
            self._query_req_id = None
        # Also respond to the interrupt RPC itself
        send_result(req_id, {"status": "interrupted"})

    async def handle_permission_response(self, req_id: int, params: dict) -> None:
        perm_id = params.get("id")
        allow = params.get("allow", False)
        future = self.pending_permissions.pop(perm_id, None)
        if future and not future.done():
            if allow:
                future.set_result({"kind": "approved"})
            else:
                future.set_result({"kind": "denied-interactively-by-user"})
        send_result(req_id, {"ok": True})

    async def handle_question_response(self, req_id: int, params: dict) -> None:
        q_id = params.get("id")
        answers = params.get("answers")
        future = self.pending_questions.pop(q_id, None)
        if future and not future.done():
            future.set_result(answers)
        send_result(req_id, {"ok": True})

    async def handle_shutdown(self, req_id: int) -> None:
        self.running = False
        if self.session:
            try:
                await self.session.destroy()
            except Exception:
                pass
        if self.client:
            try:
                await self.client.stop()
            except Exception:
                pass
        send_result(req_id, {"ok": True})

    async def _handle_permission(self, request, invocation=None) -> dict:
        """Permission callback — send to Sublime, wait for response."""
        self.permission_counter += 1
        perm_id = self.permission_counter
        future = asyncio.get_running_loop().create_future()
        self.pending_permissions[perm_id] = future

        # Extract human-readable tool info
        req = request if isinstance(request, dict) else vars(request) if hasattr(request, '__dict__') else {"raw": str(request)}
        kind = req.get("kind", "tool")
        intention = req.get("intention", "")
        path = req.get("path") or req.get("fileName") or ""

        if kind == "shell":
            tool_name = "Bash"
            cmd = req.get("fullCommandText", "") or ""
            tool_input = {"command": cmd, "description": intention or cmd[:80]}
        elif kind == "write":
            tool_name = "Edit"
            diff = req.get("diff", "")
            # Extract line number and old/new from diff
            import re as _re
            old_lines = []
            new_lines = []
            line_num = None
            for line in (diff or "").splitlines():
                hunk = _re.match(r'^@@ -(\d+)', line)
                if hunk and line_num is None:
                    line_num = int(hunk.group(1))
                elif line.startswith("-") and not line.startswith("---"):
                    old_lines.append(line[1:])
                elif line.startswith("+") and not line.startswith("+++"):
                    new_lines.append(line[1:])
            file_ref = f"{path}:{line_num}" if line_num else path
            tool_input = {
                "file_path": file_ref,
                "old_string": "\n".join(old_lines[:10]),
                "new_string": "\n".join(new_lines[:10]),
            }
            # Cache for TOOL_EXECUTION_START to pick up
            tool_call_id = req.get("toolCallId", "")
            if tool_call_id:
                self._perm_data_cache[tool_call_id] = tool_input
        elif kind == "read":
            tool_name = "Read"
            tool_input = {"file_path": path, "description": intention}
        elif kind == "mcp":
            tool_name = "MCP"
            tool_input = {"description": intention}
        elif kind == "url":
            tool_name = "Fetch"
            tool_input = {"description": intention}
        else:
            tool_name = kind.title()
            tool_input = {"description": intention or str(req)[:100]}

        send_notification("permission_request", {
            "id": perm_id,
            "tool": tool_name,
            "input": tool_input,
        })

        result = await future
        return result

    def _on_event(self, event) -> None:
        """Handle Copilot session events."""
        etype = event.type
        data = event.data

        if etype == SessionEventType.ASSISTANT_MESSAGE_DELTA:
            text = getattr(data, 'delta_content', '') or ''
            if text:
                # Strip leading newlines from first delta of a turn
                if not self._got_first_delta:
                    text = text.lstrip('\n')
                    self._got_first_delta = True
                if text:
                    send_notification("message", {"type": "text_delta", "text": text})

        elif etype == SessionEventType.ASSISTANT_MESSAGE:
            # Text already streamed via ASSISTANT_MESSAGE_DELTA — skip
            pass

        elif etype == SessionEventType.ASSISTANT_REASONING_DELTA:
            text = getattr(data, 'delta_content', '') or ''
            if text:
                send_notification("message", {"type": "thinking", "thinking": text})

        elif etype == SessionEventType.TOOL_EXECUTION_START:
            tool_name = getattr(data, 'tool_name', '') or ''
            mcp_server = getattr(data, 'mcp_server_name', '') or ''
            mcp_tool = getattr(data, 'mcp_tool_name', '') or ''
            tool_call_id = getattr(data, 'tool_call_id', '') or ''
            args = getattr(data, 'arguments', None) or {}
            if not isinstance(args, dict):
                args = {}
            # Map copilot tool names to Claude-style names for unified rendering
            name_map = {"view": "Read", "edit": "Edit", "create": "Write",
                        "bash": "Bash", "shell": "Bash", "glob": "Glob", "grep": "Grep",
                        "sql": "Bash", "report_intent": "Task"}
            name = name_map.get(tool_name, tool_name)
            if mcp_server and mcp_tool:
                name = f"mcp__{mcp_server}__{mcp_tool}"
            # Map copilot arg keys to Claude-style keys for _format_tool_detail
            tool_input = dict(args)
            if "path" in tool_input and "file_path" not in tool_input:
                tool_input["file_path"] = tool_input.pop("path")
            if "old_str" in tool_input and "old_string" not in tool_input:
                tool_input["old_string"] = tool_input.pop("old_str")
            if "new_str" in tool_input and "new_string" not in tool_input:
                tool_input["new_string"] = tool_input.pop("new_str")
            if name == "Bash" and "command" not in tool_input:
                tool_input["command"] = tool_input.get("intent", str(args)[:80])
            # Merge cached permission data (e.g. diff from write approval)
            cached = self._perm_data_cache.pop(tool_call_id, None)
            if cached:
                tool_input.update(cached)
            send_notification("message", {"type": "tool_use", "id": tool_call_id, "name": name, "input": tool_input})

        elif etype == SessionEventType.TOOL_EXECUTION_COMPLETE:
            tool_call_id = getattr(data, 'tool_call_id', '') or ''
            tool_name = getattr(data, 'tool_name', '') or ''
            name_map = {"view": "Read", "edit": "Edit", "create": "Write",
                        "bash": "Bash", "shell": "Bash", "glob": "Glob", "grep": "Grep",
                        "sql": "Bash", "report_intent": "Task"}
            name = name_map.get(tool_name, tool_name)
            result = getattr(data, 'result', None)
            content = ""
            is_error = False
            if result:
                content = getattr(result, 'output', '') or getattr(result, 'content', '') or str(result)[:500]
                is_error = bool(getattr(result, 'is_error', False) or getattr(result, 'error', None))
            send_notification("message", {
                "type": "tool_result",
                "tool_use_id": tool_call_id or name,
                "content": str(content)[:500],
                "is_error": is_error,
            })

        elif etype == SessionEventType.SESSION_IDLE:
            if self._query_req_id is not None:
                dur = (time.time() - self._turn_start_time) * 1000
                send_notification("message", {
                    "type": "result",
                    "session_id": self.session_id,
                    "duration_ms": dur,
                    "is_error": False,
                    "num_turns": 1,
                    "total_cost_usd": 0,
                })
                send_result(self._query_req_id, {"status": "complete"})
                self._query_req_id = None

        elif etype == SessionEventType.SESSION_ERROR:
            error = getattr(data, 'message', '') or str(data)
            log(f"Session error: {error}")

        elif etype == SessionEventType.ASSISTANT_USAGE:
            usage = {}
            if hasattr(data, 'input_tokens'):
                usage["input_tokens"] = data.input_tokens
            if hasattr(data, 'output_tokens'):
                usage["output_tokens"] = data.output_tokens
            if usage:
                send_notification("message", {"type": "turn_usage", "usage": usage})


async def main():
    bridge = CopilotBridge()
    log("Bridge started")

    # Read JSON-RPC from stdin (1GB limit to match Claude bridge)
    reader = asyncio.StreamReader(limit=1024 * 1024 * 1024)
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    while bridge.running:
        try:
            line = await reader.readline()
            if not line:
                break
            req = json.loads(line.decode().strip())
            await bridge.handle_request(req)
        except json.JSONDecodeError:
            continue
        except Exception as e:
            log(f"Main loop error: {e}")

    log("Bridge stopped")


if __name__ == "__main__":
    asyncio.run(main())
