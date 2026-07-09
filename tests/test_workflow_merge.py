#!/usr/bin/env python3
"""Pure-logic POC for workflow task_progress merge (no Sublime required).

Mirrors session.Session._on_sys_task_progress accumulation rules:
  - ticks are PARTIAL (agents that changed this tick only)
  - key = (phaseIndex, index)
  - no-op when signature (state, toolCalls, tokens, lastToolName) unchanged

Run:  python3 tests/test_workflow_merge.py
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

WF_DONE = ("done", "completed", "success")


def apply_ticks(ticks: List[dict]) -> Dict[str, Any]:
    """Accumulate a sequence of task_progress data payloads into one workflow."""
    wf: Dict[str, Any] = {"agents": {}, "phases": {}, "summary": "", "sig": None}
    for data in ticks:
        wp = data.get("workflow_progress") or []
        if not wp:
            continue
        wf["summary"] = data.get("summary", "") or wf["summary"]
        touched = False
        for e in wp:
            if not isinstance(e, dict):
                continue
            if e.get("type") == "workflow_phase":
                idx = e.get("index")
                wf["phases"][idx] = e.get("title") or wf["phases"].get(idx, "")
            elif e.get("type") == "workflow_agent":
                key = (e.get("phaseIndex"), e.get("index"))
                wf["agents"][key] = {**wf["agents"].get(key, {}), **e}
                touched = True
        if not touched:
            continue
        agents = list(wf["agents"].values())
        sig = tuple(sorted(
            (str(a.get("phaseIndex")), a.get("index") or 0, a.get("state"),
             a.get("toolCalls") or 0, a.get("tokens") or 0, a.get("lastToolName") or "")
            for a in agents))
        if wf.get("sig") == sig:
            continue
        wf["sig"] = sig
        wf["done"] = sum(1 for a in agents if a.get("state") in WF_DONE)
        wf["total"] = len(agents)
        wf["completed"] = bool(agents) and all(a.get("state") in WF_DONE for a in agents)
    return wf


def fixture_ticks(base_ms: int | None = None) -> List[dict]:
    base_ms = base_ms or int(time.time() * 1000)
    tid = "poc-wf-1"
    summary = "POC pick-registry-rewrite"
    return [
        {
            "task_id": tid,
            "summary": summary,
            "workflow_progress": [
                {"type": "workflow_phase", "index": 0, "title": "Engine"},
                {"type": "workflow_phase", "index": 1, "title": "Editor"},
                {"type": "workflow_agent", "phaseIndex": 0, "index": 0,
                 "label": "scene.common", "state": "progress", "model": "claude-opus-4",
                 "attempt": 1, "lastToolName": "Read", "lastToolSummary": "scene.nim",
                 "toolCalls": 3, "tokens": 12000, "startedAt": base_ms - 40000},
                {"type": "workflow_agent", "phaseIndex": 1, "index": 0,
                 "label": "inspector.panel", "state": "queued", "model": "claude-sonnet-4",
                 "attempt": 1, "toolCalls": 0, "tokens": 0},
            ],
        },
        # Partial: only scene.common advanced — count must stay 2, not jump to 1
        {
            "task_id": tid,
            "summary": summary,
            "workflow_progress": [
                {"type": "workflow_agent", "phaseIndex": 0, "index": 0,
                 "label": "scene.common", "state": "done", "model": "claude-opus-4",
                 "attempt": 1, "resultPreview": "rewrote registry",
                 "toolCalls": 16, "tokens": 31000, "startedAt": base_ms - 40000,
                 "durationMs": 100000},
            ],
        },
        {
            "task_id": tid,
            "summary": summary,
            "workflow_progress": [
                {"type": "workflow_agent", "phaseIndex": 1, "index": 0,
                 "label": "inspector.panel", "state": "progress", "model": "claude-sonnet-4",
                 "attempt": 1, "lastToolName": "Bash", "lastToolSummary": "nim c",
                 "toolCalls": 4, "tokens": 6100, "startedAt": base_ms - 15000},
                {"type": "workflow_agent", "phaseIndex": 1, "index": 1,
                 "label": "gizmo.transform", "state": "progress", "model": "claude-sonnet-4",
                 "attempt": 1, "lastToolName": "Edit", "lastToolSummary": "gizmo.nim",
                 "toolCalls": 2, "tokens": 3400, "startedAt": base_ms - 8000},
            ],
        },
        # Completion
        {
            "task_id": tid,
            "summary": summary,
            "workflow_progress": [
                {"type": "workflow_agent", "phaseIndex": 1, "index": 0,
                 "label": "inspector.panel", "state": "done", "model": "claude-sonnet-4",
                 "attempt": 1, "resultPreview": "panel wired",
                 "toolCalls": 9, "tokens": 12400, "startedAt": base_ms - 15000,
                 "durationMs": 48000},
                {"type": "workflow_agent", "phaseIndex": 1, "index": 1,
                 "label": "gizmo.transform", "state": "done", "model": "claude-sonnet-4",
                 "attempt": 1, "resultPreview": "gizmo ok",
                 "toolCalls": 7, "tokens": 8900, "startedAt": base_ms - 8000,
                 "durationMs": 31000},
            ],
        },
    ]


def test_partial_merge_keeps_agents() -> None:
    ticks = fixture_ticks()
    mid = apply_ticks(ticks[:2])
    assert mid["total"] == 2, mid
    assert mid["done"] == 1, mid
    labels = {a["label"] for a in mid["agents"].values()}
    assert labels == {"scene.common", "inspector.panel"}, labels
    states = {a["label"]: a["state"] for a in mid["agents"].values()}
    assert states["scene.common"] == "done"
    assert states["inspector.panel"] == "queued"


def test_grows_on_new_agents() -> None:
    mid = apply_ticks(fixture_ticks()[:3])
    assert mid["total"] == 3
    assert mid["done"] == 1
    assert not mid["completed"]


def test_completion() -> None:
    wf = apply_ticks(fixture_ticks())
    assert wf["total"] == 3
    assert wf["done"] == 3
    assert wf["completed"] is True
    assert set(wf["phases"].values()) == {"Engine", "Editor"}


def test_noop_same_sig() -> None:
    ticks = fixture_ticks()[:1]
    # duplicate identical agent payload should not change sig-driven fields wrongly
    ticks.append(ticks[0])
    wf = apply_ticks(ticks)
    assert wf["total"] == 2
    assert wf["done"] == 0


def main() -> None:
    tests = [
        test_partial_merge_keeps_agents,
        test_grows_on_new_agents,
        test_completion,
        test_noop_same_sig,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    wf = apply_ticks(fixture_ticks())
    print(f"\nfinal: {wf['done']}/{wf['total']} completed={wf['completed']}")
    print(f"phases: {wf['phases']}")
    for a in sorted(wf["agents"].values(), key=lambda x: (x.get("phaseIndex") or 0, x.get("index") or 0)):
        print(f"  {a.get('state'):8} {a.get('label'):20} tools={a.get('toolCalls')} tok={a.get('tokens')}")
    if failed:
        raise SystemExit(1)
    print("\nall passed")


if __name__ == "__main__":
    main()
