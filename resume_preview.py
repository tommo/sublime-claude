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
                    if not prompt or _skip_synthetic_prompt(prompt):
                        # Keep cur so the following assistant tools stay on
                        # the real user turn, not a fake ◎ system-reminder.
                        continue
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


def _kimi_prompt_text(rec: dict) -> str:
    inp = rec.get("input")
    if isinstance(inp, list):
        return _flatten(inp)
    if isinstance(inp, dict):
        return _flatten(inp)
    if isinstance(inp, str):
        return inp.strip()
    return ""


def _skip_synthetic_prompt(text: str, origin: str = "") -> bool:
    """Host/agent injects, not a real ◎ user turn (Grok stores these as type=user)."""
    if origin == "task":
        return True
    t = (text or "").lstrip()
    if t.startswith((
        "<system-reminder>",
        "<task-notification>",
        "<notification",
        "<user_info>",
    )):
        return True
    # Reminder after other wrappers
    if "<system-reminder>" in t[:200]:
        return True
    return False


def _kimi_skip_prompt(text: str, origin: str) -> bool:
    return _skip_synthetic_prompt(text, origin)


def parse_kimi_wire(path: str) -> List[dict]:
    """Kimi agents/main/wire.jsonl → turns (user prompt + text + tools)."""
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
                kind = rec.get("type")
                if kind == "turn.cancel":
                    # Rejected prompt (agent_busy) — drop the empty turn.
                    if cur is not None and not cur.get("reply") and not cur.get("tools"):
                        turns.pop()
                        cur = turns[-1] if turns else None
                    continue
                if kind == "turn.prompt":
                    text = _kimi_prompt_text(rec)
                    origin = str((rec.get("origin") or {}).get("kind") or "")
                    if not text or _kimi_skip_prompt(text, origin):
                        continue
                    cur = _new_turn(text)
                    turns.append(cur)
                    continue
                if kind != "context.append_loop_event" or cur is None:
                    continue
                ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
                et = ev.get("type")
                if et == "tool.call" and ev.get("name"):
                    cur["tools"].append(str(ev["name"]))
                elif et == "content.part":
                    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
                    if part.get("type") == "text" and part.get("text"):
                        cur["reply"] += part["text"]
    except OSError:
        return []
    return turns


def find_kimi_wire(session_id: str, cwd: str = "") -> Optional[str]:
    if not session_id:
        return None
    root = os.path.expanduser("~/.kimi-code/sessions")
    if not os.path.isdir(root):
        return None
    names = [session_id]
    if not session_id.startswith("session_"):
        names.append("session_" + session_id)
    try:
        for wd in os.listdir(root):
            base = os.path.join(root, wd)
            if not os.path.isdir(base):
                continue
            for name in names:
                cand = os.path.join(base, name, "agents", "main", "wire.jsonl")
                if os.path.isfile(cand):
                    return cand
    except OSError:
        return None
    return None


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
    if backend == "kimi":
        path = find_kimi_wire(session_id, cwd)
        return parse_kimi_wire(path) if path else []
    if claude_jsonl and os.path.isfile(claude_jsonl):
        return parse_claude_jsonl(claude_jsonl)
    return []


def format_turn_body(turn: dict) -> str:
    tools = turn.get("tools") or []
    reply = (turn.get("reply") or "").rstrip()
    parts = []
    if tools:
        collapsed = []
        for n in tools:
            if collapsed and collapsed[-1][0] == n:
                collapsed[-1] = (n, collapsed[-1][1] + 1)
            else:
                collapsed.append((n, 1))
        shown = collapsed[:12]
        lines = [
            f"⚙ {n}" + (f" ×{c}" if c > 1 else "")
            for n, c in shown
        ]
        extra = len(collapsed) - len(shown)
        if extra > 0:
            lines.append(f"⚙ … +{extra} more")
        parts.append("\n".join(lines))
    if reply:
        parts.append(reply)
    return "\n\n".join(parts)
