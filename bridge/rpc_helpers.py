"""Shared JSON-RPC helpers for bridge subprocesses. Sends to stdout.

Plugin-bound stdout MUST NOT block the asyncio event loop. If Sublime is
slow to read the bridge pipe, a sync write/flush freezes the loop → agent
stdout is no longer drained → agent blocks on its write → deadlock.

Writes go through a dedicated daemon thread + queue so the loop only
enqueues. Oversized messages are rejected (not silently truncated).
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
from typing import Any, Optional

# Max NDJSON line we will enqueue toward the plugin (bytes of UTF-8).
_PLUGIN_MSG_MAX = int(
    os.environ.get("SUBLIME_CLAUDE_PLUGIN_MSG_MAX", str(4 * 1024 * 1024)))
# Bound backlog so a stuck Sublime cannot grow RAM forever.
_PLUGIN_Q_MAX = int(os.environ.get("SUBLIME_CLAUDE_PLUGIN_Q_MAX", "256"))

_out_q: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=max(16, _PLUGIN_Q_MAX))
_writer_lock = threading.Lock()
_writer_started = False
_drop_log_lock = threading.Lock()
_drops = 0


def _ensure_writer() -> None:
    global _writer_started
    with _writer_lock:
        if _writer_started:
            return
        _writer_started = True
        t = threading.Thread(target=_stdout_writer, name="bridge-stdout", daemon=True)
        t.start()


def _stdout_writer() -> None:
    while True:
        line = _out_q.get()
        if line is None:
            break
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except Exception:
            pass


def _log_drop(reason: str) -> None:
    global _drops
    with _drop_log_lock:
        _drops += 1
        n = _drops
    try:
        sys.stderr.write(f"[bridge-stdout] drop #{n}: {reason}\n")
        sys.stderr.flush()
    except Exception:
        pass


def send(msg: dict) -> None:
    """Enqueue one NDJSON message to the plugin (non-blocking for asyncio).

    Raises nothing to callers — oversized / full-queue messages are dropped
    after a stderr note so the agent event loop never stalls.
    """
    _ensure_writer()
    try:
        line = json.dumps(msg, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as e:
        _log_drop(f"json encode failed: {e}")
        return
    if len(line.encode("utf-8", errors="replace")) > max(4096, _PLUGIN_MSG_MAX):
        # Fail closed: do not push multi‑MB lines to Sublime.
        mid = msg.get("id")
        method = msg.get("method")
        _log_drop(
            f"message too large ({len(line)} chars) "
            f"id={mid!r} method={method!r} max={_PLUGIN_MSG_MAX}")
        # Best-effort tiny substitute so UI can show failure for requests.
        if mid is not None and "result" in msg:
            line = json.dumps({
                "jsonrpc": "2.0",
                "id": mid,
                "error": {
                    "code": -32000,
                    "message": (
                        f"bridge→plugin message exceeds "
                        f"{_PLUGIN_MSG_MAX} bytes"),
                },
            }) + "\n"
        elif method:
            line = json.dumps({
                "jsonrpc": "2.0",
                "method": "message",
                "params": {
                    "type": "system",
                    "subtype": "error",
                    "data": {
                        "message": (
                            f"Dropped oversized notification "
                            f"{method!r} (>{_PLUGIN_MSG_MAX} bytes)"),
                    },
                },
            }) + "\n"
        else:
            return
    try:
        _out_q.put_nowait(line)
    except queue.Full:
        _log_drop(f"plugin stdout queue full (max {_PLUGIN_Q_MAX})")


def send_error(id: Optional[int], code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})


def send_result(id: int, result: Any) -> None:
    send({"jsonrpc": "2.0", "id": id, "result": result})


def send_notification(method: str, params: Any) -> None:
    send({"jsonrpc": "2.0", "method": method, "params": params})
