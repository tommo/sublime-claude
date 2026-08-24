#!/usr/bin/env python3
"""Live kimi acp e2e: post-background-bash recovery.

The spawn e2e (check_e2e.py) only asked whether session/prompt returned
before sleep finished. It never kept reading session/update AFTER the
prompt RPC settled — which is the host freeze: _on_done → @done / idle
while Kimi keeps sending tools/text.

This file is a thin ACP host. It does not write ~/.kimi-code/mcp.json.
wait_for_exit + release match the real host: release on a live process
detaches (does not kill) and unblocks wait_for_exit.

Strategies (argv, default during+after):
  during  bg bash, then KEEP WORKING in the same turn
  after   bg bash, stop; listen for self-wake with no second prompt
  wake    like after, then host session/prompt right after wait_for_exit

Classification (printed, and FAIL if leftover_after_prompt / self_wake
would have idled the Sublime host):
  prompt_held_until_quiet  prompt RPC stayed open until last update
  leftover_after_prompt    updates continued after prompt returned
  silent_then_self_wake    gap after prompt, then more updates
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

from turn_state import TurnState  # noqa: E402

KIMI = os.path.expanduser("~/.kimi-code/bin/kimi")
SLEEP_S = 8
LISTEN_AFTER_S = 20
MARKER = os.path.join(tempfile.gettempdir(), "kimi_bg_recover_marker.txt")
LOG = os.path.join(_HERE, "fixtures", "recovery.jsonl")


def _prompt_during(cwd: str) -> str:
    after = os.path.join(cwd, "after_launch.txt")
    return f"""Do not call EnterPlanMode, ExitPlanMode, Task, or Subagent.

Step 1: Call Bash exactly once with run_in_background true, description "sandbox recover sleep", timeout 60, command:
sleep {SLEEP_S}; echo SANDBOX_RECOVERED > {MARKER}; echo SANDBOX_RECOVERED

Step 2: The sleep is background. Immediately after the Bash launch ack (do not wait for the sleep), keep THIS SAME turn going:
- Write the file {after} with the exact text AFTER_BG_LAUNCH
- Then reply with at least two sentences that both contain the token SANDBOX_AFTER_LAUNCH

Do not end the turn at step 1. Continue after the background Bash.
"""


def _prompt_after(cwd: str) -> str:
    done = os.path.join(cwd, "after_done.txt")
    return f"""Do not call EnterPlanMode, ExitPlanMode, Task, or Subagent.

Call Bash exactly once with run_in_background true, description "sandbox recover sleep", timeout 60, command:
sleep {SLEEP_S}; echo SANDBOX_RECOVERED > {MARKER}; echo SANDBOX_RECOVERED

After the Bash launch ack, reply with exactly one line:
SANDBOX_LAUNCHED
Then stop. Do not call Write yet.

If a background-task completion arrives later, write {done} with AFTER_BG_DONE and say SANDBOX_AFTER_DONE.
"""


class Acp:
    def __init__(self, proc, cwd: str):
        self.proc = proc
        self.cwd = cwd
        self._n = 0
        self._lock = threading.Lock()
        self._pending = {}
        self._dead = False
        self.stderr_tail = []
        self.timeline = []  # (t, kind, detail)
        self.t0 = time.time()
        self.parent_sid = None
        self.agent_text = []
        self.term_creates = []
        self.wait_exits = []
        self.wait_replies = []
        self.releases = []
        self.kills = []
        self.terms = {}
        self.methods = []
        self.sids = set()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _now(self):
        return round(time.time() - self.t0, 3)

    def _note(self, kind, detail):
        rec = (self._now(), kind, detail)
        with self._lock:
            self.timeline.append(rec)
        return rec

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
                self._fs(msg)
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
                self._note("unhandled", method)
                if mid is not None:
                    self._reply_err(mid, f"sandbox: unhandled {method}")
            return
        if mid is not None:
            with self._lock:
                fut = self._pending.pop(mid, None)
            if fut is not None:
                fut.append(msg)

    def _on_update(self, params: dict):
        sid = params.get("sessionId") or params.get("session_id") or ""
        if sid:
            self.sids.add(sid)
        upd = params.get("update") or params
        st = upd.get("sessionUpdate") or upd.get("type") or ""
        foreign = bool(
            sid and self.parent_sid and sid != self.parent_sid)
        title = upd.get("title") or ""
        text = ""
        if st in ("agent_message_chunk",):
            c = upd.get("content") or {}
            text = c.get("text") if isinstance(c, dict) else ""
            if text and not foreign:
                self.agent_text.append(text)
        raw = upd.get("rawInput") or {}
        cmd = ""
        if isinstance(raw, dict):
            cmd = str(raw.get("command") or raw.get("cmd") or "")[:80]
        self._note("upd", {
            "st": st,
            "sid": sid[-12:] if sid else "",
            "foreign": foreign,
            "title": title[:80],
            "text": (text or "")[:80],
            "cmd": cmd,
            "tool": upd.get("toolCallId") or "",
            "status": upd.get("status") or "",
        })

    def _fs(self, msg: dict):
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        path = params.get("path") or ""
        self._note("fs", method.split("/")[-1] + " " + os.path.basename(path))
        if "read" in method:
            try:
                with open(path, encoding="utf-8") as f:
                    self._reply(msg.get("id"), {"content": f.read()})
            except Exception:
                self._reply(msg.get("id"), {"content": ""})
            return
        if "write" in method:
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(params.get("content") or "")
                self._reply(msg.get("id"), {})
            except Exception as e:
                self._reply_err(msg.get("id"), str(e))
            return
        self._reply(msg.get("id"), {})

    def _term_create(self, msg: dict):
        params = msg.get("params") or {}
        self.term_creates.append(_short(params))
        cmd = params.get("command") or "/bin/bash"
        args = params.get("args") or []
        cwd = params.get("cwd") or self.cwd
        env = os.environ.copy()
        for e in params.get("env") or []:
            if isinstance(e, dict) and e.get("name"):
                env[str(e["name"])] = str(e.get("value") or "")
        argv = [cmd] + list(args)
        self._note("term_create", _short(argv))
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
        slot = {
            "proc": proc, "out": b"", "exit": None,
            "detached": False, "wait_rid": None,
        }
        self.terms[tid] = slot

        def _drain():
            try:
                slot["out"] = proc.stdout.read() or b""
            except Exception:
                slot["out"] = b""
            proc.wait()
            slot["exit"] = proc.returncode
            rid = slot.get("wait_rid")
            if rid is not None and not slot.get("detached"):
                slot["wait_rid"] = None
                self.wait_replies.append((self._now(), tid, slot["exit"]))
                self._note("wait_reply", f"{tid} exit={slot['exit']}")
                self._reply(rid, {
                    "exitCode": slot["exit"], "signal": None,
                })

        threading.Thread(target=_drain, daemon=True).start()
        self._reply(msg.get("id"), {"terminalId": tid})

    def _term_wait(self, msg: dict):
        tid = (msg.get("params") or {}).get("terminalId")
        self.wait_exits.append(tid)
        rid = msg.get("id")
        slot = self.terms.get(tid) or {}
        self._note("wait_for_exit", str(tid))
        # Host: already-detached snap → return stored exit immediately.
        if slot.get("detached"):
            code = slot.get("exit")
            self.wait_replies.append((self._now(), tid, "detached"))
            self._note("wait_reply", f"{tid} already-detached")
            self._reply(rid, {
                "exitCode": 0 if code is None else code,
                "signal": None,
            })
            return
        proc = slot.get("proc")
        if proc is not None and proc.poll() is not None:
            code = slot.get("exit")
            if code is None:
                code = proc.returncode
            self.wait_replies.append((self._now(), tid, code))
            self._note("wait_reply", f"{tid} already-exited {code}")
            self._reply(rid, {"exitCode": code, "signal": None})
            return
        # Host detach cancels the wait reader. Stay pending until exit
        # *or* release unblocks (matches host _detach_terminal).
        slot["wait_rid"] = rid

    def _term_release(self, msg: dict):
        tid = (msg.get("params") or {}).get("terminalId")
        self.releases.append(tid)
        slot = self.terms.get(tid) or {}
        self._note("release", str(tid))
        # Host bg release = detach, not kill. Unblock wait_for_exit.
        slot["detached"] = True
        rid = slot.get("wait_rid")
        if rid is not None:
            slot["wait_rid"] = None
            self.wait_replies.append((self._now(), tid, "detach-unblock"))
            self._note("wait_reply", f"{tid} detach-unblock")
            self._reply(rid, {"exitCode": 0, "signal": None})
        self._reply(msg.get("id"), {})

    def _term_output(self, msg: dict):
        tid = (msg.get("params") or {}).get("terminalId")
        slot = self.terms.get(tid) or {}
        out = slot.get("out") or b""
        self._reply(msg.get("id"), {
            "output": out.decode("utf-8", "replace"),
            "truncated": False,
        })

    def _term_kill(self, msg: dict):
        tid = (msg.get("params") or {}).get("terminalId")
        self.kills.append(tid)
        self._note("kill", str(tid))
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
        self._note("perm", title[:80])
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
        "clientInfo": {"name": "sandbox-kimi-recover", "version": "0"},
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


_QUIET_UPD = (
    "available_commands_update", "current_mode_update",
    "user_message_chunk", "usage_update", "session_info_update",
)
# Host-visible agent work after prompt RPC. wait_reply/release are
# terminal lifecycle, not a painted turn.
_AGENT_KINDS = ("upd", "perm", "fs")


def _is_agent_event(kind, detail) -> bool:
    if kind == "perm":
        return True
    if kind == "fs":
        return True
    if kind != "upd":
        return False
    st = (detail or {}).get("st") if isinstance(detail, dict) else ""
    return st not in _QUIET_UPD and bool(st)


def _classify(prompt_t, timeline, prompt2_t=None):
    """prompt_t is seconds from acp.t0.

    prompt2_t: second host session/prompt (wake strategy). Events after
    that are the host-owned continuation, not leftover freeze.
    """
    after = []
    last_before = None
    cap = prompt2_t if prompt2_t is not None else 1e9
    for t, kind, detail in timeline:
        if not _is_agent_event(kind, detail):
            continue
        if t <= prompt_t + 0.05:
            last_before = t
        elif t < cap - 0.05:
            after.append((t, kind, detail))
    if not after:
        return "prompt_held_until_quiet", after, last_before
    gap = after[0][0] - prompt_t
    if gap >= 2.0:
        return "silent_then_self_wake", after, last_before
    return "leftover_after_prompt", after, last_before


def _host_sim(prompt_t, timeline):
    """What Sublime host does: idle on prompt RPC, leftovers never busy."""
    turn = TurnState()
    turn.begin_query()
    idle_at = None
    dropped_busy = []
    for t, kind, detail in timeline:
        if idle_at is None and t >= prompt_t:
            turn.end_live()
            idle_at = t
        if kind != "upd":
            continue
        st = (detail or {}).get("st") if isinstance(detail, dict) else ""
        if st not in (
            "agent_message_chunk", "tool_call", "tool_call_update",
            "agent_thought_chunk",
        ):
            continue
        if idle_at is not None and t > idle_at:
            action = turn.inbound_action(
                "tool_use_bg" if st == "tool_call" else "text")
            dropped_busy.append((round(t, 2), st, action, turn.busy))
    notify = turn.notify_action("kimi")
    return {
        "would_at_done": True,
        "busy_after": turn.busy,
        "notify_action": notify,
        "leftover_events": len(dropped_busy),
        "sample": dropped_busy[:8],
    }


def _run_strategy(name: str, prompt: str, cwd: str, wake: bool = False) -> dict:
    try:
        os.remove(MARKER)
    except OSError:
        pass
    proc = subprocess.Popen(
        [KIMI, "acp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=os.environ.copy(),
        bufsize=0,
    )
    acp = Acp(proc, cwd)
    sid, err = handshake(acp, cwd)
    if err:
        proc.kill()
        return {"name": name, "error": err}
    acp.parent_sid = sid
    acp.t0 = time.time()
    acp._note("prompt_send", name)
    first = acp.call(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]},
        timeout=120,
    )
    prompt_t = acp._now()
    acp._note("prompt_return", _short(first.get("error") or first.get("result")))
    prompt2_t = None
    second = None
    if wake:
        # Wait until wait_for_exit replies (bg bash actually finished).
        deadline = time.time() + SLEEP_S + 8
        while time.time() < deadline:
            if acp.wait_replies:
                break
            if acp._dead:
                break
            time.sleep(0.05)
        woke = os.path.join(cwd, "woke.txt")
        p2 = (
            f"The background bash finished (terminal wait_for_exit returned). "
            f"Write {woke} with the exact text WOKE and reply with one line "
            f"SANDBOX_WOKE. Do not call EnterPlanMode."
        )
        acp._note("prompt2_send", "wake")
        second = acp.call(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text": p2}]},
            timeout=90,
        )
        prompt2_t = acp._now()
        acp._note(
            "prompt2_return",
            _short(second.get("error") or second.get("result")),
        )
    # Keep reading after the host would have @done.
    time.sleep(LISTEN_AFTER_S)
    marker_later = os.path.isfile(MARKER)
    after_launch = os.path.join(cwd, "after_launch.txt")
    after_done = os.path.join(cwd, "after_done.txt")
    woke_path = os.path.join(cwd, "woke.txt")
    cls, after, last_before = _classify(
        prompt_t, acp.timeline, prompt2_t=prompt2_t)
    sim = _host_sim(prompt_t, acp.timeline)
    proc.kill()
    n_text = sum(
        1 for _, k, d in after
        if k == "upd" and isinstance(d, dict)
        and d.get("st") == "agent_message_chunk" and d.get("text"))
    n_tool = sum(
        1 for _, k, d in after
        if k == "upd" and isinstance(d, dict)
        and d.get("st") == "tool_call")
    n_foreign = sum(
        1 for _, k, d in after
        if k == "upd" and isinstance(d, dict) and d.get("foreign"))
    n_fs = sum(1 for _, k, _ in after if k == "fs")
    n_perm = sum(1 for _, k, _ in after if k == "perm")
    return {
        "name": name,
        "sid": sid,
        "prompt_t": prompt_t,
        "class": cls,
        "n_upd_after": len(after),
        "n_text_after": n_text,
        "n_tool_after": n_tool,
        "n_foreign_after": n_foreign,
        "n_fs_after": n_fs,
        "n_perm_after": n_perm,
        "last_before": last_before,
        "first_after": after[0][0] if after else None,
        "last_after": after[-1][0] if after else None,
        "n_create": len(acp.term_creates),
        "n_wait": len(acp.wait_exits),
        "n_release": len(acp.releases),
        "wait_replies": acp.wait_replies,
        "marker": marker_later,
        "after_launch": (
            os.path.isfile(after_launch)
            and open(after_launch, encoding="utf-8").read().strip()
        ),
        "after_done": (
            os.path.isfile(after_done)
            and open(after_done, encoding="utf-8").read().strip()
        ),
        "woke": (
            os.path.isfile(woke_path)
            and open(woke_path, encoding="utf-8").read().strip()
        ),
        "prompt2_t": prompt2_t,
        "prompt2_result": (
            _short(second.get("error") or second.get("result"))
            if second is not None else None
        ),
        "agent_text": "".join(acp.agent_text)[-500:],
        "sids": list(acp.sids),
        "prompt_result": _short(first.get("error") or first.get("result")),
        "host_sim": sim,
        "timeline": acp.timeline,
        "stderr": acp.stderr_tail[-8:],
    }


def _dump_log(results):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "w", encoding="utf-8") as f:
        for r in results:
            row = dict(r)
            # Keep full timeline in the jsonl; print is truncated.
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _print_result(r: dict) -> None:
    print("=" * 60)
    print("strategy", r.get("name"))
    if r.get("error"):
        print("ERROR", r["error"])
        return
    print("sid", r["sid"])
    print("prompt_return_s", r["prompt_t"])
    print("class", r["class"])
    print("n_agent_after", r["n_upd_after"],
          "text", r["n_text_after"],
          "tool", r["n_tool_after"],
          "fs", r.get("n_fs_after"),
          "perm", r.get("n_perm_after"),
          "foreign", r["n_foreign_after"])
    print("last_upd_before_prompt", r["last_before"])
    print("first_upd_after_prompt", r["first_after"])
    print("last_upd_after_prompt", r["last_after"])
    print("terminal create/wait/release",
          r["n_create"], r["n_wait"], r["n_release"])
    print("wait_replies", r["wait_replies"])
    print("marker", r["marker"])
    print("after_launch.txt", r["after_launch"])
    print("after_done.txt", r["after_done"])
    print("woke.txt", r.get("woke"))
    print("prompt_result", r["prompt_result"])
    print("prompt2_t", r.get("prompt2_t"))
    print("prompt2_result", r.get("prompt2_result"))
    print("host_sim", r["host_sim"])
    print("agent_text", r["agent_text"])
    print("sids", r["sids"])
    print("stderr", r["stderr"])
    print("--- timeline (upd/term/prompt) ---")
    for t, kind, detail in r["timeline"]:
        if kind in (
            "upd", "term_create", "wait_for_exit", "wait_reply",
            "release", "kill", "prompt_send", "prompt_return",
            "prompt2_send", "prompt2_return", "fs", "perm",
        ):
            print(f"  {t:7.2f} {kind:14} {_short(detail, 160)}")


def main() -> int:
    if not os.path.isfile(KIMI):
        print("FAIL no kimi")
        return 1
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    want = argv or ["during", "after"]
    results = []
    for name in want:
        cwd = tempfile.mkdtemp(prefix=f"kimi-bg-recover-{name}-")
        if name == "during":
            r = _run_strategy(name, _prompt_during(cwd), cwd)
        elif name == "after":
            r = _run_strategy(name, _prompt_after(cwd), cwd)
        elif name == "wake":
            r = _run_strategy(name, _prompt_after(cwd), cwd, wake=True)
        else:
            print("unknown strategy", name)
            return 1
        results.append(r)
        _print_result(r)
    _dump_log(results)

    fails = []
    for r in results:
        if r.get("error"):
            fails.append(f"{r['name']}: {r['error']}")
            continue
        # Freeze: host _on_done at prompt RPC, agent still fs/perm/tools.
        if r["name"] != "wake" and r["class"] in (
            "leftover_after_prompt", "silent_then_self_wake",
        ):
            fails.append(
                f"{r['name']}: {r['class']} "
                f"n_agent_after={r['n_upd_after']} "
                f"fs={r.get('n_fs_after')} perm={r.get('n_perm_after')} "
                f"(host would @done at {r['prompt_t']}s while agent live)"
            )
        if r["name"] == "during" and not r.get("after_launch") and not r.get("agent_text"):
            fails.append("during: no continuation after bash launch")
        if r["name"] == "wake":
            err = (r.get("prompt2_result") or "")
            low = err.lower()
            if "agent_busy" in low or "already in progress" in low:
                fails.append(f"wake: second prompt {err}")
            if not r.get("woke") and "SANDBOX_WOKE" not in (r.get("agent_text") or ""):
                fails.append("wake: no WOKE continuation on second prompt")
    if fails:
        print("FAIL")
        for f in fails:
            print(" -", f)
        return 1
    print("PASS no leftover-after-prompt on measured strategies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
