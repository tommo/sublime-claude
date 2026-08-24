#!/usr/bin/env python3
"""Live kimi acp e2e: Bash run_in_background + TaskOutput.

Does not write ~/.kimi-code/mcp.json. Spawns `kimi acp` like the host.

Measures, against real 0.37.2:
  - does session/prompt return before the sleep finishes?
  - terminal/create / wait_for_exit / release counts
  - Bash tool_result (task_id, pid, automatic_notification)
  - bash-*.json on disk
  - whether the process actually ran (marker file)
  - TaskOutput retrieval_status / output while still running
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_HERE, _ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from kimi_bg import KimiBgMixin  # noqa: E402

KIMI = os.path.expanduser("~/.kimi-code/bin/kimi")
SLEEP_S = 8
MARKER = os.path.join(tempfile.gettempdir(), "kimi_bg_e2e_marker.txt")

PROMPT1 = f"""Do not explore the filesystem except as needed for the command.
Do not call Read, Write, Edit, Grep, Glob, EnterPlanMode, ExitPlanMode, TaskOutput, or TaskStop.

Call Bash exactly once with run_in_background true, description "sandbox bg sleep", timeout 60, command:
sleep {SLEEP_S}; echo SANDBOX_BG_DONE > {MARKER}; echo SANDBOX_BG_DONE

After Bash returns, reply with exactly one line and stop:
SANDBOX_BG task_id=<id> pid=<n>
"""


class Acp:
    def __init__(self, proc):
        self.proc = proc
        self._n = 0
        self._lock = threading.Lock()
        self._pending = {}
        self.events = []
        self.agent_text = []
        self.methods = []
        self.tool_results = []
        self.term_creates = []
        self.wait_exits = []
        self.releases = []
        self.kills = []
        self.terms = {}
        self.wait_replies = []
        self._dead = False
        self.stderr_tail = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read_err(self):
        if not self.proc.stderr:
            return
        for line in iter(self.proc.stderr.readline, b""):
            s = line.decode("utf-8", "replace").rstrip()
            if s:
                self.stderr_tail.append(s[-400:])

    def _read(self):
        buf = b""
        while True:
            if self.proc.poll() is not None and not buf:
                self._dead = True
                return
            r, _, _ = select.select([self.proc.stdout], [], [], 0.2)
            if not r:
                continue
            b = self.proc.stdout.read(1)
            if not b:
                self._dead = True
                return
            if b != b"\n":
                buf += b
                continue
            line = buf.decode("utf-8", "replace").strip()
            buf = b""
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict):
        mid = msg.get("id")
        method = msg.get("method")
        if method:
            self.methods.append(method)
            params = msg.get("params") or {}
            if method == "session/update":
                self._on_update(params)
            elif method == "session/request_permission":
                self._on_perm(msg)
            elif method.startswith("fs/"):
                self._reply(mid, {"content": ""} if "read" in method else {})
            elif method == "terminal/create":
                self._term_create(msg)
            elif method == "terminal/wait_for_exit":
                self._term_wait(msg)
            elif method == "terminal/release":
                self._term_release(msg)
            elif method == "terminal/output":
                self._term_output(msg)
            elif method == "terminal/kill":
                self._term_kill(msg)
            else:
                if mid is not None:
                    self._reply_err(mid, f"sandbox: unhandled {method}")
            return
        if mid is not None:
            with self._lock:
                fut = self._pending.pop(mid, None)
            if fut is not None:
                fut.append(msg)

    def _on_update(self, params: dict):
        upd = params.get("update") or params
        st = upd.get("sessionUpdate") or upd.get("type") or ""
        if st in ("agent_message_chunk",):
            c = upd.get("content") or {}
            t = c.get("text") if isinstance(c, dict) else ""
            if t:
                self.agent_text.append(t)
        if st == "tool_call":
            raw = upd.get("rawInput") or {}
            self.events.append((
                "tool_call", upd.get("title"),
                _short(raw),
            ))
        if st == "tool_call_update":
            content = upd.get("content")
            text = ""
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    inner = c.get("content") or {}
                    if isinstance(inner, dict) and inner.get("text"):
                        text += inner["text"]
            if text:
                self.tool_results.append(text)
                self.events.append(("tool_result", text[:300]))

    def _term_create(self, msg: dict):
        params = msg.get("params") or {}
        self.term_creates.append(_short(params))
        cmd = params.get("command") or "/bin/bash"
        args = params.get("args") or []
        cwd = params.get("cwd") or os.getcwd()
        env = os.environ.copy()
        for e in params.get("env") or []:
            if isinstance(e, dict) and e.get("name"):
                env[str(e["name"])] = str(e.get("value") or "")
        argv = [cmd] + list(args)
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except Exception as e:
            self._reply_err(msg.get("id"), f"spawn failed: {e}")
            return
        tid = f"term_{proc.pid}"
        slot = {"proc": proc, "out": b"", "exit": None}
        self.terms[tid] = slot

        def _drain():
            try:
                slot["out"] = proc.stdout.read() or b""
            except Exception:
                slot["out"] = b""
            proc.wait()
            slot["exit"] = proc.returncode

        threading.Thread(target=_drain, daemon=True).start()
        self._reply(msg.get("id"), {"terminalId": tid})

    def _term_wait(self, msg: dict):
        tid = (msg.get("params") or {}).get("terminalId")
        self.wait_exits.append(tid)
        rid = msg.get("id")
        slot = self.terms.get(tid) or {}
        proc = slot.get("proc")

        def _wait():
            if proc is not None:
                try:
                    proc.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    pass
            code = slot.get("exit")
            if code is None and proc is not None:
                code = proc.poll()
            self._reply(rid, {
                "exitCode": 0 if code is None else code,
                "signal": None,
            })

        threading.Thread(target=_wait, daemon=True).start()

    def _term_output(self, msg: dict):
        tid = (msg.get("params") or {}).get("terminalId")
        slot = self.terms.get(tid) or {}
        out = slot.get("out") or b""
        self._reply(msg.get("id"), {
            "output": out.decode("utf-8", "replace"),
            "truncated": False,
        })

    def _term_release(self, msg: dict):
        tid = (msg.get("params") or {}).get("terminalId")
        self.releases.append(tid)
        # Detach — do not kill. Matches host timeout:0 / bg release.
        self._reply(msg.get("id"), {})

    def _term_kill(self, msg: dict):
        tid = (msg.get("params") or {}).get("terminalId")
        slot = self.terms.get(tid) or {}
        proc = slot.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, 15)
            except Exception:
                proc.terminate()
        self._reply(msg.get("id"), {})

    def _on_perm(self, msg: dict):
        params = msg.get("params") or {}
        opts = params.get("options") or []
        tc = params.get("toolCall") or {}
        title = str(tc.get("title") or "")
        allow = next(
            (o.get("optionId") for o in opts
             if isinstance(o, dict)
             and str(o.get("kind") or "").startswith("allow")),
            None,
        )
        reject = next(
            (o.get("optionId") for o in opts
             if isinstance(o, dict)
             and str(o.get("kind") or "").startswith("reject")),
            None,
        )
        low = title.lower()
        # Let Bash / background run; deny plan.
        if "plan" in low:
            self._reply(msg.get("id"), {
                "outcome": {"outcome": "selected",
                            "optionId": reject or allow},
            })
            return
        self._reply(msg.get("id"), {
            "outcome": {"outcome": "selected",
                        "optionId": allow or reject},
        })

    def _next_id(self):
        with self._lock:
            self._n += 1
            return self._n

    def _write(self, obj):
        with self._lock:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            self.proc.stdin.flush()

    def _reply(self, rid, result):
        if rid is not None:
            self._write({"jsonrpc": "2.0", "id": rid, "result": result})

    def _reply_err(self, rid, msg):
        if rid is not None:
            self._write({
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": -32000, "message": msg},
            })

    def call(self, method, params, timeout=30):
        rid = self._next_id()
        box = []
        with self._lock:
            self._pending[rid] = box
        self._write({
            "jsonrpc": "2.0", "id": rid,
            "method": method, "params": params,
        })
        end = time.time() + timeout
        while time.time() < end:
            if box:
                return box[0]
            if self._dead:
                return {"error": {"message": "agent exited"}}
            time.sleep(0.05)
        return {"error": {"message": f"timeout {method}"}}


def _short(obj, n=240):
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    return s[:n]


def handshake(acp, cwd):
    init = acp.call("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": True, "writeTextFile": True},
            "terminal": True,
        },
        "clientInfo": {"name": "sandbox-kimi-bg", "version": "0"},
    }, timeout=25)
    if init.get("error"):
        return None, f"initialize {init['error']}"
    nxt = acp.call("session/new", {"cwd": cwd, "mcpServers": []}, timeout=25)
    if nxt.get("error"):
        return None, f"session/new {nxt['error']}"
    sid = (nxt.get("result") or {}).get("sessionId")
    if not sid:
        return None, f"no sessionId {nxt}"
    return sid, None


def _task_files(sid: str) -> list:
    root = os.path.expanduser("~/.kimi-code/sessions")
    out = []
    try:
        for wd in os.listdir(root):
            tdir = os.path.join(root, wd, sid, "agents", "main", "tasks")
            if not os.path.isdir(tdir):
                continue
            for name in os.listdir(tdir):
                if name.startswith("bash-") and name.endswith(".json"):
                    path = os.path.join(tdir, name)
                    try:
                        with open(path, encoding="utf-8") as f:
                            out.append(json.load(f))
                    except Exception:
                        pass
    except OSError:
        pass
    return out


def main() -> int:
    if not os.path.isfile(KIMI):
        print("FAIL no kimi")
        return 1
    try:
        os.remove(MARKER)
    except OSError:
        pass
    cwd = tempfile.mkdtemp(prefix="kimi-bg-e2e-")
    proc = subprocess.Popen(
        [KIMI, "acp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=os.environ.copy(),
        bufsize=0,
    )
    acp = Acp(proc)
    sid, err = handshake(acp, cwd)
    if err:
        proc.kill()
        print("FAIL handshake", err)
        return 1

    t0 = time.time()
    first = acp.call(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": PROMPT1}]},
        timeout=90,
    )
    elapsed = time.time() - t0
    text = "".join(acp.agent_text)
    bash_txt = next(
        (t for t in acp.tool_results if "task_id:" in t), "")
    parsed = KimiBgMixin.parse_kimi_bg_result_text(bash_txt) or {}
    tasks = _task_files(sid)
    marker_after_prompt = os.path.isfile(MARKER)

    # Wait remaining sleep + a beat, then TaskOutput.
    left = SLEEP_S + 2 - elapsed
    if left > 0:
        time.sleep(left)
    marker_later = os.path.isfile(MARKER)
    tid = parsed.get("task_id") or ""
    prompt2 = (
        f"Call TaskOutput exactly once with task_id {tid or 'the bash task'}. "
        "Then reply one line: SANDBOX_TO retrieval=<status> has_output=<yes/no> "
        "and stop. Do not call Bash."
    )
    second = acp.call(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": prompt2}]},
        timeout=60,
    )
    to_txt = next(
        (t for t in acp.tool_results if "retrieval_status" in t
         or (tid and tid in t and "task_id" in t and t != bash_txt)),
        "",
    )
    text2 = "".join(acp.agent_text)
    proc.kill()

    fails = []
    returned_early = elapsed < (SLEEP_S - 1.5)
    if not returned_early:
        fails.append("session/prompt did not return before sleep finished "
                     f"(elapsed={elapsed:.1f}s sleep={SLEEP_S})")
    if not parsed.get("task_id"):
        fails.append("Bash tool_result missing task_id")
    if not parsed.get("auto"):
        fails.append("Bash tool_result missing automatic_notification")
    if not tasks:
        fails.append("no bash-*.json on disk")
    # Process must actually run. Marker may appear after prompt return.
    if not marker_later:
        fails.append(f"marker file never written ({MARKER}) — process did not run")

    print("sid", sid)
    print("elapsed_s", round(elapsed, 2), "sleep", SLEEP_S)
    print("first_prompt", _short(first.get("error") or first.get("result")))
    print("second_prompt", _short(second.get("error") or second.get("result")))
    print("n_terminal/create", len(acp.term_creates))
    print("n_wait_for_exit", len(acp.wait_exits))
    print("n_release", len(acp.releases))
    print("bash_result", bash_txt[:500])
    print("parsed", parsed)
    print("tasks", [{k: t.get(k) for k in (
        "taskId", "status", "detached", "pid", "exitCode")} for t in tasks])
    print("marker_after_prompt", marker_after_prompt)
    print("marker_later", marker_later)
    print("taskoutput", to_txt[:500])
    print("agent_text", (text + "\n" + text2)[-600:])
    print("term_create_sample", acp.term_creates[:2])
    print("stderr", acp.stderr_tail[-6:])

    if fails:
        print("FAIL")
        for f in fails:
            print(" -", f)
        return 1
    print("PASS bg spawn returned early; process ran; task json present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
