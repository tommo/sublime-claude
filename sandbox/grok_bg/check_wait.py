#!/usr/bin/env python3
"""Grok and Kimi wait_for_exit stay pending until the process exits.

Does not spawn `grok` — drives AcpBridge the same way the live log did.
Multiple simultaneous bg terminals + parallel sandbox runs.
"""
from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_HERE, _ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

import acp_base  # noqa: E402
from acp_base import AcpBridge  # noqa: E402

acp_base.send_notification = lambda *a, **k: None

N_BG = 4
N_PARALLEL = 3


class Bridge(AcpBridge):
    def __init__(self, backend: str):
        self.BACKEND_NAME = backend
        self.cwd = os.getcwd()
        self._terminals = {}
        self._child_sessions = {}
        self._released_terminals = set()
        self._detached_snaps = {}
        self._detached_procs = {}
        self._terminal_bg = {}
        self._bg_notified_tasks = set()
        self._bg_notified_tools = set()
        self._last_bg_tool_id = None
        self.terminal_wait_timeout_s = 0
        self.terminal_output_max_bytes = 1024 * 1024
        self._calls = {}
        self._pending_execute_ids = []
        self._last_execute_id = None
        self._last_session_tool_ts = 0
        self._prompt_fut = None
        self._prompt_cancelled = False
        self._cancel_in_flight = False
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
    wait_task = asyncio.create_task(
        b._acp_terminal_wait({"terminalId": "term_g"}))
    await asyncio.sleep(0.25)
    if wait_task.done():
        fails.append(f"grok wait returned early: {wait_task.result()}")
    out = await b._acp_terminal_output({"terminalId": "term_g"})
    # Grok watch_for_exit: any exitStatus means the bg task is done.
    if out.get("exitStatus") is not None:
        fails.append(f"grok output while running stamped exit {out.get('exitStatus')}")
    if wait_task.done():
        fails.append(f"grok wait finished after output poll: {wait_task.result()}")
    if slot.get("exit_status") is not None:
        fails.append(f"grok wait set exit_status {slot.get('exit_status')}")
    if not _alive(pid):
        fails.append("grok wait killed the process")
    if not wait_task.done():
        wait_task.cancel()
        try:
            await wait_task
        except (asyncio.CancelledError, Exception):
            pass
    await b._acp_terminal_release({"terminalId": "term_g"})
    await asyncio.sleep(0.2)
    if _alive(pid):
        fails.append("grok release left the process running")
        await _kill_slot(slot)
    if b._should_synth_terminal_ui():
        fails.append("grok synth enabled")
    return fails


async def check_grok_many() -> list:
    """Several bg terminals at once: wait held, output has no exit, release is per-id."""
    fails = []
    b = Bridge("grok")
    ids = []
    pids = []
    waits = []
    try:
        for i in range(N_BG):
            eid = f"tool-bg-{i}"
            call = b._ensure_call(eid)
            call.merge_input({
                "command": f"sleep-many-{i}",
                "background": True,
            })
            call.background = True
            b._note_shell_execute(eid, "Bash")
            res = await b._acp_terminal_create({
                "command": sys.executable,
                "args": ["-c",
                         "import time,sys; sys.stdout.write('up\\n'); "
                         "sys.stdout.flush(); time.sleep(30)"],
                "cwd": os.getcwd(),
            })
            tid = res["terminalId"]
            ids.append(tid)
            slot = b._terminals[tid]
            if not slot.get("bg"):
                fails.append(f"{tid} not marked bg")
            pids.append(slot["proc"].pid)
            waits.append(asyncio.create_task(
                b._acp_terminal_wait({"terminalId": tid})))

        await asyncio.sleep(0.4)
        for i, (tid, wt, pid) in enumerate(zip(ids, waits, pids)):
            if wt.done():
                fails.append(f"{tid} wait returned early: {wt.result()}")
            if not _alive(pid):
                fails.append(f"{tid} died while wait pending")
        outs = await asyncio.gather(*[
            b._acp_terminal_output({"terminalId": tid}) for tid in ids])
        for tid, out, wt in zip(ids, outs, waits):
            if out.get("exitStatus") is not None:
                fails.append(
                    f"{tid} output stamped exit {out.get('exitStatus')}")
            if wt.done():
                fails.append(
                    f"{tid} wait finished after output: {wt.result()}")

        # Release only the first. The rest must keep running + pending wait.
        first = ids[0]
        await b._acp_terminal_release({"terminalId": first})
        await asyncio.sleep(0.25)
        if _alive(pids[0]):
            fails.append(f"{first} still alive after release")
        if not waits[0].done():
            waits[0].cancel()
            try:
                await waits[0]
            except (asyncio.CancelledError, Exception):
                pass
        for tid, wt, pid in zip(ids[1:], waits[1:], pids[1:]):
            if not _alive(pid):
                fails.append(f"{tid} killed when {first} was released")
            if wt.done():
                fails.append(
                    f"{tid} wait returned after sibling release: {wt.result()}")
            out = await b._acp_terminal_output({"terminalId": tid})
            if out.get("exitStatus") is not None:
                fails.append(
                    f"{tid} exit stamped after sibling release "
                    f"{out.get('exitStatus')}")
    finally:
        for wt in waits:
            if wt is not None and not wt.done():
                wt.cancel()
                try:
                    await wt
                except (asyncio.CancelledError, Exception):
                    pass
        for tid in ids[1:]:
            if tid in b._terminals:
                await b._acp_terminal_release({"terminalId": tid})
        await asyncio.sleep(0.15)
        for pid in pids:
            if _alive(pid):
                try:
                    os.killpg(pid, 9)
                except OSError:
                    pass
    return fails


async def check_stdout_after_kill() -> list:
    """User-close: process dies, wait returns, terminal/output still has stdout."""
    fails = []
    b = Bridge("grok")
    marker = "SANDBOX_STDOUT_MARKER_9f3a"
    eid = "tool-stdout"
    call = b._ensure_call(eid)
    call.merge_input({"command": "print-then-sleep", "background": True})
    call.background = True
    b._note_shell_execute(eid, "Bash")
    res = await b._acp_terminal_create({
        "command": sys.executable,
        "args": ["-c",
                 f"import time,sys; sys.stdout.write('{marker}\\n'); "
                 "sys.stdout.flush(); time.sleep(30)"],
        "cwd": os.getcwd(),
    })
    tid = res["terminalId"]
    slot = b._terminals[tid]
    pid = slot["proc"].pid
    wait_task = asyncio.create_task(
        b._acp_terminal_wait({"terminalId": tid}))
    try:
        for _ in range(20):
            out = await b._acp_terminal_output({"terminalId": tid})
            if marker in (out.get("output") or ""):
                break
            await asyncio.sleep(0.05)
        else:
            fails.append("marker never appeared in live output")
        if wait_task.done():
            fails.append(f"wait returned before kill: {wait_task.result()}")
        # User closed the process.
        try:
            os.killpg(pid, 15)
        except OSError:
            slot["proc"].terminate()
        result = await asyncio.wait_for(wait_task, timeout=3.0)
        wait_task = None
        if result.get("exitCode") is None and not result.get("signal"):
            fails.append(f"wait after kill had no exit: {result}")
        out = await b._acp_terminal_output({"terminalId": tid})
        body = out.get("output") or ""
        if marker not in body:
            fails.append(
                f"stdout missing after kill n={len(body)} {body[:80]!r}")
        if out.get("exitStatus") is None:
            fails.append("exitStatus still null after wait returned")
    except asyncio.TimeoutError:
        fails.append("wait did not return after kill")
        if wait_task is not None and not wait_task.done():
            wait_task.cancel()
            try:
                await wait_task
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        if tid in b._terminals:
            await b._acp_terminal_release({"terminalId": tid})
        if _alive(pid):
            try:
                os.killpg(pid, 9)
            except OSError:
                pass
    return fails


async def check_output_limit() -> list:
    """Grok 20k outputByteLimit: bg keeps >20k; fg keeps the tail."""
    fails = []
    head, tail = "SANDBOX_HEAD_aa01", "SANDBOX_TAIL_bb02"
    pad = 40_000
    script = (
        "import sys; sys.stdout.write(%r); sys.stdout.write('x' * %d); "
        "sys.stdout.write(%r); sys.stdout.flush()"
        % (head + "\n", pad, tail + "\n")
    )

    b_bg = Bridge("grok")
    eid = "tool-limit-bg"
    call = b_bg._ensure_call(eid)
    call.merge_input({"command": "pad", "timeout": 0})
    b_bg._note_shell_execute(eid, "Bash")
    res = await b_bg._acp_terminal_create({
        "command": sys.executable,
        "args": ["-c", script],
        "cwd": os.getcwd(),
        "outputByteLimit": 20000,
    })
    tid = res["terminalId"]
    slot = b_bg._terminals[tid]
    if not slot.get("bg"):
        fails.append("bg pad terminal not marked bg")
    if int(slot.get("limit") or 0) <= 20000:
        fails.append(f"bg limit not raised: {slot.get('limit')}")
    wait = await asyncio.wait_for(
        b_bg._acp_terminal_wait({"terminalId": tid}), timeout=5.0)
    out = await b_bg._acp_terminal_output({"terminalId": tid})
    await b_bg._acp_terminal_release({"terminalId": tid})
    body = out.get("output") or ""
    if head not in body or tail not in body:
        fails.append(
            f"bg output missing markers n={len(body)} trunc={out.get('truncated')}")
    if out.get("truncated"):
        fails.append("bg 40k output truncated under raised cap")
    if wait.get("exitCode") is None and not wait.get("signal"):
        fails.append(f"bg wait had no exit: {wait}")

    b_fg = Bridge("grok")
    eid = "tool-limit-fg"
    call = b_fg._ensure_call(eid)
    call.merge_input({"command": "pad"})
    b_fg._note_shell_execute(eid, "Bash")
    res = await b_fg._acp_terminal_create({
        "command": sys.executable,
        "args": ["-c", script],
        "cwd": os.getcwd(),
        "outputByteLimit": 8000,
    })
    tid = res["terminalId"]
    slot = b_fg._terminals[tid]
    if slot.get("bg"):
        fails.append("fg pad terminal marked bg")
    wait = await asyncio.wait_for(
        b_fg._acp_terminal_wait({"terminalId": tid}), timeout=5.0)
    out = await b_fg._acp_terminal_output({"terminalId": tid})
    await b_fg._acp_terminal_release({"terminalId": tid})
    body = out.get("output") or ""
    if tail not in body:
        fails.append(f"fg tail missing n={len(body)} {body[:60]!r}")
    if head in body:
        fails.append("fg kept the prefix instead of the tail")
    if not out.get("truncated"):
        fails.append("fg 40k under 8k limit was not truncated")
    if wait.get("exitCode") is None and not wait.get("signal"):
        fails.append(f"fg wait had no exit: {wait}")
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
    # Simultaneous sandbox runs (separate bridges) + many terminals on one.
    groups = await asyncio.gather(
        *[check_grok() for _ in range(N_PARALLEL)],
        *[check_kimi() for _ in range(N_PARALLEL)],
        check_grok_many(),
        check_stdout_after_kill(),
        check_output_limit(),
    )
    for g in groups:
        fails.extend(g)
    if fails:
        print("FAIL")
        for f in fails:
            print(" -", f)
        return 1
    print(f"PASS {N_PARALLEL} concurrent grok waits, process lived until release")
    print(f"PASS {N_PARALLEL} concurrent kimi waits, process lived")
    print(f"PASS {N_BG} simultaneous grok bg terminals: wait held, "
          "output had no exit, sibling release did not kill the rest")
    print("PASS stdout still present after kill + wait")
    print("PASS grok does not synth host Bash")
    print("PASS bg output past 20k kept; fg outputByteLimit keeps the tail")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
