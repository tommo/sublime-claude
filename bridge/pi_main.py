#!/usr/bin/env python3
"""
Bridge process between Sublime Text and pi coding agent (RPC mode).
Communicates via JSON-RPC over stdio with Sublime, spawns `pi --mode rpc` as subprocess.

pi RPC protocol: https://pi.dev/docs/rpc
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from rpc_helpers import send, send_error, send_result, send_notification

os.environ["PI_OFFLINE"] = "1"


class JsonlStreamReader:
    """Line reader that splits on LF only — pi RPC compliant.

    Pi RPC uses strict LF-delimited JSONL. We must NOT use readline
    (which splits on Unicode separators inside JSON). Instead, we buffer
    and split on \\n manually, stripping an optional trailing \\r.
    """

    def __init__(self, reader: asyncio.StreamReader):
        self._reader = reader
        self._buf = b""

    async def read_line(self) -> bytes | None:
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                line = self._buf[:idx]
                self._buf = self._buf[idx + 1:]
                if line.endswith(b"\r"):
                    line = line[:-1]
                return line
            chunk = await self._reader.read(4096)
            if not chunk:
                if self._buf:
                    line = self._buf
                    self._buf = b""
                    if line.endswith(b"\r"):
                        line = line[:-1]
                    return line
                return None
            self._buf += chunk


class PiBridge:
    """Bridge from sublime-claude JSON-RPC to pi RPC mode."""

    BACKEND_NAME = "pi"

    def __init__(self):
        self.running = True
        self._proc: asyncio.subprocess.Process | None = None
        self._pi_stdin: asyncio.StreamWriter | None = None
        self._pi_reader: JsonlStreamReader | None = None
        self._listen_task: asyncio.Task | None = None
        self._stderr_drain_task: asyncio.Task | None = None
        self._query_rid: int | None = None
        self._query_done: asyncio.Future | None = None
        self._current_tool_id: str | None = None
        self._pending_permissions: dict[int, asyncio.Future] = {}
        self._pending_questions: dict[int, asyncio.Future] = {}
        self._permission_id: int = 0
        self._question_id: int = 0
        # Guard: once finalize_query fires, agent_end must not fire a second result
        self._query_finalized: bool = False

    def log(self, msg: str) -> None:
        sys.stderr.write(f"[pi-bridge] {msg}\n")
        sys.stderr.flush()

    async def _spawn_pi(self, cwd: str | None = None,
                        session_file: str | None = None) -> None:
        """Spawn pi --mode rpc subprocess."""
        pi_cmd = self._find_pi()
        if not pi_cmd:
            raise RuntimeError(
                "pi not found. Install: npm install -g @earendil-works/pi-coding-agent"
            )

        args = [pi_cmd, "--mode", "rpc", "--no-session"]
        if session_file:
            args.extend(["--session", session_file])

        self.log(f"spawning: {' '.join(args)} cwd={cwd or os.getcwd()}")

        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or os.getcwd(),
        )
        self._pi_stdin = self._proc.stdin
        self._pi_reader = JsonlStreamReader(self._proc.stdout)
        self._listen_task = asyncio.create_task(self._listen_pi_events())
        self._stderr_drain_task = asyncio.create_task(self._drain_stderr())
        self.log("pi subprocess started")

    def _find_pi(self) -> str | None:
        """Locate the pi CLI binary."""
        import shutil
        bun_pi = os.path.expanduser(
            "~/.bun/install/global/node_modules/.bin/pi"
        )
        if os.path.isfile(bun_pi):
            return bun_pi
        for p in ("/opt/homebrew/bin/pi",
                  "/usr/local/bin/pi"):
            if os.path.isfile(p):
                return p
        return shutil.which("pi")

    def _send_pi(self, msg: dict) -> None:
        """Send a JSON command to pi's stdin (strict JSONL = LF only)."""
        if self._pi_stdin:
            payload = json.dumps(msg, ensure_ascii=False) + "\n"
            self._pi_stdin.write(payload.encode("utf-8"))

    async def _drain_stderr(self) -> None:
        """Continuously drain pi's stderr to prevent pipe buffer blocking."""
        try:
            while self.running and self._proc and self._proc.stderr:
                chunk = await self._proc.stderr.read(4096)
                if not chunk:
                    break
        except (asyncio.CancelledError, Exception):
            pass

    # ── Pi RPC event listener ──────────────────────────────────────────

    async def _listen_pi_events(self) -> None:
        """Continuously read stdout events from pi and dispatch them."""
        try:
            while self.running and self._pi_reader:
                line = await self._pi_reader.read_line()
                if line is None:
                    self.log("pi stdout closed")
                    self._handle_pi_disconnect()
                    break
                try:
                    event = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")
                if etype == "response":
                    await self._handle_pi_response(event)
                else:
                    await self._handle_pi_event(event)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log(f"event listener error: {e}")
            self._handle_pi_disconnect()

    def _handle_pi_disconnect(self) -> None:
        """Pi process died unexpectedly — resolve any hanging query."""
        if self._query_done and not self._query_done.done():
            self._query_finalized = True
            send_notification("message", {
                "type": "result",
                "session_id": "",
                "duration_ms": 0,
                "is_error": True,
                "num_turns": 1,
                "total_cost_usd": 0,
            })
            send_error(self._query_rid, -32000, "pi process disconnected unexpectedly")
            self._query_done.set_result(None)
            self._query_rid = None

    async def _handle_pi_response(self, msg: dict) -> None:
        """Handle a response to one of our commands."""
        cmd = msg.get("command")
        success = msg.get("success", False)
        if not success:
            error = msg.get("error", "unknown error")
            self.log(f"pi command {cmd} failed: {error}")
            # Prompt rejection means we'll never get agent_end — finalize now
            if cmd == "prompt" and not self._query_finalized:
                self._query_finalized = True
                self._finalize_query(interrupted=True)

    async def _handle_pi_event(self, event: dict) -> None:
        """Translate pi RPC events into sublime notifications."""
        etype = event.get("type")

        if etype == "message_update":
            await self._handle_message_update(event)

        elif etype == "tool_execution_start":
            tid = event.get("toolCallId", "")
            name = event.get("toolName", "")
            args = event.get("args", {})
            display = {"read": "Read", "write": "Write", "edit": "Edit",
                       "bash": "Bash", "grep": "Grep", "glob": "Glob",
                       "find": "Find", "ls": "Ls"}.get(name, name)
            self._current_tool_id = tid
            send_notification("message", {
                "type": "tool_use",
                "id": tid, "name": display, "input": args,
            })

        elif etype == "tool_execution_update":
            # Progressive output from pi — suppress here so tool_execution_end
            # is the single canonical result event. pi's partialResult already
            # contains the full accumulated output, so emitting on each update
            # just causes duplicate result rendering in the output view.
            pass

        elif etype == "tool_execution_end":
            result = event.get("result", {})
            text = self._extract_tool_text(result.get("content", []))
            is_error = event.get("isError", False)
            send_notification("message", {
                "type": "tool_result",
                "tool_use_id": event.get("toolCallId", ""),
                "content": self._normalize_tool_content(text),
                "is_error": is_error,
            })
            self._current_tool_id = None

        elif etype == "agent_end":
            if not self._query_finalized:
                self._query_finalized = True
                # Send result notification BEFORE send_result so _on_msg_result
                # calls output.meta() which sets current.working=False —
                # without this, enter_input_mode() silently aborts.
                send_notification("message", {
                    "type": "result",
                    "session_id": "",
                    "duration_ms": 0,
                    "is_error": False,
                    "num_turns": 1,
                    "total_cost_usd": 0,
                })
                self._finalize_query()

        elif etype == "compaction_start":
            send_notification("message", {
                "type": "system", "subtype": "compaction",
                "data": {"message": "Compacting context..."},
            })

        elif etype == "extension_ui_request":
            await self._handle_extension_ui(event)

    @staticmethod
    def _extract_tool_text(content: list) -> str:
        """Pull plain text from pi's content block list (list of {"type": "text", "text": ...})."""
        return "".join(
            c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )

    @staticmethod
    def _normalize_tool_content(text: str) -> str:
        """Normalize tool output to match other bridges.

        Mirrors session.py _on_msg_tool_result: caps at 10,000 chars and always
        returns a non-empty string so the output view has something to display.
        """
        if not text:
            return "(no output)"
        if len(text) > 10000:
            text = text[:10000]
        return text

    def _finalize_query(self, interrupted: bool = False) -> None:
        """Complete the active query by resolving the future."""
        if self._query_done and not self._query_done.done():
            send_result(self._query_rid, {
                "status": "interrupted" if interrupted else "complete",
            })
            self._query_done.set_result(None)
        self._query_rid = None
        self._query_done = None

    async def _handle_message_update(self, event: dict) -> None:
        """Handle streaming message content from pi.

        Pi RPC sends BOTH toolcall_start/end (streaming) AND
        tool_execution_start/end (actual run). We only emit tool_use from
        tool_execution_start to avoid duplicates. toolcall events from
        streaming are just the model building the tool call — the real
        execution event has the canonical toolCallId and full args.
        """
        msg_ev = event.get("assistantMessageEvent", {})
        stype = msg_ev.get("type")

        if stype == "text_delta":
            delta = msg_ev.get("delta", "")
            if delta:
                send_notification("message", {
                    "type": "text_delta", "text": delta,
                })
        elif stype == "thinking_delta":
            thinking = msg_ev.get("delta", "")
            if thinking:
                send_notification("message", {
                    "type": "thinking", "thinking": thinking,
                })
        # toolcall_start / toolcall_end are suppressed — real tool use is
        # emitted by tool_execution_start which fires with the canonical id.
        elif stype == "toolcall_start":
            # Track the tool id for continuity on toolcall_end
            tc = msg_ev.get("partial", {}).get("toolCall", {})
            if tc.get("id"):
                self._current_tool_id = tc["id"]
        elif stype == "toolcall_end":
            pass

    async def _handle_extension_ui(self, event: dict) -> None:
        """Handle extension UI requests from pi (permissions, questions)."""
        method = event.get("method")
        req_id = event.get("id")

        if method == "confirm":
            self._permission_id += 1
            pid = self._permission_id
            future = asyncio.get_event_loop().create_future()
            self._pending_permissions[pid] = future
            title = event.get("title", "Allow action?")
            send_notification("permission_request", {
                "id": pid, "tool": "Pi",
                "input": {"question": title},
            })
            try:
                allowed = await asyncio.wait_for(future, timeout=3600)
                self._send_pi({
                    "type": "extension_ui_response",
                    "id": req_id, "confirmed": bool(allowed),
                })
            except asyncio.TimeoutError:
                self._send_pi({
                    "type": "extension_ui_response",
                    "id": req_id, "cancelled": True,
                })
            finally:
                self._pending_permissions.pop(pid, None)

        elif method == "select":
            self._permission_id += 1
            pid = self._permission_id
            future = asyncio.get_event_loop().create_future()
            self._pending_permissions[pid] = future
            title = event.get("title", "Select option")
            options = event.get("options", [])
            send_notification("permission_request", {
                "id": pid, "tool": "Pi",
                "input": {"question": title, "options": options},
            })
            try:
                allowed = await asyncio.wait_for(future, timeout=3600)
                if allowed and options:
                    self._send_pi({
                        "type": "extension_ui_response",
                        "id": req_id, "value": options[0],
                    })
                else:
                    self._send_pi({
                        "type": "extension_ui_response",
                        "id": req_id, "cancelled": True,
                    })
            except asyncio.TimeoutError:
                self._send_pi({
                    "type": "extension_ui_response",
                    "id": req_id, "cancelled": True,
                })
            finally:
                self._pending_permissions.pop(pid, None)

        elif method == "input":
            self._question_id += 1
            qid = self._question_id
            future = asyncio.get_event_loop().create_future()
            self._pending_questions[qid] = future
            title = event.get("title", "Input required")
            send_notification("question_request", {
                "id": qid, "questions": [{"question": title}],
            })
            try:
                answers = await asyncio.wait_for(future, timeout=3600)
                self._send_pi({
                    "type": "extension_ui_response",
                    "id": req_id,
                    "value": answers if answers else "",
                })
            except asyncio.TimeoutError:
                self._send_pi({
                    "type": "extension_ui_response",
                    "id": req_id, "cancelled": True,
                })
            finally:
                self._pending_questions.pop(qid, None)

        elif method == "editor":
            # Open a multi-line text input — forward to Sublime as question
            self._question_id += 1
            qid = self._question_id
            future = asyncio.get_event_loop().create_future()
            self._pending_questions[qid] = future
            title = event.get("title", "Edit text")
            prefill = event.get("prefill", "")
            send_notification("question_request", {
                "id": qid, "questions": [{"question": title, "prefill": prefill}],
            })
            try:
                value = await asyncio.wait_for(future, timeout=3600)
                self._send_pi({
                    "type": "extension_ui_response",
                    "id": req_id,
                    "value": value if value else prefill,
                })
            except asyncio.TimeoutError:
                self._send_pi({
                    "type": "extension_ui_response",
                    "id": req_id, "cancelled": True,
                })
            finally:
                self._pending_questions.pop(qid, None)

        elif method == "notify":
            # Fire-and-forget: display as system message, not permission prompt
            msg_text = event.get("message", "")
            notify_type = event.get("notifyType", "info")
            send_notification("message", {
                "type": "system",
                "subtype": f"notify-{notify_type}",
                "data": {"message": msg_text},
            })
            self._send_pi({
                "type": "extension_ui_response", "id": req_id,
            })

        elif method == "setStatus" or method == "setTitle":
            # Fire-and-forget: forward status/setTitle as system messages
            status_text = event.get("statusText", event.get("title", ""))
            if status_text:
                send_notification("message", {
                    "type": "system",
                    "subtype": "status",
                    "data": {"message": f"[{method}] {status_text}"},
                })
            self._send_pi({
                "type": "extension_ui_response", "id": req_id,
            })

        else:
            # Unknown method: acknowledge to avoid blocking the extension
            self._send_pi({
                "type": "extension_ui_response", "id": req_id,
            })

    # ── Sublime JSON-RPC handlers ──────────────────────────────────────

    async def handle_request(self, req: dict) -> None:
        """Dispatch incoming sublime JSON-RPC requests."""
        mid = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        try:
            if method == "initialize":
                await self._handle_initialize(mid, params)
            elif method == "query":
                await self._handle_query(mid, params)
            elif method == "interrupt":
                await self._handle_interrupt(mid)
            elif method == "shutdown":
                await self._handle_shutdown(mid)
            elif method == "permission_response":
                await self._handle_permission_response(mid, params)
            elif method == "question_response":
                await self._handle_question_response(mid, params)
            elif method == "get_history":
                send_result(mid, {"messages": []})
            elif method == "inject_message":
                await self._handle_inject_message(mid, params)
            elif method == "register_notification":
                send_result(mid, {"ok": True})
            elif method == "discover_services":
                send_result(mid, {"services": []})
            elif method == "set_model":
                send_result(mid, {"ok": True})
            elif method == "list_notifications":
                send_result(mid, {"notifications": []})
            elif method == "poll_bg_tasks":
                send_result(mid, {"pending": 0, "checked": 0})
            else:
                send_error(mid, -32601, f"Method not found: {method}")
        except Exception as e:
            self.log(f"Error handling {method}: {e}")
            send_error(mid, -32000, str(e))

    async def _handle_initialize(self, rid: int | None, params: dict) -> None:
        """Initialize: spawn pi subprocess."""
        cwd = params.get("cwd") or os.getcwd()
        resume_id = params.get("resume")
        await self._spawn_pi(cwd=cwd, session_file=resume_id)
        send_result(rid, {
            "status": "initialized",
            "session_id": resume_id or "pi-session",
            "mcp_servers": [], "agents": [],
        })

    async def _handle_query(self, rid: int | None, params: dict) -> None:
        """Send a prompt to pi — returns immediately; completion via notification.

        Spawns a background task that sends the prompt and waits for agent_end.
        The stdin loop stays responsive so interrupt/shutdown can be dispatched.
        """
        prompt = params.get("prompt", "")
        if not prompt:
            send_error(rid, -32602, "Missing prompt")
            return

        self._query_finalized = False
        self._query_rid = rid
        self._query_done = asyncio.get_event_loop().create_future()

        cmd: dict = {"type": "prompt", "message": prompt}
        images = params.get("images", [])
        if images:
            cmd["images"] = [
                {"type": "image", "data": img["data"],
                 "mimeType": img["mime_type"]}
                for img in images
            ]

        self._send_pi(cmd)

        # Run completion wait in background so main loop stays responsive
        asyncio.create_task(self._wait_for_query_done())

    async def _wait_for_query_done(self) -> None:
        """Background task: wait until agent_end or timeout resolves the future."""
        try:
            await asyncio.wait_for(self._query_done, timeout=7200)
        except asyncio.TimeoutError:
            self.log("query timed out after 2 hours")
            if not self._query_finalized:
                self._query_finalized = True
                self._finalize_query(interrupted=True)

    async def _handle_inject_message(self, rid: int | None, params: dict) -> None:
        """Inject a message mid-query (queue it to pi as steer)."""
        message = params.get("message", "")
        if not message:
            send_error(rid, -32602, "Missing message")
            return
        self._send_pi({"type": "steer", "message": message})
        send_result(rid, {"status": "ok"})

    async def _handle_interrupt(self, rid: int | None) -> None:
        """Interrupt current query."""
        self._send_pi({"type": "abort"})
        if not self._query_finalized:
            self._query_finalized = True
            self._finalize_query(interrupted=True)
        send_result(rid, {"status": "interrupted"})

    async def _handle_shutdown(self, rid: int | None) -> None:
        """Shutdown: kill pi process cleanly."""
        self.running = False

        # Cancel event listeners first so they don't race with process kill
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._stderr_drain_task:
            self._stderr_drain_task.cancel()
            try:
                await self._stderr_drain_task
            except asyncio.CancelledError:
                pass

        if self._proc and self._proc.returncode is None:
            try:
                self._send_pi({"type": "abort"})
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError, BrokenPipeError):
                try:
                    self._proc.kill()
                    await self._proc.wait()
                except (ProcessLookupError, Exception):
                    pass

        send_result(rid, {"status": "shutdown"})

    async def _handle_permission_response(self, rid: int | None,
                                          params: dict) -> None:
        pid = params.get("id")
        allow = params.get("allow", False)
        future = self._pending_permissions.pop(pid, None)
        if future and not future.done():
            future.set_result(allow)
        send_result(rid, {"status": "ok"})

    async def _handle_question_response(self, rid: int | None,
                                        params: dict) -> None:
        qid = params.get("id")
        answers = params.get("answers")
        future = self._pending_questions.pop(qid, None)
        if future and not future.done():
            future.set_result(answers)
        send_result(rid, {"status": "ok"})

    # ── Main stdin loop ────────────────────────────────────────────────

    async def run_stdin_loop(self, buffer_size: int = 1024 * 1024 * 1024) -> None:
        """Read JSON-RPC requests from stdin (sublime)."""
        reader = asyncio.StreamReader(limit=buffer_size)
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(
            lambda: protocol, sys.stdin
        )

        while self.running:
            try:
                line = await reader.readline()
                if not line:
                    break
                req = json.loads(line.decode().strip())
                await self.handle_request(req)
            except json.JSONDecodeError:
                continue
            except Exception as e:
                self.log(f"Main loop error: {e}")


async def main():
    bridge = PiBridge()
    await bridge.run_stdin_loop()


if __name__ == "__main__":
    asyncio.run(main())
