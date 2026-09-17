#!/usr/bin/env python3
"""Live kimi acp: force native Agent, dump ACP + agents/agent-N wire.

Does not write ~/.kimi-code/mcp.json. Spawns `kimi acp` like the host.

Checks:
  - Agent tool_call on ACP (same parent sessionId vs foreign)
  - agents/agent-N/wire.jsonl created
  - session/prompt return vs child wire still growing
  - host_gate leftover ⚙ (Agent → Task bg, no bash-* notify)
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

from host_gate import verdict  # noqa: E402
from extract import agent_dirs, find_session_dir, main_agent_calls, wire_types  # noqa: E402

KIMI = os.path.expanduser("~/.kimi-code/bin/kimi")

PROMPT = """Do not call EnterPlanMode, ExitPlanMode, Bash, Read, Write, Edit, Grep, or Glob yourself.

Call the Agent tool exactly once. Subagent type explore (or default).
The subagent's ONLY job: reply with exactly one line SANDBOX_CHILD_OK and stop. No tools.

After Agent returns, reply with exactly one line:
SANDBOX_PARENT agent_id=<id> status=<completed/failed>
and stop. Do not call Agent again.
"""


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
        self.titles = []
        self.tool_calls = []
        self.tool_updates = []
        self.session_ids = set()
        self.parent_sid = None
        self.foreign_updates = 0
        self.fs_reads = []
        self.term_creates = []
        self.errors = []
        self.t0 = time.time()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _now(self):
        return round(time.time() - self.t0, 3)

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
            sid = params.get("sessionId") or params.get("session_id")
            if sid:
                self.session_ids.add(str(sid))
                if self.parent_sid and str(sid) != str(self.parent_sid):
                    self.foreign_updates += 1
            if method == "session/update":
                self._on_update(params)
            elif method == "session/request_permission":
                self._on_perm(msg)
            elif method == "elicitation/create":
                self._on_elicit(msg)
            elif method.startswith("fs/"):
                self._on_fs(msg)
            elif method == "terminal/create":
                self.term_creates.append((params.get("command") or "")[:80])
                self._reply(mid, {"terminalId": "term_sandbox"})
            elif method == "terminal/wait_for_exit":
                self._reply(mid, {"exitCode": 0, "signal": None})
            elif method == "terminal/output":
                self._reply(mid, {"output": "", "truncated": False})
            elif method in ("terminal/release", "terminal/kill"):
                self._reply(mid, {})
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
        if st == "agent_message_chunk":
            c = upd.get("content") or {}
            t = c.get("text") if isinstance(c, dict) else ""
            if t:
                self.agent_text.append(t)
        if st in ("tool_call", "tool_call_update"):
            title = str(upd.get("title") or "")
            kind = str(upd.get("kind") or "")
            name = str(
                ((upd.get("_meta") or {}).get("x.ai/tool") or {}).get("name")
                or ""
            )
            raw = upd.get("rawInput") if isinstance(upd.get("rawInput"), dict) else {}
            if title:
                self.titles.append(title)
            rec = {
                "t": self._now(),
                "st": st,
                "title": title,
                "kind": kind,
                "name": name,
                "status": upd.get("status"),
                "sid": params.get("sessionId"),
            }
            if st == "tool_call":
                rec["raw"] = {k: raw[k] for k in list(raw)[:6]}
                self.tool_calls.append(rec)
            else:
                text = _tool_text(upd)
                rec["text_head"] = text[:300]
                self.tool_updates.append(rec)

    def _on_fs(self, msg: dict):
        params = msg.get("params") or {}
        path = params.get("path") or ""
        method = msg.get("method") or ""
        self.fs_reads.append(path)
        if "read" not in method:
            self._reply(msg.get("id"), {})
            return
        if not path:
            self._reply(msg.get("id"), {"content": ""})
            return
        try:
            if os.path.isdir(path):
                names = sorted(os.listdir(path))[:40]
                body = "\n".join(
                    n + ("/" if os.path.isdir(os.path.join(path, n)) else "")
                    for n in names
                )
                self._reply(msg.get("id"), {"content": "Directory:\n" + body})
                return
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(120_000)
            self._reply(msg.get("id"), {"content": content})
        except Exception as e:
            self._reply_err(msg.get("id"), str(e))

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
        if "plan" in title.lower():
            self._reply(msg.get("id"), {
                "outcome": {"outcome": "selected", "optionId": reject or allow},
            })
            return
        self._reply(msg.get("id"), {
            "outcome": {"outcome": "selected", "optionId": allow or reject},
        })

    def _on_elicit(self, msg: dict):
        self._reply(msg.get("id"), {"action": "cancel"})

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
            self.errors.append(msg)
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


def _tool_text(upd: dict) -> str:
    content = upd.get("content")
    text = ""
    if isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            inner = c.get("content") or {}
            if isinstance(inner, dict) and inner.get("text"):
                text += inner["text"]
            elif c.get("type") == "terminal":
                text += f" terminal:{c.get('terminalId')}"
    elif isinstance(content, str):
        text = content
    return text


def handshake(acp, cwd):
    init = acp.call("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": True, "writeTextFile": True},
            "terminal": True,
        },
        "clientInfo": {"name": "sandbox-kimi-agent", "version": "0"},
    }, timeout=25)
    if init.get("error"):
        return None, f"initialize {init['error']}"
    methods = ((init.get("result") or {}).get("authMethods")) or []
    ids = [m.get("id") for m in methods if isinstance(m, dict)]
    if "cached_token" in ids:
        auth = acp.call("authenticate", {"methodId": "cached_token"}, timeout=15)
        if auth.get("error"):
            return None, f"authenticate {auth['error']}"
    nxt = acp.call("session/new", {"cwd": cwd, "mcpServers": []}, timeout=25)
    if nxt.get("error"):
        return None, f"session/new {nxt['error']}"
    sid = (nxt.get("result") or {}).get("sessionId")
    if not sid:
        return None, f"no sessionId {nxt}"
    acp.parent_sid = sid
    return sid, None


def main() -> int:
    if not os.path.isfile(KIMI):
        print("FAIL no kimi")
        return 1
    cwd = tempfile.mkdtemp(prefix="kimi-agent-e2e-")
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
        if acp.stderr_tail:
            print("stderr", "\n".join(acp.stderr_tail[-8:]))
        return 1

    t0 = time.time()
    first = acp.call(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": PROMPT}]},
        timeout=120,
    )
    elapsed = time.time() - t0
    text = "".join(acp.agent_text)
    time.sleep(1.5)
    sdir = find_session_dir(sid)
    kids = agent_dirs(sdir) if sdir else []
    calls = main_agent_calls(sdir) if sdir else []
    proc.kill()

    agent_titles = [
        t for t in acp.titles
        if "agent" in t.lower() or t.lower() in ("task", "explore")
    ]
    result_txt = ""
    for u in acp.tool_updates:
        h = u.get("text_head") or ""
        if "agent_id:" in h or "SANDBOX_CHILD" in h:
            result_txt = h
            break
    if not result_txt:
        for c in calls:
            if c.get("ev") == "tool.result":
                result_txt = c.get("result_head") or ""
                break
    raw = {}
    for tc in acp.tool_calls:
        title = (tc.get("title") or "")
        if "agent" in title.lower() or (tc.get("name") or "").lower() == "agent":
            raw = tc.get("raw") or {}
            break
    v = verdict("Agent", "Agent", raw, result_txt or "")
    v_launch = verdict(
        next((t for t in acp.titles if "launching" in t.lower()), "Agent"),
        "Agent", raw, result_txt or "",
    )

    print("sid", sid)
    print("elapsed", round(elapsed, 2))
    print("prompt_err", (first.get("error") or None))
    print("stop", (first.get("result") or {}).get("stopReason"))
    print("acp_sids", sorted(acp.session_ids))
    print("foreign_updates", acp.foreign_updates)
    print("titles", acp.titles[:12])
    print("tool_calls", [
        (c.get("title"), c.get("name"), c.get("sid") == sid)
        for c in acp.tool_calls[:8]
    ])
    print("parent_text", text[:240].replace("\n", " / "))
    print("session_dir", sdir)
    print("child_agents", [os.path.basename(k) for k in kids])
    for k in kids:
        print(" ", os.path.basename(k), dict(wire_types(
            os.path.join(k, "wire.jsonl")).most_common(6)))
    print("host_gate leftover_gear", v["leftover_gear"],
          "paint_bg", v["paint_bg"], "agent_id", v["agent_id"],
          "launch_spawn", v_launch["spawn"])
    if acp.stderr_tail:
        print("stderr_tail", acp.stderr_tail[-4:])

    fails = []
    if first.get("error"):
        fails.append(f"prompt {first['error']}")
    if not agent_titles and not any(
            "agent" in (c.get("title") or "").lower()
            or (c.get("name") or "").lower() == "agent"
            for c in acp.tool_calls) and not calls:
        fails.append("no Agent tool_call on ACP or main wire")
    if not kids:
        fails.append("no agents/agent-N dir")
    if acp.foreign_updates:
        fails.append(f"foreign session/update x{acp.foreign_updates} (Grok-style multiplex?)")
    if v["leftover_gear"] or v_launch["leftover_gear"] or v_launch["spawn"]:
        fails.append("host would keep ⚙ after Agent completed (no bash notify)")
    if "SANDBOX_PARENT" not in text and "SANDBOX_CHILD_OK" not in text:
        fails.append("no SANDBOX_PARENT / SANDBOX_CHILD_OK in agent text")

    if fails:
        print("FAIL")
        for f in fails:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
