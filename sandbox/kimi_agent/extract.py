#!/usr/bin/env python3
"""Dump Kimi Agent subagent artifacts from a session dir.

Sources:
  ~/.kimi-code/sessions/<wd>/<session_id>/agents/main/wire.jsonl  (tool.call Agent)
  .../agents/agent-N/wire.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from typing import Any, Dict, List, Optional


def find_session_dir(sid: str) -> Optional[str]:
    root = os.path.expanduser("~/.kimi-code/sessions")
    if not os.path.isdir(root):
        return None
    for wd in os.listdir(root):
        p = os.path.join(root, wd, sid)
        if os.path.isdir(os.path.join(p, "agents")):
            return p
    return None


def latest_session_dir() -> Optional[str]:
    root = os.path.expanduser("~/.kimi-code/sessions")
    best = None
    best_m = 0.0
    if not os.path.isdir(root):
        return None
    for wd in os.listdir(root):
        wdp = os.path.join(root, wd)
        if not os.path.isdir(wdp):
            continue
        for name in os.listdir(wdp):
            if not name.startswith("session_"):
                continue
            p = os.path.join(wdp, name, "agents")
            if not os.path.isdir(p):
                continue
            m = os.path.getmtime(os.path.join(wdp, name))
            if m > best_m:
                best_m = m
                best = os.path.join(wdp, name)
    return best


def agent_dirs(session_dir: str) -> List[str]:
    ad = os.path.join(session_dir, "agents")
    if not os.path.isdir(ad):
        return []
    names = sorted(os.listdir(ad))
    return [os.path.join(ad, n) for n in names if n != "main"]


def wire_types(path: str) -> Counter:
    c: Counter = Counter()
    if not os.path.isfile(path):
        return c
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            c[o.get("type") or "?"] += 1
    return c


def main_agent_calls(session_dir: str) -> List[dict]:
    path = os.path.join(session_dir, "agents", "main", "wire.jsonl")
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if '"Agent"' not in line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = o.get("event") if isinstance(o.get("event"), dict) else {}
            if ev.get("type") not in ("tool.call", "tool.result"):
                continue
            if ev.get("type") == "tool.call" and ev.get("name") != "Agent":
                continue
            if ev.get("type") == "tool.result":
                res = ev.get("result")
                if isinstance(res, dict):
                    text = str(res.get("output") or json.dumps(res))
                else:
                    text = res if isinstance(res, str) else json.dumps(res or {})
                if "agent_id:" not in text and "actual_subagent_type" not in text:
                    continue
            rec: Dict[str, Any] = {
                "line": i,
                "ev": ev.get("type"),
                "name": ev.get("name"),
            }
            if ev.get("type") == "tool.result":
                res = ev.get("result")
                if isinstance(res, dict):
                    text = str(res.get("output") or json.dumps(res))
                else:
                    text = res if isinstance(res, str) else json.dumps(res or {})
                rec["result_head"] = text[:240]
            out.append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", default="")
    ap.add_argument("--latest", action="store_true")
    args = ap.parse_args()
    if args.sid:
        d = find_session_dir(args.sid)
    else:
        d = latest_session_dir()
    if not d:
        print("FAIL no session dir")
        return 1
    print("session", d)
    kids = agent_dirs(d)
    print("child_agents", [os.path.basename(k) for k in kids])
    calls = main_agent_calls(d)
    print("main Agent events", len(calls))
    for c in calls[:8]:
        print(" ", c)
    for k in kids:
        w = os.path.join(k, "wire.jsonl")
        c = wire_types(w)
        print(os.path.basename(k), "wire", dict(c.most_common(8)))
    if not kids and not calls:
        print("FAIL no Agent calls and no agent-N dirs")
        return 1
    print("PASS extract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
