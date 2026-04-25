"""Shared dispatch + permission/question scaffolding for all bridges.

Each backend-specific bridge (Claude, Codex, Copilot) subclasses BaseBridge
and provides handle_initialize/handle_query/handle_interrupt/handle_shutdown.
Permission and question response handling are uniform across backends and live
on the base class.

Bridges that need more methods (e.g. Claude's plan_response, inject_message,
register_notification) override `extra_dispatch()` to extend the dispatch table.
"""
import asyncio
import json
import sys
from typing import Any, Awaitable, Callable, Dict, Optional

from rpc_helpers import send_error, send_result


HandlerFn = Callable[[Optional[int], dict], Awaitable[None]]


class BaseBridge:
    """Common bridge machinery: stdin loop + dispatch + permission/question state."""

    BACKEND_NAME: str = "base"  # Override in subclass for log prefixes

    def __init__(self) -> None:
        self.running = True
        # Permission state: id → asyncio.Future to be resolved by handle_permission_response
        self.pending_permissions: Dict[int, asyncio.Future] = {}
        self.pending_questions: Dict[int, asyncio.Future] = {}
        self.permission_id: int = 0
        self.question_id: int = 0
        # Track the active query RPC id so subclasses can finalize it on completion
        self._query_req_id: Optional[int] = None

    # ── Subclass override hooks ─────────────────────────────────────────

    async def handle_initialize(self, req_id: Optional[int], params: dict) -> None:
        raise NotImplementedError

    async def handle_query(self, req_id: Optional[int], params: dict) -> None:
        raise NotImplementedError

    async def handle_interrupt(self, req_id: Optional[int], params: dict) -> None:
        raise NotImplementedError

    async def handle_shutdown(self, req_id: Optional[int], params: dict) -> None:
        raise NotImplementedError

    def extra_dispatch(self) -> Dict[str, HandlerFn]:
        """Subclasses can return additional method → handler mappings here."""
        return {}

    def log(self, msg: str) -> None:
        """Emit a log line. Default: stderr with backend prefix."""
        sys.stderr.write(f"[{self.BACKEND_NAME}-bridge] {msg}\n")
        sys.stderr.flush()

    # ── Final dispatch loop ─────────────────────────────────────────────

    def _dispatch_table(self) -> Dict[str, HandlerFn]:
        base: Dict[str, HandlerFn] = {
            "initialize": self.handle_initialize,
            "query": self.handle_query,
            "interrupt": self.handle_interrupt,
            "permission_response": self._handle_permission_response,
            "question_response": self._handle_question_response,
            "shutdown": self.handle_shutdown,
        }
        base.update(self.extra_dispatch())
        return base

    async def handle_request(self, req: dict) -> None:
        method = req.get("method")
        rid = req.get("id")
        params = req.get("params", {})
        handler = self._dispatch_table().get(method)
        if handler is None:
            send_error(rid, -32601, f"Unknown method: {method}")
            return
        try:
            await handler(rid, params)
        except Exception as e:
            self.log(f"Error handling {method}: {e}")
            send_error(rid, -32000, str(e))

    # ── Permission/question response (uniform across backends) ──────────

    async def _handle_permission_response(self, req_id: Optional[int], params: dict) -> None:
        perm_id = params.get("id")
        allow = params.get("allow", False)
        future = self.pending_permissions.pop(perm_id, None)
        if future and not future.done():
            future.set_result({"kind": "approved"} if allow else {"kind": "denied-interactively-by-user"})
        send_result(req_id, {"ok": True})

    async def _handle_question_response(self, req_id: Optional[int], params: dict) -> None:
        q_id = params.get("id")
        answers = params.get("answers")
        future = self.pending_questions.pop(q_id, None)
        if future and not future.done():
            future.set_result(answers)
        send_result(req_id, {"ok": True})

    # ── Main stdin loop (used by all bridges) ───────────────────────────

    async def run_stdin_loop(self, buffer_size: int = 1024 * 1024 * 1024) -> None:
        reader = asyncio.StreamReader(limit=buffer_size)
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)

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
