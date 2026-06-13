#!/usr/bin/env python3
"""Spike: drive the interactive Claude Code CLI and mirror it into a structured
event stream by tailing its session transcript.

Flow:  start -> trace -> extract

  start    pin a session uuid, build/launch the `claude` command
  trace    tail ~/.claude/projects/<cwd-slug>/<uuid>.jsonl as it grows
  extract  parse each appended JSON line into normalized render events
           (text / thinking / tool_use / tool_result+structured / usage / prompt)

The transcript carries a top-level `toolUseResult` object that is RICHER than
the model-facing text: Edit/Write give structuredPatch + originalFile, Bash
gives stdout/stderr, Read/Grep/Task are fully structured. That is what the
Sublime side would render (diffs, edits, order tables) without a control channel.

Usage:
  cc_trace.py inspect [JSONL]                 # schema/type census of a transcript
  cc_trace.py watch [--cwd DIR] [--session ID|latest] [--all]
  cc_trace.py start [--cwd DIR] [-p PROMPT] [--model M] [--interactive]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid as uuidlib

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")


# ── path resolution ──────────────────────────────────────────────────────────
def project_slug(cwd: str) -> str:
    """Claude Code slug: every non-alphanumeric char in the abs path -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd))


def transcript_dir(cwd: str) -> str:
    return os.path.join(PROJECTS, project_slug(cwd))


def transcript_path(cwd: str, session_id: str) -> str:
    return os.path.join(transcript_dir(cwd), session_id + ".jsonl")


def latest_transcript(cwd: str):
    d = transcript_dir(cwd)
    if not os.path.isdir(d):
        return None
    js = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")]
    return max(js, key=os.path.getmtime) if js else None


# ── trace: tail an append-only jsonl ─────────────────────────────────────────
def tail(path: str, from_start: bool, wait: bool = True, stop=None):
    """Yield parsed JSON records as lines are appended. Buffers partial lines.

    stop(): optional callable; when it returns True and no data is pending,
    the tail returns (used to end after the driving process exits and drains).
    """
    while wait and not os.path.exists(path):
        if stop and stop():
            return
        time.sleep(0.2)
    if not os.path.exists(path):
        return  # no-wait poll before the file exists yet
    buf = ""
    with open(path, "r") as f:
        if not from_start:
            f.seek(0, os.SEEK_END)
        while True:
            chunk = f.read()
            if chunk:
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        pass  # partial/corrupt; skip
            else:
                if stop and stop():
                    return
                if not wait:
                    return
                time.sleep(0.15)


# ── extract: record -> normalized events ─────────────────────────────────────
class Extractor:
    """Stateful: remembers tool_use id -> name so results can be labeled."""

    def __init__(self, show_all: bool = False):
        self.id2name = {}
        self.show_all = show_all

    def feed(self, o: dict):
        t = o.get("type")
        meta = bool(o.get("isMeta")) or bool(o.get("isSidechain"))
        if t == "assistant":
            yield from self._assistant(o, meta)
        elif t == "user":
            yield from self._user(o, meta)
        elif t == "system":
            if self.show_all:
                yield {"kind": "system", "subtype": o.get("subtype"),
                       "text": (o.get("content") or "")[:200], "meta": meta}
        elif o.get("isCompactSummary") or t == "summary":
            yield {"kind": "summary", "text": (o.get("summary") or o.get("content") or "")[:200]}
        elif self.show_all:
            yield {"kind": "other", "type": t}

    def _assistant(self, o, meta):
        m = o.get("message") or {}
        for b in m.get("content", []):
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and b.get("text", "").strip():
                yield {"kind": "text", "text": b["text"], "meta": meta}
            elif bt == "thinking" and b.get("thinking", "").strip():
                yield {"kind": "thinking", "text": b["thinking"], "meta": meta}
            elif bt == "tool_use":
                self.id2name[b.get("id")] = b.get("name")
                yield {"kind": "tool_use", "id": b.get("id"), "name": b.get("name"),
                       "input": b.get("input", {}), "meta": meta}
        u = m.get("usage")
        if u:
            yield {"kind": "usage", "in": u.get("input_tokens", 0),
                   "out": u.get("output_tokens", 0),
                   "cache_r": u.get("cache_read_input_tokens", 0),
                   "cache_w": u.get("cache_creation_input_tokens", 0)}

    def _user(self, o, meta):
        m = o.get("message") or {}
        c = m.get("content")
        structured = o.get("toolUseResult")
        if isinstance(c, str):
            if c.strip():
                yield {"kind": "prompt", "text": c, "meta": meta}
            return
        if isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_result":
                    tid = b.get("tool_use_id")
                    yield {"kind": "tool_result", "id": tid,
                           "name": self.id2name.get(tid, "?"),
                           "is_error": bool(b.get("is_error")),
                           "structured": structured,
                           "text": _flatten_text(b.get("content")), "meta": meta}
                elif bt == "text" and b.get("text", "").strip():
                    yield {"kind": "prompt", "text": b["text"], "meta": meta}
                elif bt == "image":
                    yield {"kind": "image", "meta": meta}


def _flatten_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
        return "\n".join(out)
    return ""


# ── display: mimic tool_formatters, prove the structured payload is usable ────
def render(ev: dict) -> str:
    k = ev["kind"]
    tag = " [meta]" if ev.get("meta") else ""
    if k == "prompt":
        return f"\033[1;36m◎ USER{tag}\033[0m {_oneline(ev['text'], 200)}"
    if k == "text":
        return f"\033[1m▶{tag}\033[0m {_oneline(ev['text'], 200)}"
    if k == "thinking":
        return f"\033[2m… thinking{tag}: {_oneline(ev['text'], 120)}\033[0m"
    if k == "usage":
        return (f"\033[2m   ↳ tokens in={ev['in']} out={ev['out']} "
                f"cache_r={ev['cache_r']} cache_w={ev['cache_w']}\033[0m")
    if k == "tool_use":
        return f"\033[33m⚙ {ev['name']}\033[0m{tag} {_tool_args(ev['name'], ev['input'])}"
    if k == "tool_result":
        mark = "\033[31m✘\033[0m" if ev["is_error"] else "\033[32m✔\033[0m"
        return f"  {mark} {ev['name']} {_tool_result(ev['name'], ev)}"
    if k == "summary":
        return f"\033[35m≡ summary\033[0m {_oneline(ev['text'], 160)}"
    if k == "system":
        return f"\033[2m· system/{ev.get('subtype')}\033[0m"
    return f"\033[2m· {k} {ev.get('type','')}\033[0m"


def _tool_args(name, inp) -> str:
    if not isinstance(inp, dict):
        return ""
    if name in ("Read", "Edit", "Write"):
        return inp.get("file_path", "")
    if name == "Bash":
        return _oneline(inp.get("command", ""), 120)
    if name in ("Grep", "Glob"):
        return inp.get("pattern", "") + (f"  ({inp.get('path')})" if inp.get("path") else "")
    if name in ("TaskCreate",):
        return inp.get("subject", "")
    if name in ("TaskUpdate",):
        return f"#{inp.get('taskId','')} -> {inp.get('status','')}"
    if name in ("WebFetch", "WebSearch"):
        return inp.get("url") or inp.get("query", "")
    return _oneline(json.dumps(inp), 100)


def _tool_result(name, ev) -> str:
    s = ev.get("structured")
    if name in ("Edit", "Write") and isinstance(s, dict):
        patch = s.get("structuredPatch") or []
        adds = sum(1 for h in patch for ln in h.get("lines", []) if ln.startswith("+"))
        dels = sum(1 for h in patch for ln in h.get("lines", []) if ln.startswith("-"))
        return f"{os.path.basename(s.get('filePath',''))}  \033[32m+{adds}\033[0m \033[31m-{dels}\033[0m ({len(patch)} hunk)"
    if name == "Bash" and isinstance(s, dict):
        out = (s.get("stdout") or "").strip()
        err = (s.get("stderr") or "").strip()
        bits = []
        if out:
            bits.append(f"{out.count(chr(10))+1} lines out")
        if err:
            bits.append("stderr")
        return " ".join(bits) or "(no output)"
    if name == "Read" and isinstance(s, dict):
        fi = s.get("file") or {}
        return f"{os.path.basename(fi.get('filePath',''))}  {fi.get('numLines','?')} lines"
    if name == "Grep" and isinstance(s, dict):
        return f"{s.get('numFiles','?')} files, {s.get('numLines','?')} lines"
    return _oneline(ev.get("text", ""), 80)


def _oneline(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ── commands ─────────────────────────────────────────────────────────────────
def cmd_inspect(args):
    path = args.jsonl or latest_transcript(os.getcwd())
    if not path or not os.path.exists(path):
        sys.exit(f"no transcript at {path}")
    import collections
    types = collections.Counter()
    n = 0
    for o in tail(path, from_start=True, wait=False):
        n += 1
        types[o.get("type")] += 1
    print(f"{path}\n{n} records")
    for t, c in types.most_common():
        print(f"  {c:6d}  {t}")


def cmd_watch(args):
    cwd = os.path.abspath(args.cwd or os.getcwd())
    if args.session and args.session != "latest":
        path = transcript_path(cwd, args.session)
    else:
        path = latest_transcript(cwd)
    if not path:
        sys.exit(f"no transcript found under {transcript_dir(cwd)}")
    print(f"\033[2m# tailing {path}\033[0m", file=sys.stderr)
    ex = Extractor(show_all=args.all)
    for o in tail(path, from_start=args.all, wait=True):
        for ev in ex.feed(o):
            print(render(ev))


def cmd_start(args):
    cwd = os.path.abspath(args.cwd or os.getcwd())
    sid = str(uuidlib.uuid4())
    path = transcript_path(cwd, sid)
    base = ["claude", "--session-id", sid]
    if args.model:
        base += ["--model", args.model]
    print(f"\033[2m# session {sid}\n# transcript {path}\033[0m", file=sys.stderr)

    if args.interactive:
        # Real product path: hand this to the terminal/ PTY module; rides the
        # interactive subscription billing bucket. We just print it here.
        print("# interactive launch command (run in a PTY / terminal view):")
        print("  " + " ".join(base))
        print(f"# then: cc_trace.py watch --session {sid}")
        return

    # Headless proof: -p drives one turn so the loop runs end-to-end here.
    cmd = base + ["-p", args.prompt or "List the files in this directory, then stop."]
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ex = Extractor(show_all=args.all)
    last = [time.time()]

    def stop():  # end once the driver exited and the stream has been idle
        return proc.poll() is not None and (time.time() - last[0]) > 1.0

    try:
        for o in tail(path, from_start=True, wait=True, stop=stop):
            last[0] = time.time()
            for ev in ex.feed(o):
                print(render(ev))
                sys.stdout.flush()
    finally:
        if proc.poll() is None:
            proc.terminate()


def main():
    ap = argparse.ArgumentParser(description="Trace+extract Claude Code transcripts")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inspect")
    p.add_argument("jsonl", nargs="?")
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("watch")
    p.add_argument("--cwd")
    p.add_argument("--session", help="session uuid, or 'latest'")
    p.add_argument("--all", action="store_true", help="from start + show meta/system")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("start")
    p.add_argument("--cwd")
    p.add_argument("-p", "--prompt")
    p.add_argument("--model")
    p.add_argument("--interactive", action="store_true")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_start)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
