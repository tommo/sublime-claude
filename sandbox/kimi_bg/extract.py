#!/usr/bin/env python3
"""Extract Kimi bash/bg patterns from real session artifacts.

Sources:
  ~/.kimi-code/sessions/<wd>/<session_id>/agents/*/tasks/bash-*.json
  .../agents/*/wire.jsonl          (type=task.started|task.terminated)
  $TMPDIR/kimi_bridge.log          (ACP terminal/* + session/update titles)

Do not guess protocol from host code — run this on a live session first.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional


TASK_TYPES = ("task.started", "task.terminated")
TITLE_RE = re.compile(r'"title": "((?:\\.|[^"\\])*)"')
KIND_RE = re.compile(r'"kind": "([^"]+)"')
TERM_CREATE_RE = re.compile(
    r'terminal/create.*?"args": \["-c", "((?:\\.|[^"\\])*)"',
)
TERM_ID_RE = re.compile(r"terminal/create (term_\w+)")
BASH_ID_RE = re.compile(r"bash-[\w-]+")


def _unescape(s: str) -> str:
    try:
        return bytes(s, "utf-8").decode("unicode_escape")
    except Exception:
        return s.replace("\\n", "\n").replace('\\"', '"')


def load_task_files(session_dir: str) -> List[dict]:
    out = []
    for root, _dirs, files in os.walk(session_dir):
        if os.path.basename(root) != "tasks":
            continue
        for fn in files:
            if not (fn.startswith("bash-") and fn.endswith(".json")):
                continue
            path = os.path.join(root, fn)
            try:
                obj = json.load(open(path, encoding="utf-8"))
            except Exception as e:
                obj = {"_error": str(e)}
            obj["_path"] = path
            obj["_file"] = fn
            out.append(obj)
    out.sort(key=lambda o: o.get("startedAt") or 0)
    return out


def load_wire_tasks(session_dir: str) -> List[dict]:
    out = []
    for root, _dirs, files in os.walk(session_dir):
        if "wire.jsonl" not in files:
            continue
        path = os.path.join(root, "wire.jsonl")
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if '"task.started"' not in line and '"task.terminated"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") not in TASK_TYPES:
                    continue
                info = obj.get("info") if isinstance(obj.get("info"), dict) else {}
                out.append({
                    "line": i,
                    "wire": path,
                    "type": obj.get("type"),
                    "time": obj.get("time"),
                    "taskId": info.get("taskId"),
                    "status": info.get("status"),
                    "detached": info.get("detached"),
                    "command": info.get("command"),
                    "exitCode": info.get("exitCode"),
                    "has_output_tail": bool(obj.get("outputTail")),
                    "output_tail_len": len(obj.get("outputTail") or ""),
                })
    return out


def parse_bridge_log(path: str) -> dict:
    titles: Counter = Counter()
    kinds: Counter = Counter()
    acp: Counter = Counter()
    term_cmds: List[str] = []
    reading: List[str] = []
    starting_bg = 0
    bash_ids = Counter()
    if not path or not os.path.isfile(path):
        return {
            "path": path,
            "missing": True,
            "titles": {},
            "kinds": {},
            "acp": {},
            "terminal_cmds": [],
            "reading_output": [],
            "starting_background": 0,
            "bash_ids": {},
        }
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "terminal/" in line:
                m = re.search(r"terminal/\w+", line)
                if m:
                    acp[m.group(0)] += 1
            if "acp REQ terminal/create" in line:
                m = TERM_CREATE_RE.search(line)
                if m:
                    term_cmds.append(_unescape(m.group(1)))
            if "Starting background" in line:
                starting_bg += 1
            if "Reading output of task" in line:
                for bid in BASH_ID_RE.findall(line):
                    reading.append(bid)
            for bid in BASH_ID_RE.findall(line):
                bash_ids[bid] += 1
            if "acp update" in line or "tool_call" in line:
                for m in TITLE_RE.finditer(line):
                    titles[_unescape(m.group(1))[:120]] += 1
                for m in KIND_RE.finditer(line):
                    kinds[m.group(1)] += 1
    return {
        "path": path,
        "missing": False,
        "titles": dict(titles.most_common(80)),
        "kinds": dict(kinds),
        "acp": dict(acp),
        "terminal_cmds": term_cmds,
        "reading_output": sorted(set(reading)),
        "starting_background": starting_bg,
        "bash_ids": dict(bash_ids.most_common(40)),
    }


def _cmd_hit(task_cmd: str, term_cmds: Iterable[str]) -> bool:
    a = (task_cmd or "").strip()
    if not a:
        return False
    needle = a[:48]
    for t in term_cmds:
        if needle in t or t[:48] in a:
            return True
    return False


def correlate(tasks: List[dict], wire: List[dict], acp: dict) -> dict:
    term_cmds = acp.get("terminal_cmds") or []
    reading = set(acp.get("reading_output") or [])
    wire_ids = {w.get("taskId") for w in wire if w.get("taskId")}
    rows = []
    for t in tasks:
        tid = t.get("taskId") or (t.get("_file") or "").replace(".json", "")
        cmd = t.get("command") or ""
        rows.append({
            "taskId": tid,
            "status": t.get("status"),
            "detached": t.get("detached"),
            "timeoutMs": t.get("timeoutMs"),
            "exitCode": t.get("exitCode"),
            "in_wire": tid in wire_ids,
            "has_terminal_create": _cmd_hit(cmd, term_cmds),
            "taskoutput_poll": tid in reading,
            "command": cmd[:160],
        })
    title_keys = list((acp.get("titles") or {}).keys())
    running_titles = [t for t in title_keys if t.startswith("Running:")]
    starting = [t for t in title_keys if t.lower().startswith("starting background")]
    return {
        "tasks": rows,
        "n_tasks": len(rows),
        "n_detached": sum(1 for r in rows if r.get("detached")),
        "n_with_terminal": sum(1 for r in rows if r["has_terminal_create"]),
        "n_with_taskoutput": sum(1 for r in rows if r["taskoutput_poll"]),
        "n_in_wire": sum(1 for r in rows if r["in_wire"]),
        "n_running_titles": len(running_titles),
        "n_starting_background_titles": len(starting),
        "starting_background_log_hits": acp.get("starting_background") or 0,
    }


def infer_patterns(corr: dict, acp: dict) -> List[str]:
    pats = []
    if corr["n_detached"] and corr["n_detached"] == corr["n_tasks"]:
        pats.append("all bash-*.json tasks have detached=true")
    if corr["n_tasks"] and corr["n_with_terminal"] == corr["n_tasks"]:
        pats.append("every detached bash-* also has ACP terminal/create")
    if corr["starting_background_log_hits"] == 0:
        pats.append("ACP never uses title 'Starting background' (host gate misses)")
    if corr["n_running_titles"]:
        pats.append("execute titles are 'Running: <cmd>' (kind=execute)")
    if corr["n_with_taskoutput"]:
        pats.append("agent polls via title 'Reading output of task bash-…'")
    acp_n = acp.get("acp") or {}
    if acp_n.get("terminal/wait_for_exit") and acp_n.get("terminal/create"):
        pats.append(
            "Kimi still issues terminal/wait_for_exit after create "
            "(blocking ACP even for detached tasks)"
        )
    if acp_n.get("terminal/output", 0) > 100:
        pats.append("terminal/output is polled at high rate while wait_for_exit is outstanding")
    if corr["n_in_wire"] == corr["n_tasks"] and corr["n_tasks"]:
        pats.append("wire.jsonl task.started/terminated matches every task file")
    return pats


def extract(session_dir: str, bridge_log: Optional[str] = None) -> dict:
    tasks = load_task_files(session_dir)
    wire = load_wire_tasks(session_dir)
    acp = parse_bridge_log(bridge_log or "")
    corr = correlate(tasks, wire, acp)
    return {
        "session_dir": session_dir,
        "n_task_files": len(tasks),
        "task_statuses": dict(Counter(t.get("status") for t in tasks)),
        "wire_events": len(wire),
        "acp": {
            "missing": acp.get("missing"),
            "path": acp.get("path"),
            "counts": acp.get("acp"),
            "kinds": acp.get("kinds"),
            "starting_background": acp.get("starting_background"),
            "reading_output": acp.get("reading_output"),
            "n_terminal_cmds": len(acp.get("terminal_cmds") or []),
            "sample_titles": list((acp.get("titles") or {}).keys())[:25],
        },
        "correlation": corr,
        "patterns": infer_patterns(corr, acp),
    }


def newest_session(root: Optional[str] = None) -> Optional[str]:
    root = root or os.path.expanduser("~/.kimi-code/sessions")
    best = None
    best_m = 0.0
    if not os.path.isdir(root):
        return None
    for dirpath, dirnames, _files in os.walk(root):
        base = os.path.basename(dirpath)
        if not base.startswith("session_"):
            continue
        try:
            m = os.path.getmtime(dirpath)
        except OSError:
            continue
        if m > best_m:
            best_m = m
            best = dirpath
    return best


def default_bridge_log() -> str:
    return os.path.join(
        os.environ.get("TMPDIR")
        or os.environ.get("TEMP")
        or "/tmp",
        "kimi_bridge.log",
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", help="Kimi session directory")
    ap.add_argument("--latest", action="store_true", help="Use newest ~/.kimi-code session")
    ap.add_argument("--bridge-log", default=None, help="kimi_bridge.log (ACP)")
    ap.add_argument("-o", "--out", help="Write JSON report here")
    args = ap.parse_args(argv)
    sess = args.session
    if args.latest or not sess:
        sess = sess or newest_session()
    if not sess or not os.path.isdir(sess):
        print("no session dir", file=sys.stderr)
        return 2
    blog = args.bridge_log or default_bridge_log()
    report = extract(sess, blog)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
