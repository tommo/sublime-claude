"""Load a short tail of a saved transcript to paint when reopening history."""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional


MIN_CHARS = 500
MAX_TURNS = 8

_USER_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)


def display_prompt(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    m = _USER_QUERY.search(text)
    if m:
        return m.group(1).strip()
    return text


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return content.get("text") or ""
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text" or "text" in b:
                    parts.append(b.get("text") or "")
        return "\n".join(p for p in parts if p)
    return ""


def _new_turn(prompt: str) -> dict:
    return {"prompt": prompt or "", "reply": "", "tools": []}


def select_preview(turns: List[dict], min_chars: int = MIN_CHARS,
                   max_turns: int = MAX_TURNS) -> List[dict]:
    """Last turn, plus earlier ones if the tail is too short."""
    if not turns:
        return []
    out: List[dict] = []
    total = 0
    for t in reversed(turns):
        out.append(t)
        total += len(t.get("prompt") or "") + len(t.get("reply") or "")
        if total >= min_chars or len(out) >= max_turns:
            break
    out.reverse()
    return out


def parse_claude_jsonl(path: str) -> List[dict]:
    turns: List[dict] = []
    cur = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("isMeta") or rec.get("isSidechain"):
                    continue
                et = rec.get("type")
                if et == "user":
                    msg = rec.get("message") or {}
                    content = msg.get("content", [])
                    if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content
                    ):
                        continue
                    prompt = _flatten(content)
                    if prompt:
                        cur = _new_turn(prompt)
                        turns.append(cur)
                elif et == "assistant" and cur is not None:
                    msg = rec.get("message") or {}
                    content = msg.get("content", [])
                    if isinstance(content, str):
                        cur["reply"] += content
                    elif isinstance(content, list):
                        for b in content:
                            if not isinstance(b, dict):
                                continue
                            if b.get("type") == "text":
                                cur["reply"] += b.get("text") or ""
                            elif b.get("type") == "tool_use" and b.get("name"):
                                cur["tools"].append(b["name"])
    except OSError:
        return []
    return turns


def parse_grok_chat(path: str) -> List[dict]:
    turns: List[dict] = []
    cur = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                et = rec.get("type")
                if et == "user":
                    prompt = _flatten(rec.get("content"))
                    if prompt:
                        cur = _new_turn(prompt)
                        turns.append(cur)
                elif et == "assistant" and cur is not None:
                    cur["reply"] += _flatten(rec.get("content"))
                    for tc in rec.get("tool_calls") or []:
                        if isinstance(tc, dict) and tc.get("name"):
                            cur["tools"].append(tc["name"])
    except OSError:
        return []
    return turns


def find_grok_chat(session_id: str, cwd: str = "") -> Optional[str]:
    if not session_id:
        return None
    root = os.path.expanduser("~/.grok/sessions")
    if cwd:
        enc = cwd.replace("/", "%2F")
        cand = os.path.join(root, enc, session_id, "chat_history.jsonl")
        if os.path.isfile(cand):
            return cand
    if not os.path.isdir(root):
        return None
    try:
        for name in os.listdir(root):
            cand = os.path.join(root, name, session_id, "chat_history.jsonl")
            if os.path.isfile(cand):
                return cand
    except OSError:
        return None
    return None


def load_turns(session_id: str, backend: str, cwd: str = "",
               claude_jsonl: str = "") -> List[dict]:
    backend = (backend or "claude").lower()
    if backend == "grok":
        path = find_grok_chat(session_id, cwd)
        return parse_grok_chat(path) if path else []
    if claude_jsonl and os.path.isfile(claude_jsonl):
        return parse_claude_jsonl(claude_jsonl)
    return []


def format_turn_body(turn: dict) -> str:
    tools = turn.get("tools") or []
    reply = (turn.get("reply") or "").rstrip()
    parts = []
    if tools:
        shown = tools[:24]
        lines = [f"⚙ {n}" for n in shown]
        extra = len(tools) - len(shown)
        if extra > 0:
            lines.append(f"⚙ … +{extra} more")
        parts.append("\n".join(lines))
    if reply:
        parts.append(reply)
    return "\n\n".join(parts)
