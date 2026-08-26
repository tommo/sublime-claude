#!/usr/bin/env python3
"""Live kimi acp e2e: 2-question AskUser via elicitation/create.

kimi 0.38 `packages/acp-server/src/question.ts` (from the binary):
  if elicitationForm: createElicitation(ALL questions)
  elicitationResponseToQuestionAnswers keeps only exact option labels
  Other/otherLabel is unsupported (no extra text field)
  request_permission fallback degrades to q0_opt_N only

Does not write ~/.kimi-code/mcp.json. Spawns `kimi acp` like the host.
No session/cancel + followup prompt (that is the anti-pattern).

Strategies (argv, default listed other):
  listed  elicitation accept q0+q1 declared labels — Q1 must be in tool result
  other   elicitation accept q1='all' (not in enum) — Kimi must drop it
  interrupt_during  session/cancel while elicitation outstanding
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
Q1_LISTED = "dynamics only"
# Other is not a declared option — Kimi drops it (elicitationResponseToQuestionAnswers).
Q1_OTHER = "all"

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

ANSWERS_LISTED = {
    QUESTIONS[0]["question"]: Q0_LABEL,
    QUESTIONS[1]["question"]: Q1_LISTED,
}
ANSWERS_OTHER = {
    QUESTIONS[0]["question"]: Q0_LABEL,
    QUESTIONS[1]["question"]: Q1_OTHER,
}

Q0_Q = QUESTIONS[0]["question"]
Q1_Q = QUESTIONS[1]["question"]


def _freetext_content(strategy: str, qs, keys) -> dict:
    """Payloads to try for Other. Engine TUI uses {kind: other, text}."""
    k0 = keys[0] if keys else "q0"
    k1 = keys[1] if len(keys) > 1 else "q1"
    listed = AcpBridge._elicitation_content_from_answers(
        qs, keys, ANSWERS_LISTED)
    if strategy in ("other", "after_prompt"):
        return {k0: Q0_LABEL, k1: Q1_OTHER}
    if strategy == "extra":
        return {
            k0: Q0_LABEL,
            k1: Q1_OTHER,
            f"{k1}_other": Q1_OTHER,
            "other": Q1_OTHER,
            "q1_other": Q1_OTHER,
        }
    if strategy == "qtext":
        return {Q0_Q: Q0_LABEL, Q1_Q: Q1_OTHER, k0: Q0_LABEL, k1: Q1_OTHER}
    if strategy == "kind":
        return {k0: Q0_LABEL, k1: {"kind": "other", "text": Q1_OTHER}}
    if strategy == "hold_prompt":
        return {k0: Q0_LABEL, k1: Q1_OTHER}
    if strategy == "meta":
        return {
            k0: Q0_LABEL,
            k1: Q1_OTHER,
            "_meta": {"q1_other": Q1_OTHER, "kind": "other", "text": Q1_OTHER},
        }
    return listed

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
    def __init__(self, proc, strategy: str = "listed"):
        self.proc = proc
        self.strategy = strategy
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
        self._content_override = None
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
        if self.strategy == "listed":
            content = AcpBridge._elicitation_content_from_answers(
                qs, keys, ANSWERS_LISTED)
        else:
            content = _freetext_content(self.strategy, qs, keys)
        if self._content_override is not None:
            content = self._content_override(qs, keys, content)
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
            self._reply(msg.get("id"), {
                "action": "accept",
                "content": {keys[0]: Q0_LABEL} if keys else {},
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
    # leftover helper — not used by listed/other. Keep for interrupt tests.
    return AcpBridge._kimi_followup_answers(QUESTIONS, ANSWERS_OTHER)


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


def _verdict(text: str, tool_results: list, q1_expect: str) -> dict:
    joined = text
    q1_missing = (
        "PHASE 1 UNANSWERED" in joined.upper()
        or "Q1=MISSING" in joined.upper()
        or "Q1=<MISSING" in joined.upper()
    )
    q1_got = (
        f"Q1={q1_expect}" in joined
        or (q1_expect and q1_expect in joined and "Phase 1" in joined)
    )
    q0_got = Q0_LABEL in joined
    tr_q1 = any(q1_expect in t for t in tool_results) if q1_expect else False
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
    acp = Acp(proc, strategy=name)
    sid, err = handshake(acp, cwd)
    if err:
        proc.kill()
        return {"ok": False, "strategy": name, "stage": "handshake", "err": err}

    extra_rpc = {"error": {"message": "not sent"}}
    if name in ("interrupt_during", "hold_prompt"):
        acp._hold_elicitation = True

    def on_ask(_msg, oid):
        acp.events.append(("ask_oid", oid))
        if name == "interrupt_during":
            acp.notify("session/cancel", {"sessionId": sid})
            acp.events.append(("cancel", True))

    acp._on_ask = on_ask
    first_timeout = 12 if name == "interrupt_during" else timeout
    if name == "hold_prompt":
        first_timeout = 8
    first = acp.call(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": PROMPT}]},
        timeout=first_timeout,
    )
    follow = {
        "sessionId": sid,
        "prompt": [{
            "type": "text",
            "text": (
                "The user typed Other for Phase 1: all. "
                "Honor Q0=procmotion Q1=all. "
                "Reply SANDBOX_RESULT Q0=procmotion Q1=all"
            ),
        }],
    }
    if name == "hold_prompt":
        extra_rpc = acp.call("session/prompt", follow, timeout=15)
        held = acp._held_elicit
        if held:
            qs, keys = AcpBridge._questions_from_elicitation(
                (held.get("params") or {}))
            content = _freetext_content("other", qs, keys)
            acp._reply(held.get("id"), {
                "action": "accept", "content": content,
            })
            acp.events.append(("elicit_accept_after_hold_prompt", content))
        # original prompt may still be open
        if not first.get("result") and not (
                isinstance(first.get("error"), dict)
                and "timeout" in str(first["error"])):
            pass
        else:
            first = first
        # wait remaining original prompt
        if isinstance(first.get("error"), dict) and str(
                first["error"].get("message") or "").startswith("timeout"):
            t2 = time.time() + timeout
            while time.time() < t2 and not acp._dead:
                # original call already timed out; drain via new wait on agent_text
                if "SANDBOX_RESULT" in "".join(acp.agent_text):
                    break
                time.sleep(0.1)
    if name == "after_prompt":
        extra_rpc = acp.call("session/prompt", follow, timeout=30)
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

    text = "".join(acp.agent_text)
    q1_expect = Q1_LISTED if name == "listed" else Q1_OTHER
    v = _verdict(text, acp.tool_results, q1_expect)
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
    elif name == "listed":
        ok = bool(v["tool_q0"] and v["tool_q1"])
        why = ("elicitation q0+q1 in tool_result" if ok
               else "listed Q1 missing from tool_result — elicitation failed")
    elif name in ("other", "extra", "qtext", "kind", "meta"):
        # PASS if freetext reached the tool result (the actual goal).
        if v["tool_q1"]:
            ok = True
            why = f"{name}: Other reached tool_result"
        else:
            ok = False
            why = f"{name}: Other still dropped from tool_result"
    elif name in ("hold_prompt", "after_prompt"):
        err = extra_rpc.get("error") if isinstance(extra_rpc, dict) else None
        busy = bool(err) and "busy" in str(err).lower()
        last = ""
        for ln in reversed(text.splitlines()):
            if "SANDBOX_RESULT" in ln:
                last = ln
                break
        if not last and "SANDBOX_RESULT" in text:
            last = text[text.rfind("SANDBOX_RESULT"):]
        ok = (not busy) and f"Q1={Q1_OTHER}" in last
        why = (f"{name}: last={last!r} extra={_short(extra_rpc)}" if not ok
               else f"{name}: followup end_turn Q1={Q1_OTHER}")
    elif name == "other_drop":
        ok = bool(v["tool_q0"]) and not v["tool_q1"]
        why = ("Other dropped from tool_result" if ok
               else "expected Other absent from tool_result")
    else:
        ok = False
        why = f"unknown strategy {name}"
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
        names = ["listed", "other"]
    failed = 0
    print("kimi", KIMI)
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
