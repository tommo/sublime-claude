"""Shared dispatch + permission/question/plan scaffolding for all bridges.

Each backend-specific bridge (Claude, Codex, Copilot) subclasses BaseBridge
and provides handle_initialize/handle_query/handle_interrupt/handle_shutdown.
Permission, question, and plan-approval response handling are uniform and live
on the base class (or as free helpers for bridges that do not subclass).

Bridges that need more methods (e.g. inject_message, register_notification)
override `extra_dispatch()` to extend the dispatch table.
"""
import asyncio
import json
import sys
from typing import Any, Awaitable, Callable, Dict, Optional

from rpc_helpers import send_error, send_notification, send_result


HandlerFn = Callable[[Optional[int], dict], Awaitable[None]]


async def request_plan_approval(
    host: Any,
    tool_input: dict,
    *,
    timeout: float = 3600,
) -> Optional[dict]:
    """Show plan approval UI and wait for plugin `plan_response`.

    Shared by Claude (`ExitPlanMode` permission) and ACP (`_x.ai/exit_plan_mode`).
    `host` must expose `plan_id: int` and `pending_plan_approvals: dict`.

    Returns a dict (or None on cancel/timeout):
      {
        "approved": True | False | None,
        "plan": str,           # plan file content at response time (user edits)
        "planFilePath": str,
      }
    Claude Code sends the current plan file back on ExitPlanMode allow so
    agent sees any user edits made during the approval UI.
    """
    host.plan_id = int(getattr(host, "plan_id", 0)) + 1
    pid = host.plan_id
    pending = host.pending_plan_approvals
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    pending[pid] = fut
    send_notification("plan_mode_exit", {
        "id": pid,
        "tool_input": tool_input or {},
    })
    try:
        result = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None
    finally:
        pending.pop(pid, None)
    if result is None:
        return None
    if isinstance(result, dict):
        return result
    # Backward compat: bare bool/None from older callers
    return {
        "approved": result,
        "plan": (tool_input or {}).get("plan") or "",
        "planFilePath": (tool_input or {}).get("planFilePath") or "",
    }


def resolve_plan_response(host: Any, params: dict) -> Any:
    """Resolve a pending plan-approval future from plugin params.

    Plugin should send:
      {id, approved, plan?, planFilePath?}
    Future result is that full params dict (minus id).
    """
    pid = params.get("id")
    pending = getattr(host, "pending_plan_approvals", None) or {}
    fut = pending.pop(pid, None) if pid is not None else None
    payload = {
        "approved": params.get("approved"),
        "plan": params.get("plan") or "",
        "planFilePath": params.get("planFilePath") or params.get("plan_file") or "",
    }
    if fut is not None and not fut.done():
        fut.set_result(payload)
    return payload


class BaseBridge:
    """Common bridge machinery: stdin loop + dispatch + permission/question/plan state."""

    BACKEND_NAME: str = "base"  # Override in subclass for log prefixes

    def __init__(self) -> None:
        self.running = True
        # Permission state: id → asyncio.Future to be resolved by handle_permission_response
        self.pending_permissions: Dict[int, asyncio.Future] = {}
        self.pending_questions: Dict[int, asyncio.Future] = {}
        self.pending_plan_approvals: Dict[int, asyncio.Future] = {}
        self.permission_id: int = 0
        self.question_id: int = 0
        self.plan_id: int = 0
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
            "plan_response": self.handle_plan_response,
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
        # always=True when user chose "Always" (allow_always) in the plugin UI.
        always = bool(params.get("always", False))
        future = self.pending_permissions.pop(perm_id, None)
        if future and not future.done():
            if allow:
                future.set_result({
                    "kind": "approved",
                    "always": always,
                })
            else:
                future.set_result({
                    "kind": "denied-interactively-by-user",
                    "always": always,
                })
        send_result(req_id, {"ok": True})

    async def _handle_question_response(self, req_id: Optional[int], params: dict) -> None:
        q_id = params.get("id")
        answers = params.get("answers")
        future = self.pending_questions.pop(q_id, None)
        if future and not future.done():
            future.set_result(answers)
        send_result(req_id, {"ok": True})

    async def handle_plan_response(self, req_id: Optional[int], params: dict) -> None:
        """Plugin answered plan approval — wake awaiters. Subclasses may override
        to also switch agent mode (ACP)."""
        resolve_plan_response(self, params)
        send_result(req_id, {"ok": True, "approved": params.get("approved")})

    async def request_plan_approval(
        self, tool_input: dict, *, timeout: float = 3600,
    ) -> Optional[bool]:
        return await request_plan_approval(self, tool_input, timeout=timeout)

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
                # Concurrent dispatch (matches Claude/Codex bridges). Long-running
                # handlers like `query` must not block stdin: while a query awaits
                # the agent, Sublime still needs to deliver permission_response /
                # interrupt / question_response. Serial await deadlocks ACP agents
                # that gate tools on session/request_permission.
                asyncio.create_task(self.handle_request(req))
            except json.JSONDecodeError:
                continue
            except Exception as e:
                self.log(f"Main loop error: {e}")
