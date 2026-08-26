#!/usr/bin/env python3
"""Live kimi acp: what does turn.agent_busy look like, and what recovers?

Does not write ~/.kimi-code/mcp.json.

Strategies:
  overlap     second session/prompt while the first is still in flight
  after0      second prompt immediately after first end_turn
  after2      second prompt 2s after first end_turn
  bg_after0   bg bash, second prompt immediately when first returns
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
for p in (_HERE, _ROOT, os.path.join(_ROOT, "bridge")):
    if p not in sys.path:
        sys.path.insert(0, p)

KIMI = os.path.expanduser("~/.kimi-code/bin/kimi")

PROMPT_SLOW = (
    "Do not call EnterPlanMode. Reply with exactly one line: "
    "SANDBOX_BUSY_ONE and stop. No tools."
)
PROMPT_TWO = (
    "Do not call EnterPlanMode. Reply with exactly one line: "
    "SANDBOX_BUSY_TWO and stop. No tools."
)
PROMPT_BG = (
    "Do not call EnterPlanMode or Task. "
    "Call Bash once with run_in_background true, timeout 60, command: sleep 6. "
    "After the launch ack, reply SANDBOX_BUSY_BG and stop."
)


class Acp:
    def __init__(self, proc):
        self.proc = proc
        self._n = 0
        self._lock = threading.Lock()
        self._pending = {}
        self._dead = False
        self.stderr_tail = []
        self.methods = []
        self.agent_text = []
        self.errors = []
        self.timeline = []
        self.t0 = time.time()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _now(self):
        return round(time.time() - self.t0, 3)

    def _note(self, kind, detail):
        self.timeline.append((self._now(), kind, detail))

    def _read_err(self):
        if not self.proc.stderr:
            return
        for line in iter(self.proc.stderr.readline, b""):
            s = line.decode("utf-8", "replace").rstrip()
            if s:
                self.stderr_tail.append(s[-300:])

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
                upd = params.get("update") or {}
                st = upd.get("sessionUpdate") or ""
                if st == "agent_message_chunk":
                    t = ((upd.get("content") or {}) or {}).get("text") or ""
                    if t:
                        self.agent_text.append(t)
                if st in ("agent_message_chunk", "tool_call") and (
                        st != "agent_message_chunk" or True):
                    title = upd.get("title") or ""
                    text = ""
                    if st == "agent_message_chunk":
                        text = ((upd.get("content") or {}) or {}).get("text") or ""
                    self._note("upd", f"{st} {title}{text[:60]}")
            elif method == "session/request_permission":
                self._on_perm(msg)
            elif method == "elicitation/create":
                self._reply(mid, {"action": "cancel"})
            elif method.startswith("fs/"):
                self._reply(mid, {"content": ""} if "read" in method else {})
            elif method.startswith("terminal/"):
                self._term(msg, method)
            else:
                if mid is not None:
                    self._reply_err(mid, f"unhandled {method}")
            return
        if mid is not None:
            with self._lock:
                fut = self._pending.pop(mid, None)
            if fut is not None:
                fut.append(msg)

    def _term(self, msg, method):
        mid = msg.get("id")
        params = msg.get("params") or {}
        if method.endswith("create"):
            self._note("term_create", "")
            self._reply(mid, {"terminalId": "term_busy"})
            return
        if method.endswith("wait_for_exit"):
            self._note("wait", "")
            # Do not block the ACP reader. Reply later so the first
            # prompt can return while the fake term is "running".
            def _later():
                time.sleep(8)
                self._reply(mid, {"exitCode": 0, "signal": None})
            threading.Thread(target=_later, daemon=True).start()
            return
        self._reply(mid, {})

    def _on_perm(self, msg):
        opts = (msg.get("params") or {}).get("options") or []
        allow = next(
            (o.get("optionId") for o in opts
             if isinstance(o, dict)
             and str(o.get("kind") or "").startswith("allow")),
            None,
        )
        self._reply(msg.get("id"), {
            "outcome": {"outcome": "selected", "optionId": allow},
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

    def call(self, method, params, timeout=30, wait=True):
        rid = self._next_id()
        box = []
        with self._lock:
            self._pending[rid] = box
        self._write({
            "jsonrpc": "2.0", "id": rid,
            "method": method, "params": params,
        })
        self._note("call", method)
        if not wait:
            return rid, box
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


def _err(msg):
    if not isinstance(msg, dict):
        return ""
    e = msg.get("error")
    if isinstance(e, dict):
        return str(e.get("message") or e)
    if msg.get("error"):
        return str(msg["error"])
    return ""


def handshake(acp, cwd):
    init = acp.call("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": True, "writeTextFile": True},
            "terminal": True,
            "elicitation": {"form": {}},
        },
        "clientInfo": {"name": "sandbox-kimi-busy", "version": "0"},
    }, timeout=25)
    if init.get("error"):
        return None, f"initialize {_err(init)}"
    nxt = acp.call("session/new", {"cwd": cwd, "mcpServers": []}, timeout=25)
    if nxt.get("error"):
        return None, f"session/new {_err(nxt)}"
    sid = (nxt.get("result") or {}).get("sessionId")
    if not sid:
        return None, f"no sessionId {nxt}"
    return sid, None


def _busy(msg) -> bool:
    s = _err(msg).lower() + _short(msg).lower()
    return "agent_busy" in s or "already in progress" in s


def run_strategy(name: str) -> dict:
    if not os.path.isfile(KIMI):
        return {"ok": False, "err": "no kimi"}
    cwd = tempfile.mkdtemp(prefix=f"kimi-busy-{name}-")
    proc = subprocess.Popen(
        [KIMI, "acp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd, env=os.environ.copy(), bufsize=0,
    )
    acp = Acp(proc)
    sid, err = handshake(acp, cwd)
    if err:
        proc.kill()
        return {"ok": False, "strategy": name, "stage": "hs", "err": err}

    p1 = PROMPT_BG if name == "bg_after0" else PROMPT_SLOW
    p2 = {
        "sessionId": sid,
        "prompt": [{"type": "text", "text": PROMPT_TWO}],
    }
    p1p = {
        "sessionId": sid,
        "prompt": [{"type": "text", "text": p1}],
    }

    if name in ("overlap", "overlap_retry"):
        rid1, box1 = acp.call("session/prompt", p1p, wait=False)
        time.sleep(0.15)
        second = acp.call("session/prompt", p2, timeout=20)
        t1 = time.time() + 60
        while time.time() < t1 and not box1 and not acp._dead:
            time.sleep(0.05)
        first = box1[0] if box1 else {"error": {"message": "timeout p1"}}
        if name == "overlap_retry" and _busy(second):
            # Clean recovery: wait for turn 1, retry WITHOUT session/cancel.
            third = acp.call("session/prompt", p2, timeout=40)
            second = third
    else:
        first = acp.call("session/prompt", p1p, timeout=90)
        if name == "after2":
            time.sleep(2)
        second = acp.call("session/prompt", p2, timeout=40)

    text = "".join(acp.agent_text)
    proc.kill()
    first_busy = _busy(first)
    second_busy = _busy(second)
    two = "SANDBOX_BUSY_TWO" in text
    return {
        "ok": True,
        "strategy": name,
        "first_busy": first_busy,
        "second_busy": second_busy,
        "first": _short(first.get("error") or first.get("result") or first),
        "second": _short(second.get("error") or second.get("result") or second),
        "text_has_two": two,
        "agent_text": text[-400:],
        "stderr": acp.stderr_tail[-6:],
        "n_upd": sum(1 for _, k, _ in acp.timeline if k == "upd"),
    }


def main() -> int:
    names = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not names:
        names = ["overlap", "after0", "after2", "bg_after0"]
    failed = 0
    print("kimi", KIMI)
    for name in names:
        print(f"\n=== {name} ===")
        try:
            r = run_strategy(name)
        except Exception as e:
            r = {"ok": False, "strategy": name, "err": repr(e)}
        for k, v in r.items():
            print(f"  {k}: {v}")
        # The experiment reports. overlap SHOULD be second_busy.
        # after0/after2 SHOULD deliver TWO without busy (clean gap).
        if not r.get("ok"):
            failed += 1
            continue
        if name == "overlap" and not r.get("second_busy"):
            print("  NOTE overlap did not get agent_busy")
        if name == "overlap_retry":
            if r.get("second_busy"):
                print("  FAIL retry after first end_turn still agent_busy")
                failed += 1
            elif not r.get("text_has_two"):
                print("  FAIL retry did not produce SANDBOX_BUSY_TWO")
                failed += 1
        if name in ("after0", "after2") and r.get("second_busy"):
            print("  FAIL second prompt agent_busy after first end_turn")
            failed += 1
        if name == "bg_after0" and r.get("second_busy"):
            print("  FAIL bg end_turn then immediate prompt is agent_busy")
            failed += 1
    print(f"\n{failed} failed / {len(names)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
