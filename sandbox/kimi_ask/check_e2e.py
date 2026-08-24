#!/usr/bin/env python3
"""Live kimi acp e2e: 2-question AskUser — does Q1 reach the first continuation?

Does not write ~/.kimi-code/mcp.json. Spawns `kimi acp` like the host.

Strategies (argv, default all):
  q0            permission q0_opt only
  inject_during session/prompt with Q1 while permission is outstanding
  inject_after  return q0, immediately session/prompt (no cancel)
  cancel_then   return q0, session/cancel, wait prompt settle, then followup
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

from acp_base import AcpBridge  # noqa: E402

KIMI = os.path.expanduser("~/.kimi-code/bin/kimi")
Q0_LABEL = "procmotion"
# Live failure: user typed Other "all" — not a listed Phase 1 label.
Q1_LABEL = "all"

QUESTIONS = [
    {
        "header": "Pkg name",
        "question": "What should the new procedural animation package be named?",
        "options": [
            {"label": "procanim",
             "description": "Modules: procanim.dynamics, procanim.verlet, procanim.ik, ..."},
            {"label": "procmotion",
             "description": "Modules: procmotion.dynamics, procmotion.verlet, ..."},
            {"label": "panim",
             "description": "Short form: panim.dynamics, panim.verlet, ..."},
        ],
        "multiSelect": False,
    },
    {
        "header": "Phase 1",
        "question": (
            "Confirm Phase 1 scope (pure math modules, no ECS coupling yet)?"
        ),
        "options": [
            {"label": "dynamics + noise_motion (Recommended)",
             "description": "Springs + motion noise wrappers — broadest reuse, easy tests"},
            {"label": "dynamics only",
             "description": "Just springs first, smallest possible start"},
            {"label": "dynamics + verlet",
             "description": "Springs + verlet chains, skip noise for now"},
        ],
        "multiSelect": False,
    },
]

ANSWERS = {
    QUESTIONS[0]["question"]: Q0_LABEL,
    QUESTIONS[1]["question"]: Q1_LABEL,
}

PROMPT = """Do not explore the filesystem. Do not call Bash, Read, Write, Edit, Grep, Glob, EnterPlanMode, ExitPlanMode, or any tool except AskUserQuestion.

Call AskUserQuestion exactly once with exactly these two questions (copy the labels verbatim):

1. header "Pkg name"
   question "What should the new procedural animation package be named?"
   options: procanim, procmotion, panim

2. header "Phase 1"
   question "Confirm Phase 1 scope (pure math modules, no ECS coupling yet)?"
   options: "dynamics + noise_motion (Recommended)", "dynamics only", "dynamics + verlet"

After AskUserQuestion returns, reply with exactly one line and stop:
SANDBOX_RESULT Q0=<label or MISSING> Q1=<label or MISSING>

Use MISSING for any answer the tool result did not contain. Do not guess. Do not enter plan mode.
"""


class Acp:
    def __init__(self, proc):
        self.proc = proc
        self._n = 0
        self._lock = threading.Lock()
        self._pending = {}
        self._inbox = []
        self.events = []
        self.agent_text = []
        self.methods = []
        self.permission_opts = []
        self.elicitation = []
        self.elicitation_io = []
        self.tool_results = []
        self.errors = []
        self._on_ask = None
        self._after_ask = None
        self._hold_elicitation = False
        self._held_elicit = None
        self._dead = False
        t = threading.Thread(target=self._read, daemon=True)
        t.start()
        t2 = threading.Thread(target=self._read_err, daemon=True)
        t2.start()
        self.stderr_tail = []

    def _read_err(self):
        if not self.proc.stderr:
            return
        for line in iter(self.proc.stderr.readline, b""):
            s = line.decode("utf-8", "replace").rstrip()
            if s:
                self.stderr_tail.append(s[-400:])
                if len(self.stderr_tail) > 40:
                    self.stderr_tail = self.stderr_tail[-20:]

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
                self.events.append(("bad_json", line[:200]))
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict):
        mid = msg.get("id")
        method = msg.get("method")
        if method:
            self.methods.append(method)
            self.events.append(("in", method, mid, _short(msg.get("params"))))
            if method == "session/update":
                self._on_update(msg.get("params") or {})
            elif method == "session/request_permission":
                self._on_permission(msg)
            elif method == "elicitation/create":
                self._on_elicitation(msg)
            elif method.startswith("fs/"):
                self._reply(mid, self._fs(method, msg.get("params") or {}))
            elif method.startswith("terminal/"):
                self._reply_err(mid, f"sandbox: no {method}")
            else:
                self._reply_err(mid, f"sandbox: unhandled {method}")
            return
        if mid is not None:
            with self._lock:
                fut = self._pending.pop(mid, None)
            if fut is not None:
                fut.append(msg)
            else:
                self.events.append(("orphan_resp", mid, _short(msg)))

    def _on_update(self, params: dict):
        upd = params.get("update") or params
        st = upd.get("sessionUpdate") or upd.get("type") or ""
        if st in ("agent_message_chunk", "agent_thought_chunk"):
            c = upd.get("content") or {}
            t = c.get("text") if isinstance(c, dict) else ""
            if t:
                if st == "agent_message_chunk":
                    self.agent_text.append(t)
        if st == "tool_call":
            raw = upd.get("rawInput") or {}
            self.events.append((
                "tool_call", upd.get("title") or raw.get("name"),
                list((raw.get("questions") or [])),
            ))
        if st == "tool_call_update":
            status = upd.get("status")
            content = upd.get("content")
            self.events.append(("tool_upd", status, _short(content)))
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "content":
                        inner = c.get("content") or {}
                        txt = inner.get("text") if isinstance(inner, dict) else ""
                        if txt and "answers" in txt:
                            self.tool_results.append(txt)

    def _on_permission(self, msg: dict):
        params = msg.get("params") or {}
        opts = params.get("options") or []
        self.permission_opts.append(opts)
        tc = params.get("toolCall") or {}
        title = str(tc.get("title") or "")
        oids = [
            o.get("optionId") for o in opts if isinstance(o, dict)
        ]
        is_ask = (
            "question" in title.lower()
            or any(str(x).startswith("q0_opt_") for x in oids)
        )
        if is_ask:
            oid = _q0_oid(opts, Q0_LABEL) or _first_q0(opts)
            if self._on_ask:
                self._on_ask(msg, oid)
            self._reply(msg.get("id"), {
                "outcome": {"outcome": "selected", "optionId": oid},
            })
            if self._after_ask:
                self._after_ask(msg, oid)
            return
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
        # Stay on the ask path: allow read-ish, reject plan/write.
        low = title.lower()
        if any(x in low for x in ("plan", "write", "edit", "bash")):
            self._reply(msg.get("id"), {
                "outcome": {"outcome": "selected",
                            "optionId": reject or allow},
            })
            return
        self._reply(msg.get("id"), {
            "outcome": {"outcome": "selected",
                        "optionId": allow or reject},
        })

    def _on_elicitation(self, msg: dict):
        params = msg.get("params") or {}
        self.elicitation.append(params)
        qs, keys = AcpBridge._questions_from_elicitation(params)
        content = AcpBridge._elicitation_content_from_answers(
            qs, keys, ANSWERS)
        self.elicitation_io.append({
            "keys": keys,
            "content": content,
            "schema_titles": [
                ((params.get("requestedSchema") or {}).get("properties") or {})
                .get(k, {}).get("title")
                for k in keys
            ],
            "message": params.get("message"),
        })
        if self._on_ask:
            self._on_ask(msg, "elicitation")
        if self._hold_elicitation:
            self._held_elicit = msg
            return
        if content:
            self._reply(msg.get("id"), {
                "action": "accept", "content": content,
            })
        else:
            # Still accept q0 so we observe Kimi dropping Other, not dismiss.
            self._reply(msg.get("id"), {
                "action": "accept",
                "content": content or {keys[0]: Q0_LABEL} if keys else {},
            })
        if self._after_ask:
            self._after_ask(msg, "elicitation")

    def _fs(self, method, params):
        if method.endswith("read_text_file"):
            return {"content": ""}
        return {}

    def _next_id(self):
        with self._lock:
            self._n += 1
            return self._n

    def _write(self, obj):
        line = json.dumps(obj) + "\n"
        with self._lock:
            self.proc.stdin.write(line.encode())
            self.proc.stdin.flush()

    def _reply(self, rid, result):
        if rid is None:
            return
        self._write({"jsonrpc": "2.0", "id": rid, "result": result})

    def _reply_err(self, rid, msg):
        if rid is None:
            return
        self._write({
            "jsonrpc": "2.0", "id": rid,
            "error": {"code": -32000, "message": msg},
        })

    def notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

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


def _q0_oid(opts, label):
    return AcpBridge._kimi_q0_option_id(opts, QUESTIONS, label)


def _first_q0(opts):
    return AcpBridge._kimi_first_q0_option_id(opts)


def _followup():
    return AcpBridge._kimi_followup_answers(QUESTIONS, ANSWERS)


def spawn(cwd):
    env = os.environ.copy()
    return subprocess.Popen(
        [KIMI, "acp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        bufsize=0,
    )


def handshake(acp, cwd):
    init = acp.call("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": True, "writeTextFile": True},
            "terminal": True,
            "elicitation": {"form": {}},
        },
        "clientInfo": {"name": "sandbox-kimi-ask", "version": "0"},
    }, timeout=25)
    if init.get("error"):
        return None, f"initialize {init['error']}"
    nxt = acp.call("session/new", {
        "cwd": cwd,
        "mcpServers": [],
    }, timeout=25)
    if nxt.get("error"):
        return None, f"session/new {nxt['error']}"
    sid = (nxt.get("result") or {}).get("sessionId")
    if not sid:
        return None, f"no sessionId {nxt}"
    return sid, None


def _verdict(text: str, tool_results: list) -> dict:
    joined = text
    q1_missing = (
        "PHASE 1 UNANSWERED" in joined.upper()
        or "Q1=MISSING" in joined.upper()
        or "Q1=<MISSING" in joined.upper()
    )
    q1_got = (
        f"Q1={Q1_LABEL}" in joined
        or f"Q1={Q1_LABEL}".lower() in joined.lower()
        or (Q1_LABEL in joined and "Phase 1" in joined)
    )
    q0_got = Q0_LABEL in joined
    tr_q1 = any(Q1_LABEL in t for t in tool_results)
    tr_q0 = any(Q0_LABEL in t for t in tool_results)
    return {
        "agent_text": joined[:800],
        "q0_in_text": q0_got,
        "q1_in_text": q1_got and not q1_missing,
        "q1_declared_missing": q1_missing,
        "tool_q0": tr_q0,
        "tool_q1": tr_q1,
        "sandbox_line": next(
            (ln for ln in joined.splitlines() if "SANDBOX_RESULT" in ln),
            "",
        ),
    }


def run_strategy(name: str, timeout=180) -> dict:
    if not os.path.isfile(KIMI):
        return {"ok": False, "stage": "spawn", "err": "no kimi"}
    cwd = tempfile.mkdtemp(prefix="kimi-ask-e2e-")
    proc = spawn(cwd)
    acp = Acp(proc)
    sid, err = handshake(acp, cwd)
    if err:
        proc.kill()
        return {"ok": False, "strategy": name, "stage": "handshake", "err": err}

    extra_rpc = {"error": {"message": "not sent"}}
    follow_box = []
    follow = _followup()
    follow_params = {
        "sessionId": sid,
        "prompt": [{"type": "text", "text": follow}],
    }

    def send_follow(tag):
        rid = acp._next_id()
        with acp._lock:
            acp._pending[rid] = follow_box
        acp._write({
            "jsonrpc": "2.0", "id": rid,
            "method": "session/prompt",
            "params": follow_params,
        })
        acp.events.append((tag, rid))

    if name == "interrupt_during":
        acp._hold_elicitation = True

    def on_ask(_msg, oid):
        acp.events.append(("ask_oid", oid))
        if name == "inject_during":
            send_follow("inject_during")
        elif name == "interrupt_during":
            acp.notify("session/cancel", {"sessionId": sid})
            acp.events.append(("cancel", True))

    def after_ask(_msg, oid):
        if name == "inject_after":
            send_follow("inject_after")
        elif name == "cancel_then":
            acp.notify("session/cancel", {"sessionId": sid})
            acp.events.append(("cancel", True))

    acp._on_ask = on_ask
    acp._after_ask = after_ask
    first_timeout = 12 if name == "interrupt_during" else timeout
    first = acp.call(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": PROMPT}]},
        timeout=first_timeout,
    )
    if name == "interrupt_during":
        err = first.get("error") if isinstance(first.get("error"), dict) else {}
        timed_out = str(err.get("message") or "").startswith("timeout")
        if timed_out:
            held = acp._held_elicit
            if held:
                acp._reply(held.get("id"), {"action": "cancel"})
                acp.events.append(("elicit_cancel_after_hang", True))
            extra_rpc = {
                "error": {"message": "needed elicitation cancel to unstick"},
            }
    if name == "cancel_then":
        extra_rpc = acp.call(
            "session/prompt", follow_params, timeout=timeout)
    elif name in ("inject_during", "inject_after"):
        end = time.time() + timeout
        while time.time() < end and not follow_box and not acp._dead:
            time.sleep(0.05)
        extra_rpc = follow_box[0] if follow_box else {
            "error": {"message": f"{name} no response"},
        }

    text = "".join(acp.agent_text)
    v = _verdict(text, acp.tool_results)
    asked = any(e[0] == "ask_oid" for e in acp.events)
    if not asked and not acp.elicitation:
        ok = False
        why = "AskUserQuestion never reached elicitation or permission"
    elif name == "interrupt_during":
        stop = ""
        if isinstance(first, dict):
            stop = str((first.get("result") or {}).get("stopReason")
                       or first.get("error") or "")
        hung = any(e[0] == "elicit_cancel_after_hang" for e in acp.events)
        ok = (not hung) and ("cancel" in stop.lower() or "interrupt" in stop.lower())
        why = ("session/cancel unblocked ask" if ok
               else "session/cancel ignored while elicitation outstanding")
    elif name == "q0":
        # Live Other="all" is not a listed option. Kimi must drop it from
        # the tool result (permission q0-only, or elicitation enum filter).
        ok = bool(v["tool_q0"]) and not v["tool_q1"]
        why = ("Other Q1 dropped from tool_result" if ok
               else "expected Q1 Other to be absent from tool_result")
    else:
        ok = bool(v["q1_in_text"]) and not v["q1_declared_missing"]
        why = "Q1 reached first continuation" if ok else "Q1 still missing"
    proc.kill()
    return {
        "ok": ok,
        "why": why,
        "asked": asked,
        "strategy": name,
        "sid": sid,
        "first_prompt": _short(first.get("error") or first.get("result")),
        "extra_prompt": _short(
            extra_rpc.get("error") or extra_rpc.get("result")
            if isinstance(extra_rpc, dict) else extra_rpc
        ),
        "elicitation": len(acp.elicitation),
        "elicitation_io": acp.elicitation_io[:2],
        "methods": acp.methods[:40],
        "stderr": acp.stderr_tail[-8:],
        "ask_events": [e for e in acp.events if e[0] in (
            "ask_oid", "tool_call", "inject_during", "inject_after",
            "cancel",
        )][:30],
        **v,
    }


def main(argv) -> int:
    names = [a for a in argv[1:] if not a.startswith("-")]
    if not names:
        names = ["q0", "inject_during"]
    failed = 0
    print("followup:\n", _followup())
    print("q0_oid preview", _q0_oid([
        {"optionId": "q0_opt_0", "name": "procanim", "kind": "allow_once"},
        {"optionId": "q0_opt_1", "name": "procmotion", "kind": "allow_once"},
        {"optionId": "q0_opt_2", "name": "panim", "kind": "allow_once"},
        {"optionId": "q0_skip", "name": "Skip", "kind": "reject_once"},
    ], Q0_LABEL))
    for name in names:
        print(f"\n=== {name} ===")
        try:
            r = run_strategy(name)
        except Exception as e:
            r = {"ok": False, "strategy": name, "err": repr(e)}
        status = "PASS" if r.get("ok") else "FAIL"
        if not r.get("ok"):
            failed += 1
        print(status, name)
        for k, v in r.items():
            if k == "ok":
                continue
            print(f"  {k}: {v}")
    print(f"\n{failed} failed / {len(names)}")
    return 1 if failed == len(names) else (0 if failed == 0 else 2)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
