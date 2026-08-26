#!/usr/bin/env python3
"""Grok timeout:0 wait acks now; Kimi wait stays pending; process lives.

Does not spawn `grok` — drives AcpBridge the same way the live log did.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_HERE, _ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from acp_base import AcpBridge  # noqa: E402


class Bridge(AcpBridge):
    def __init__(self, backend: str):
        self.BACKEND_NAME = backend
        self._terminals = {}
        self._child_sessions = {}
        self._released_terminals = set()
        self._detached_snaps = {}
        self._detached_procs = {}
        self.terminal_wait_timeout_s = 0
        self._bg_tool_ids = set()
        self._pending_execute_ids = []
        self._tool_ids_emitted = set()
        self._last_session_tool_ts = 0
        self._logs = []

    def file_log(self, msg):
        self._logs.append(msg)

    def _emit_bg_terminal_complete(self, *a, **k):
        pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


async def _spawn_sleep(seconds: float = 30):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", f"import time; time.sleep({seconds})",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )

    async def _wait():
        await proc.wait()

    slot = {
        "proc": proc,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "exit_status": None,
        "bg": True,
        "cmd": f"sleep {seconds}",
        "reader": asyncio.create_task(_wait()),
    }
    return slot


async def _kill_slot(slot: dict) -> None:
    proc = slot.get("proc")
    reader = slot.get("reader")
    if proc is not None and proc.returncode is None:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except Exception:
            pass
    if reader is not None and not reader.done():
        reader.cancel()
        try:
            await reader
        except Exception:
            pass


async def check_grok() -> list:
    fails = []
    b = Bridge("grok")
    slot = await _spawn_sleep(30)
    b._terminals["term_g"] = slot
    pid = slot["proc"].pid
    t0 = time.monotonic()
    result = await b._acp_terminal_wait({"terminalId": "term_g"})
    elapsed = time.monotonic() - t0
    if elapsed > 0.5:
        fails.append(f"grok wait blocked {elapsed:.2f}s")
    if result != {"exitCode": 0, "signal": None}:
        fails.append(f"grok wait result {result}")
    if slot.get("exit_status") is not None:
        fails.append(f"grok wait set exit_status {slot.get('exit_status')}")
    if not _alive(pid):
        fails.append("grok wait killed the process")
    await b._acp_terminal_release({"terminalId": "term_g"})
    if _alive(pid):
        pass
    else:
        fails.append("grok release killed the process")
    await _kill_slot(slot)
    if b._should_synth_terminal_ui():
        fails.append("grok synth enabled")
    return fails


async def check_kimi() -> list:
    fails = []
    b = Bridge("kimi")
    slot = await _spawn_sleep(30)
    b._terminals["term_k"] = slot
    pid = slot["proc"].pid
    task = asyncio.create_task(
        b._acp_terminal_wait({"terminalId": "term_k"}))
    await asyncio.sleep(0.25)
    if task.done():
        fails.append(f"kimi wait returned early: {task.result()}")
    else:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    if slot.get("exit_status") is not None:
        fails.append(f"kimi wait set exit_status {slot.get('exit_status')}")
    if not _alive(pid):
        fails.append("kimi wait killed the process")
    await _kill_slot(slot)
    return fails


async def main() -> int:
    fails = []
    fails.extend(await check_grok())
    fails.extend(await check_kimi())
    if fails:
        print("FAIL")
        for f in fails:
            print(" -", f)
        return 1
    print("PASS grok wait ack <0.5s, process lived, release detached")
    print("PASS kimi wait stayed pending, process lived")
    print("PASS grok does not synth host Bash")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
