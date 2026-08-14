"""Pull AskUserQuestion tool_call / resolved / tool_result from a Kimi wire."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def load_events(path: str) -> List[dict]:
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            obj["_line"] = i
            out.append(obj)
    return out


def extract_ask(path: str) -> Optional[Dict[str, Any]]:
    events = load_events(path)
    call = None
    resolved = None
    result = None
    for ev in events:
        if ev.get("type") == "context.append_loop_event":
            inner = ev.get("event") or {}
            if inner.get("type") == "tool.call" and inner.get("name") == "AskUserQuestion":
                call = inner
            if inner.get("type") == "tool.result" and call and (
                    inner.get("toolCallId") == call.get("toolCallId")):
                result = inner
        if ev.get("type") == "interaction.resolved" and call and (
                ev.get("id") == call.get("toolCallId")
                or ev.get("id") == ev.get("id")):
            if call and ev.get("id") == call.get("toolCallId"):
                resolved = ev
    if not call:
        return None
    questions = ((call.get("args") or {}).get("questions")) or []
    raw_result = ""
    if result:
        raw_result = str((result.get("result") or {}).get("output") or "")
    parsed = {}
    if raw_result.strip().startswith("{"):
        try:
            parsed = json.loads(raw_result)
        except json.JSONDecodeError:
            parsed = {}
    answers = parsed.get("answers") if isinstance(parsed, dict) else {}
    if not answers and resolved:
        answers = resolved.get("response") or {}
    return {
        "questions": questions,
        "n_questions": len(questions),
        "resolved": (resolved or {}).get("response") or {},
        "tool_result_answers": answers or {},
        "tool_result_raw": raw_result[:500],
        "toolCallId": call.get("toolCallId"),
    }
