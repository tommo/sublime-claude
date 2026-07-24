#!/usr/bin/env python3
"""Grok *conversation-only* undo PoC — session history + ACP probe.

Product scope: conversation rewind only. **Never** project file_snapshots /
code restore. Session JSONL truncate (chat_history, rewind_points, updates)
is still required so the model forgets later turns after session/load.

Read-only by default. Validates:

1. ``prompt_index`` is dense (model turns), not "Nth user message".
2. Bridge must cut by real ``prompt_index``, not Nth ``<user_query>``.
3. Map: chat ``prompt_index`` + updates ``_meta.promptIndex``.
4. ``updates.jsonl`` needs **stream-cut** (not filter-only).
5. ACP ``_x.ai/rewind/execute`` conversation_only is often success:false —
   session truncate is the reliable path.
6. Live ``_x.ai/rewind/points`` returns previews (disk previews often null).

Usage::

    python3 prototype/grok_rewind_poc.py list
    python3 prototype/grok_rewind_poc.py analyze [session]
    python3 prototype/grok_rewind_poc.py map [session] [--last N]
    python3 prototype/grok_rewind_poc.py dry-cut [session] --target N
    python3 prototype/grok_rewind_poc.py compare [session]
    python3 prototype/grok_rewind_poc.py sandbox-cut [session] --target N
        # copies session to /tmp, applies proposed cut, reports sizes
    python3 prototype/grok_rewind_poc.py acp-points [session]
    python3 prototype/grok_rewind_poc.py acp-execute [session] --target N
        # live execute on a SANDBOX copy only (unless --live-danger)

``--apply`` against a real session is intentionally omitted except via
``sandbox-cut`` (isolated copy) or ``acp-execute --live-danger``.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SESSIONS_ROOT = Path.home() / ".grok" / "sessions"


# ── helpers ──────────────────────────────────────────────────────────────

def extract_text(obj: dict) -> str:
    c = obj.get("content") or ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
        return "".join(parts)
    return ""


def user_query_body(text: str) -> Optional[str]:
    if "<user_query>" not in text:
        return None
    try:
        body = text.split("<user_query>", 1)[1]
        return body.split("</user_query>", 1)[0].strip()
    except IndexError:
        return text.strip() or None


def load_jsonl(path: Path) -> List[dict]:
    out: List[dict] = []
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def safe_idx(o: dict, *keys: str) -> Optional[int]:
    for k in keys:
        if k in o and o[k] is not None:
            try:
                return int(o[k])
            except (TypeError, ValueError):
                pass
    return None


def find_session_dirs(root: Path = SESSIONS_ROOT) -> List[Path]:
    if not root.is_dir():
        return []
    found: List[Path] = []
    for dirpath, _dirs, files in os.walk(root):
        if "rewind_points.jsonl" in files:
            found.append(Path(dirpath))
    found.sort(key=lambda p: (p / "rewind_points.jsonl").stat().st_mtime, reverse=True)
    return found


def resolve_session(arg: Optional[str]) -> Path:
    if arg:
        p = Path(arg).expanduser()
        if p.is_dir() and (p / "rewind_points.jsonl").is_file():
            return p
        for d in find_session_dirs():
            if d.name == arg:
                return d
        raise SystemExit(f"session not found: {arg}")

    enc = str(Path.cwd()).replace("/", "%2F")
    preferred = SESSIONS_ROOT / enc
    if preferred.is_dir():
        sub = sorted(
            [d for d in preferred.iterdir()
             if d.is_dir() and (d / "rewind_points.jsonl").is_file()],
            key=lambda d: (d / "rewind_points.jsonl").stat().st_mtime,
            reverse=True,
        )
        if sub:
            return sub[0]
    dirs = find_session_dirs()
    if not dirs:
        raise SystemExit(f"no sessions under {SESSIONS_ROOT}")
    return dirs[0]


def decode_cwd_from_session(sdir: Path) -> str:
    parent = sdir.parent.name
    if "%2F" in parent:
        return parent.replace("%2F", "/")
    return str(Path.cwd())


# ── prompt_index map ─────────────────────────────────────────────────────

@dataclass
class PromptEntry:
    prompt_index: int
    source: str  # chat | updates | both
    chat_line: Optional[int] = None
    draft: str = ""
    in_rewind_points: bool = False
    has_file_snapshots: bool = False


def build_map(sdir: Path) -> Dict[int, PromptEntry]:
    """Merge chat.prompt_index + updates._meta.promptIndex + rewind_points."""
    m: Dict[int, PromptEntry] = {}

    # rewind_points
    rp_path = sdir / "rewind_points.jsonl"
    if rp_path.is_file():
        for o in load_jsonl(rp_path):
            idx = safe_idx(o, "prompt_index", "promptIndex")
            if idx is None:
                continue
            e = m.setdefault(idx, PromptEntry(prompt_index=idx, source="rp"))
            e.in_rewind_points = True
            e.has_file_snapshots = bool(o.get("file_snapshots"))
            prev = o.get("prompt_preview") or o.get("prompt_text") or ""
            if prev and not e.draft:
                e.draft = str(prev)[:200]

    # chat_history user rows
    ch_path = sdir / "chat_history.jsonl"
    if ch_path.is_file():
        for i, o in enumerate(load_jsonl(ch_path)):
            if o.get("type") != "user":
                continue
            idx = safe_idx(o, "prompt_index", "promptIndex")
            text = extract_text(o)
            body = user_query_body(text)
            if idx is None:
                # synthetic / preamble — skip unless we need ordinal fallback
                continue
            e = m.setdefault(idx, PromptEntry(prompt_index=idx, source="chat"))
            if e.source == "rp":
                e.source = "both"
            elif e.source == "updates":
                e.source = "both"
            else:
                e.source = "chat" if e.source == "chat" else e.source
            e.chat_line = i
            if body:
                e.draft = body[:200]

    # updates user_message_chunk (full history even after chat compact)
    up_path = sdir / "updates.jsonl"
    if up_path.is_file():
        # accumulate text per promptIndex
        texts: Dict[int, str] = OrderedDict()
        with up_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "user_message_chunk" not in line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                u = (o.get("params") or {}).get("update") or {}
                if u.get("sessionUpdate") != "user_message_chunk":
                    continue
                meta = u.get("_meta") or {}
                idx = safe_idx(meta, "promptIndex", "prompt_index")
                if idx is None:
                    continue
                chunk = ((u.get("content") or {}).get("text") or "")
                texts[idx] = texts.get(idx, "") + chunk
        for idx, text in texts.items():
            e = m.setdefault(idx, PromptEntry(prompt_index=idx, source="updates"))
            if e.source == "rp":
                e.source = "both"
            elif e.source == "chat":
                e.source = "both"
            elif e.source not in ("both", "chat", "updates"):
                e.source = "updates"
            if not e.draft:
                body = user_query_body(text)
                if body:
                    e.draft = body[:200]
                elif text.strip() and len(text) < 400:
                    e.draft = text.strip()[:200]
    return m


def human_prompts(m: Dict[int, PromptEntry]) -> List[PromptEntry]:
    """Prefer entries that look like real user turns (have draft from user_query)."""
    out = []
    for idx in sorted(m):
        e = m[idx]
        if e.draft and e.chat_line is not None:
            out.append(e)
        elif e.draft and e.source in ("updates", "both", "chat") and e.draft:
            # updates-only with short draft — keep if not system-ish
            d = e.draft
            if d.startswith("<") and "user_query" not in d.lower():
                continue
            if "system-reminder" in d or "user_info" in d:
                continue
            out.append(e)
    return out


# ── analysis ─────────────────────────────────────────────────────────────

@dataclass
class UserTurn:
    line_index: int
    body: str
    synthetic: Optional[str] = None
    false_positive: bool = False
    source_type: str = "user"
    prompt_index: Optional[int] = None


@dataclass
class SessionReport:
    session_dir: Path
    rp_count: int = 0
    rp_min: Optional[int] = None
    rp_max: Optional[int] = None
    rp_with_preview: int = 0
    rp_with_snapshots: int = 0
    chat_lines: int = 0
    chat_types: Counter = field(default_factory=Counter)
    real_user_queries: List[UserTurn] = field(default_factory=list)
    chat_with_pi: int = 0
    false_pos: List[UserTurn] = field(default_factory=list)
    updates_lines: int = 0
    updates_user_chunks: int = 0
    updates_size: int = 0
    map_size: int = 0
    human_count: int = 0


def analyze(sdir: Path) -> SessionReport:
    rep = SessionReport(session_dir=sdir)
    rp_path = sdir / "rewind_points.jsonl"
    ch_path = sdir / "chat_history.jsonl"
    up_path = sdir / "updates.jsonl"

    idxs: List[int] = []
    if rp_path.is_file():
        for o in load_jsonl(rp_path):
            idx = safe_idx(o, "prompt_index")
            if idx is None:
                continue
            idxs.append(idx)
            if o.get("prompt_preview") or o.get("prompt_text"):
                rep.rp_with_preview += 1
            if o.get("file_snapshots"):
                rep.rp_with_snapshots += 1
    if idxs:
        rep.rp_count = len(idxs)
        rep.rp_min, rep.rp_max = min(idxs), max(idxs)

    chat = load_jsonl(ch_path)
    rep.chat_lines = len(chat)
    user_turns = 0
    for i, o in enumerate(chat):
        t = o.get("type") or "?"
        rep.chat_types[t] += 1
        text = extract_text(o)
        pi = safe_idx(o, "prompt_index", "promptIndex")
        if t != "user":
            if "<user_query>" in text:
                body = user_query_body(text) or ""
                rep.false_pos.append(
                    UserTurn(i, body[:80], o.get("synthetic_reason"), True, t, pi)
                )
            continue
        if pi is not None:
            rep.chat_with_pi += 1
        if o.get("synthetic_reason"):
            continue
        if "<user_query>" not in text and user_turns == 0 and not text.strip():
            continue
        if "<user_query>" not in text:
            continue
        body = user_query_body(text) or text[:80]
        rep.real_user_queries.append(
            UserTurn(i, body[:120], None, False, "user", pi)
        )
        user_turns += 1

    if up_path.is_file():
        rep.updates_size = up_path.stat().st_size
        n = umc = 0
        with up_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                n += 1
                if "user_message_chunk" in line:
                    umc += 1
        rep.updates_lines = n
        rep.updates_user_chunks = umc

    m = build_map(sdir)
    rep.map_size = len(m)
    rep.human_count = len(human_prompts(m))
    return rep


def print_report(rep: SessionReport) -> None:
    print(f"session: {rep.session_dir}")
    print(f"  rewind_points: count={rep.rp_count} range=[{rep.rp_min}..{rep.rp_max}] "
          f"preview={rep.rp_with_preview} snapshots={rep.rp_with_snapshots}")
    print(f"  chat_history:  lines={rep.chat_lines} types={dict(rep.chat_types)}")
    print(f"  bridge user_query turns: {len(rep.real_user_queries)} "
          f"(of which chat.prompt_index set: "
          f"{sum(1 for t in rep.real_user_queries if t.prompt_index is not None)})")
    for t in rep.real_user_queries[:6]:
        print(f"    [chat#{t.line_index} pi={t.prompt_index}] {t.body!r}")
    if len(rep.real_user_queries) > 6:
        print(f"    ... +{len(rep.real_user_queries) - 6} more")
    print(f"  chat user rows with prompt_index field: {rep.chat_with_pi}")
    print(f"  false-positive <user_query> in non-user rows: {len(rep.false_pos)}")
    print(f"  updates.jsonl: lines={rep.updates_lines} "
          f"size={rep.updates_size // 1024}KiB umc={rep.updates_user_chunks}")
    print(f"  merged map size: {rep.map_size}  human-like prompts: {rep.human_count}")

    print("\n  VERDICT:")
    n_uq = len(rep.real_user_queries)
    if rep.rp_count and n_uq and rep.rp_count > n_uq * 2:
        print(f"    ✗ prompt_index ≠ Nth user turn "
              f"(rp={rep.rp_count} vs user_query={n_uq})")
    if rep.chat_with_pi:
        print(f"    ✓ chat carries prompt_index on {rep.chat_with_pi} user rows "
              f"— correct cut key")
    else:
        print("    ✗ no chat.prompt_index (compacted?) — use updates map")
    if rep.rp_with_preview == 0 and rep.rp_count:
        print("    ✗ all prompt_preview null on disk — draft from chat/updates")
    if rep.updates_lines:
        print("    ✗ updates.jsonl not truncated by bridge today")
    if rep.map_size:
        print(f"    ✓ updates._meta.promptIndex rebuilds full map ({rep.map_size} entries)")


# ── dry-cut strategies ───────────────────────────────────────────────────

@dataclass
class DryCutResult:
    strategy: str
    target: int
    cut_at: Optional[int]
    kept_chat_lines: int
    dropped_chat_lines: int
    kept_rp: int
    dropped_rp: int
    kept_updates: Optional[int] = None
    dropped_updates: Optional[int] = None
    draft: str = ""
    notes: List[str] = field(default_factory=list)


def dry_cut_user_query(sdir: Path, target: int) -> DryCutResult:
    """Bridge today: target = Nth <user_query> turn."""
    chat = load_jsonl(sdir / "chat_history.jsonl")
    rps = load_jsonl(sdir / "rewind_points.jsonl")
    draft = ""
    kept_rp = dropped_rp = 0
    for o in rps:
        idx = safe_idx(o, "prompt_index")
        if idx is None:
            continue
        if idx < target:
            kept_rp += 1
        else:
            if idx == target:
                draft = o.get("prompt_preview") or o.get("prompt_text") or draft
            dropped_rp += 1

    user_turns = 0
    cut_at = None
    for i, o in enumerate(chat):
        if o.get("type") != "user" or o.get("synthetic_reason"):
            continue
        text = extract_text(o)
        if "<user_query>" not in text and user_turns == 0 and not text.strip():
            continue
        if "<user_query>" not in text:
            continue
        if user_turns == target:
            cut_at = i
            if not draft:
                draft = user_query_body(text) or ""
            break
        user_turns += 1

    notes = []
    if cut_at is None:
        notes.append(
            f"NO CUT: only {user_turns} user_query turns; target={target}"
        )
    else:
        notes.append(f"keep chat [0..{cut_at}) drop [{cut_at}..]")

    kept = cut_at if cut_at is not None else len(chat)
    dropped = 0 if cut_at is None else len(chat) - cut_at
    return DryCutResult(
        strategy="user_query (bridge today)",
        target=target,
        cut_at=cut_at,
        kept_chat_lines=kept,
        dropped_chat_lines=dropped,
        kept_rp=kept_rp,
        dropped_rp=dropped_rp,
        draft=str(draft or "")[:200],
        notes=notes,
    )


def dry_cut_by_prompt_index(sdir: Path, target: int) -> DryCutResult:
    """Proposed: treat target as real prompt_index.

    Chat: cut at first line whose prompt_index >= target, else first
    user_query whose mapped pi >= target from updates, else fail closed.
    RP: keep prompt_index < target.
    Updates: drop lines whose _meta.promptIndex >= target (or nested).
    """
    chat = load_jsonl(sdir / "chat_history.jsonl")
    rps = load_jsonl(sdir / "rewind_points.jsonl")
    m = build_map(sdir)

    draft = ""
    if target in m and m[target].draft:
        draft = m[target].draft
    for o in rps:
        if safe_idx(o, "prompt_index") == target:
            draft = draft or (o.get("prompt_preview") or o.get("prompt_text") or "")

    kept_rp = sum(1 for o in rps if (safe_idx(o, "prompt_index") or -1) < target)
    dropped_rp = sum(
        1 for o in rps
        if (idx := safe_idx(o, "prompt_index")) is not None and idx >= target
    )

    notes: List[str] = []
    cut_at: Optional[int] = None

    # 1) exact chat.prompt_index match
    for i, o in enumerate(chat):
        pi = safe_idx(o, "prompt_index", "promptIndex")
        if pi is not None and pi == target:
            cut_at = i
            text = extract_text(o)
            draft = draft or (user_query_body(text) or "")
            notes.append(f"exact chat.prompt_index=={target} at line {i}")
            break

    # 2) first chat row with prompt_index >= target
    if cut_at is None:
        for i, o in enumerate(chat):
            pi = safe_idx(o, "prompt_index", "promptIndex")
            if pi is not None and pi >= target:
                cut_at = i
                text = extract_text(o)
                draft = draft or (user_query_body(text) or "")
                notes.append(
                    f"first chat.prompt_index>={target} → pi={pi} line {i}"
                )
                break

    # 3) map entry has chat_line
    if cut_at is None and target in m and m[target].chat_line is not None:
        cut_at = m[target].chat_line
        draft = draft or m[target].draft
        notes.append(f"map chat_line for target → {cut_at}")

    # 4) next higher mapped chat_line
    if cut_at is None:
        for idx in sorted(m):
            if idx >= target and m[idx].chat_line is not None:
                cut_at = m[idx].chat_line
                draft = draft or m[idx].draft
                notes.append(
                    f"next mapped chat_line idx={idx} line={cut_at}"
                )
                break

    if cut_at is None:
        notes.append(
            "NO CHAT CUT: no prompt_index on chat rows and no map hit "
            "(compacted history without pi)"
        )

    # updates: STREAM cut at first line whose promptIndex >= target
    # (tool_call lines after that user turn lack promptIndex — must drop them too)
    kept_u = dropped_u = 0
    up = sdir / "updates.jsonl"
    if up.is_file():
        cut_line = None
        with up.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                pi = _update_line_prompt_index(line)
                if cut_line is None and pi is not None and pi >= target:
                    cut_line = i
                    dropped_u += 1
                elif cut_line is not None:
                    dropped_u += 1
                else:
                    kept_u += 1
        notes.append(
            f"updates STREAM-cut at line {cut_line}: "
            f"keep {kept_u} drop {dropped_u}"
        )

    kept = cut_at if cut_at is not None else len(chat)
    dropped = 0 if cut_at is None else len(chat) - cut_at
    return DryCutResult(
        strategy="prompt_index (proposed)",
        target=target,
        cut_at=cut_at,
        kept_chat_lines=kept,
        dropped_chat_lines=dropped,
        kept_rp=kept_rp,
        dropped_rp=dropped_rp,
        kept_updates=kept_u if up.is_file() else None,
        dropped_updates=dropped_u if up.is_file() else None,
        draft=str(draft or "")[:200],
        notes=notes,
    )


def _update_line_prompt_index(line: str) -> Optional[int]:
    """Extract promptIndex from an updates.jsonl line if present."""
    if "promptIndex" not in line and "prompt_index" not in line:
        return None
    try:
        o = json.loads(line)
    except json.JSONDecodeError:
        return None
    u = (o.get("params") or {}).get("update") or {}
    meta = u.get("_meta") or {}
    pi = safe_idx(meta, "promptIndex", "prompt_index")
    if pi is not None:
        return pi
    # some nested shapes
    return safe_idx(u, "promptIndex", "prompt_index")


def print_dry(r: DryCutResult) -> None:
    print(f"strategy: {r.strategy}")
    print(f"  target={r.target} cut_at={r.cut_at}")
    print(f"  chat keep={r.kept_chat_lines} drop={r.dropped_chat_lines}")
    print(f"  rp   keep={r.kept_rp} drop={r.dropped_rp}")
    if r.kept_updates is not None:
        print(f"  upd  keep={r.kept_updates} drop={r.dropped_updates}")
    print(f"  draft={r.draft!r}")
    for n in r.notes:
        print(f"  note: {n}")


# ── apply cut on a directory (sandbox) ───────────────────────────────────

def apply_cut(sdir: Path, target: int, *, truncate_updates: bool = True) -> dict:
    """Mutate sdir in place using proposed prompt_index strategy."""
    dry = dry_cut_by_prompt_index(sdir, target)
    stats = {
        "target": target,
        "cut_at": dry.cut_at,
        "draft": dry.draft,
        "before": {},
        "after": {},
    }

    def sizes() -> dict:
        out = {}
        for name in ("chat_history.jsonl", "rewind_points.jsonl", "updates.jsonl"):
            p = sdir / name
            if p.is_file():
                out[name] = {
                    "bytes": p.stat().st_size,
                    "lines": sum(1 for _ in p.open("rb")),
                }
        return out

    stats["before"] = sizes()

    # rp
    rp = sdir / "rewind_points.jsonl"
    if rp.is_file():
        kept = []
        for line in rp.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            idx = safe_idx(o, "prompt_index")
            if idx is None or idx < target:
                kept.append(line)
        rp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

    # chat
    ch = sdir / "chat_history.jsonl"
    if ch.is_file() and dry.cut_at is not None:
        lines = ch.read_text(encoding="utf-8", errors="replace").splitlines()
        kept = lines[: dry.cut_at]
        ch.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

    # updates — stream cut (see dry_cut_by_prompt_index)
    up = sdir / "updates.jsonl"
    if truncate_updates and up.is_file():
        kept_lines = []
        cutting = False
        with up.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                raw = line.rstrip("\n")
                if not cutting:
                    pi = _update_line_prompt_index(raw)
                    if pi is not None and pi >= target:
                        cutting = True
                        continue
                    kept_lines.append(raw)
                # else drop
        up.write_text(
            "\n".join(kept_lines) + ("\n" if kept_lines else ""),
            encoding="utf-8",
        )

    stats["after"] = sizes()
    stats["notes"] = dry.notes
    return stats


# ── ACP client ───────────────────────────────────────────────────────────

class AcpClient:
    """Minimal ACP client for `grok agent stdio` (matches bridge spawn)."""

    def __init__(self, cwd: str, timeout: float = 25.0):
        self.cwd = cwd
        self.timeout = timeout
        self.proc = None
        self._id = 0
        self._results: Dict[int, Any] = {}
        self._lock = threading.Lock()
        self._events: List[dict] = []
        self.session_id: Optional[str] = None
        self._stderr: List[str] = []

    def start(self) -> None:
        import subprocess
        # Bridge uses `grok agent stdio` (plain `grok acp` dies without a TTY).
        self.proc = subprocess.Popen(
            ["grok", "agent", "stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.cwd,
            bufsize=1,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read_err(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            self._stderr.append(line)
            if len(self._stderr) <= 5:
                print(f"[acp stderr] {line.rstrip()}", file=sys.stderr)

    def _reply(self, msg_id: Any, result: Any = None, error: Any = None) -> None:
        assert self.proc and self.proc.stdin
        m: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            m["error"] = error
        else:
            m["result"] = result if result is not None else {}
        with self._lock:
            self.proc.stdin.write(json.dumps(m) + "\n")
            self.proc.stdin.flush()

    def _handle_agent_request(self, msg: dict) -> None:
        """Answer agent→client requests so session/load does not stall/die."""
        mid = msg.get("id")
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        if mid is None:
            with self._lock:
                self._events.append(msg)
            return
        if method == "fs/read_text_file":
            path = params.get("path") or ""
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
                self._reply(mid, {"content": text[:200_000]})
            except OSError as e:
                self._reply(mid, error={"code": -32000, "message": str(e)})
        elif method == "session/request_permission":
            self._reply(mid, {
                "outcome": {"outcome": "selected", "optionId": "allow-once"},
            })
        else:
            # terminal/*, fs/write, etc. — empty ok for points/execute probe
            self._reply(mid, {})

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Response to our request (no method field)
            if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
                with self._lock:
                    self._results[msg["id"]] = msg
            elif msg.get("method"):
                self._handle_agent_request(msg)
            else:
                with self._lock:
                    self._events.append(msg)

    def request(self, method: str, params: dict, wait: float = None) -> Any:
        wait = self.timeout if wait is None else wait
        assert self.proc and self.proc.stdin
        self._id += 1
        rid = self._id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        if self.proc.poll() is not None:
            return {"error": {"message": f"agent dead before {method}",
                              "stderr": self._stderr[-5:]}}
        with self._lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        deadline = time.time() + wait
        while time.time() < deadline:
            with self._lock:
                if rid in self._results:
                    return self._results.pop(rid)
            if self.proc.poll() is not None:
                return {"error": {"message": f"agent died during {method}",
                                  "stderr": self._stderr[-8:]}}
            time.sleep(0.05)
        return {"error": {"message": f"timeout after {wait}s waiting for {method}"}}

    def close(self) -> None:
        if not self.proc:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    def bootstrap(self, session_id: Optional[str] = None) -> dict:
        info = {"initialize": None, "session": None, "methods_tried": []}
        info["initialize"] = self.request("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "grok-rewind-poc", "version": "0.2"},
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            },
        }, wait=15)
        if session_id:
            info["methods_tried"].append("session/load")
            r = self.request(
                "session/load",
                {"sessionId": session_id, "cwd": self.cwd, "mcpServers": []},
                wait=max(self.timeout, 90),
            )
            info["session"] = r
            if "result" in r and r.get("error") is None:
                res = r["result"] or {}
                self.session_id = (
                    res.get("sessionId")
                    or res.get("session_id")
                    or session_id
                )
                return info
        info["methods_tried"].append("session/new")
        r = self.request(
            "session/new",
            {"cwd": self.cwd, "mcpServers": []},
            wait=30,
        )
        info["session"] = r
        if "result" in r:
            res = r["result"] or {}
            self.session_id = res.get("sessionId") or res.get("session_id")
        return info


def try_rewind_points(client: AcpClient) -> dict:
    sid = client.session_id
    out = {"tried": [], "best": None}
    for method, params in (
        ("_x.ai/rewind/points", {"sessionId": sid}),
        ("x.ai/rewind/points", {"sessionId": sid}),
        ("_x.ai/rewind/points", {"session_id": sid}),
        ("x.ai/rewind/points", {"session_id": sid}),
    ):
        r = client.request(method, params, wait=12)
        out["tried"].append({"method": method, "response_keys": list(r.keys()),
                             "error": r.get("error"),
                             "result_preview": _preview(r.get("result"))})
        if "result" in r and r["result"] is not None:
            out["best"] = {"method": method, "result": r["result"]}
            break
    return out


def try_rewind_execute(
    client: AcpClient, target: int, mode: str = "conversation_only"
) -> dict:
    sid = client.session_id
    out = {"tried": [], "best": None}
    bodies = [
        {"sessionId": sid, "target_prompt_index": target, "mode": mode},
        {"sessionId": sid, "targetPromptIndex": target, "mode": mode},
        {"session_id": sid, "target_prompt_index": target, "mode": mode},
        {"sessionId": sid, "target_prompt_index": target, "mode": "conversation_only"},
    ]
    methods = ("_x.ai/rewind/execute", "x.ai/rewind/execute")
    for method in methods:
        for params in bodies:
            r = client.request(method, params, wait=15)
            entry = {
                "method": method,
                "params": params,
                "error": r.get("error"),
                "result_preview": _preview(r.get("result")),
            }
            out["tried"].append(entry)
            if "result" in r and r.get("error") is None:
                out["best"] = {"method": method, "params": params, "result": r["result"]}
                return out
    return out


def _preview(obj: Any, n: int = 600) -> Any:
    if obj is None:
        return None
    s = json.dumps(obj, default=str)
    if len(s) > n:
        return s[:n] + "…"
    try:
        return json.loads(s)
    except Exception:
        return s


# ── CLI commands ─────────────────────────────────────────────────────────

def cmd_list(_args: argparse.Namespace) -> None:
    dirs = find_session_dirs()
    print(f"{len(dirs)} sessions with rewind_points under {SESSIONS_ROOT}\n")
    import datetime
    for d in dirs[:40]:
        rp = d / "rewind_points.jsonl"
        ch = d / "chat_history.jsonl"
        try:
            rpn = sum(1 for _ in rp.open()) if rp.is_file() else 0
            chn = sum(1 for _ in ch.open()) if ch.is_file() else 0
            mtime = rp.stat().st_mtime
        except OSError:
            continue
        ts = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        print(f"{ts}  rp={rpn:4d}  ch={chn:4d}  {d}")


def cmd_analyze(args: argparse.Namespace) -> None:
    sdir = resolve_session(args.session)
    print_report(analyze(sdir))


def cmd_map(args: argparse.Namespace) -> None:
    sdir = resolve_session(args.session)
    m = build_map(sdir)
    humans = human_prompts(m)
    print(f"session: {sdir}")
    print(f"map entries: {len(m)}  human-like: {len(humans)}")
    print(f"  sources: {Counter(e.source for e in m.values())}")
    print(f"  in_rp: {sum(1 for e in m.values() if e.in_rewind_points)}  "
          f"with_chat_line: {sum(1 for e in m.values() if e.chat_line is not None)}  "
          f"with_snapshots: {sum(1 for e in m.values() if e.has_file_snapshots)}")
    last = args.last
    show = humans[-last:] if last else humans
    print(f"\nhuman-like prompts (showing {len(show)}):")
    for e in show:
        d = e.draft.replace("\n", " ")[:70]
        print(f"  pi={e.prompt_index:4d} chat#{str(e.chat_line):>4} "
              f"rp={int(e.in_rewind_points)} snap={int(e.has_file_snapshots)} "
              f"src={e.source:7} {d!r}")

    # gap analysis: consecutive human pi
    if len(humans) >= 2:
        gaps = [humans[i + 1].prompt_index - humans[i].prompt_index
                for i in range(len(humans) - 1)]
        print(f"\nhuman pi gaps: min={min(gaps)} max={max(gaps)} "
              f"avg={sum(gaps)/len(gaps):.1f}")


def cmd_dry_cut(args: argparse.Namespace) -> None:
    sdir = resolve_session(args.session)
    target = args.target
    if target is None:
        m = build_map(sdir)
        humans = human_prompts(m)
        if humans:
            target = humans[-1].prompt_index
            print(f"(default target = last human-like pi = {target})\n")
        else:
            rps = load_jsonl(sdir / "rewind_points.jsonl")
            idxs = [safe_idx(o, "prompt_index") for o in rps]
            idxs = [i for i in idxs if i is not None]
            if not idxs:
                raise SystemExit("no rewind points")
            target = max(idxs)
            print(f"(default target = max rp = {target})\n")

    print(f"session: {sdir}\n")
    if args.strategy in ("user_query", "both"):
        print_dry(dry_cut_user_query(sdir, target))
        print()
    if args.strategy in ("prompt_index", "both"):
        print_dry(dry_cut_by_prompt_index(sdir, target))
        print()


def cmd_compare(args: argparse.Namespace) -> None:
    sdir = resolve_session(args.session)
    rep = analyze(sdir)
    print_report(rep)
    m = build_map(sdir)
    humans = human_prompts(m)
    print("\n=== dry-cut matrix ===\n")
    targets: List[int] = []
    if rep.rp_max is not None:
        targets += [rep.rp_max, max(0, rep.rp_max - 1), 0]
    if humans:
        targets.append(humans[-1].prompt_index)
        if len(humans) > 1:
            targets.append(humans[-2].prompt_index)
        targets.append(humans[0].prompt_index)
    n_uq = len(rep.real_user_queries)
    if n_uq:
        targets.append(max(0, n_uq - 1))
    seen = set()
    for t in targets:
        if t in seen:
            continue
        seen.add(t)
        print(f"--- target={t} ---")
        print_dry(dry_cut_user_query(sdir, t))
        print()
        print_dry(dry_cut_by_prompt_index(sdir, t))
        print()


def cmd_sandbox_cut(args: argparse.Namespace) -> None:
    sdir = resolve_session(args.session)
    target = args.target
    if target is None:
        humans = human_prompts(build_map(sdir))
        if not humans:
            raise SystemExit("need --target (no human prompts found)")
        target = humans[-1].prompt_index
        print(f"(default target = last human pi = {target})")

    # copy only the files we touch + small metadata
    tmp = Path(tempfile.mkdtemp(prefix="grok_rewind_sandbox_"))
    print(f"sandbox: {tmp}")
    for name in (
        "chat_history.jsonl", "rewind_points.jsonl", "updates.jsonl",
        "summary.json", "signals.json",
    ):
        src = sdir / name
        if src.is_file():
            # updates can be huge — stream copy
            print(f"  copy {name} ({src.stat().st_size // 1024}KiB)…")
            shutil.copy2(src, tmp / name)

    print("\nbefore dry-run on original:")
    print_dry(dry_cut_by_prompt_index(sdir, target))

    print("\napplying cut on sandbox…")
    stats = apply_cut(tmp, target, truncate_updates=not args.skip_updates)
    print(json.dumps(stats, indent=2, default=str))

    print("\nre-analyze sandbox:")
    print_report(analyze(tmp))
    print(f"\n(sandbox left at {tmp} — delete manually if desired)")


def cmd_acp_points(args: argparse.Namespace) -> None:
    sdir = resolve_session(args.session) if args.session or not args.session_id else None
    sid = args.session_id or (sdir.name if sdir else None)
    cwd = args.cwd or (decode_cwd_from_session(sdir) if sdir else str(Path.cwd()))
    if not sid:
        sdir = resolve_session(None)
        sid = sdir.name
        cwd = decode_cwd_from_session(sdir)

    print(f"ACP points: session={sid} cwd={cwd}")
    client = AcpClient(cwd, timeout=args.timeout)
    try:
        client.start()
        boot = client.bootstrap(sid)
        print("bootstrap:", json.dumps({
            "session_id": client.session_id,
            "init_error": (boot.get("initialize") or {}).get("error"),
            "session_error": (boot.get("session") or {}).get("error"),
            "session_result_preview": _preview(
                (boot.get("session") or {}).get("result"), 400
            ),
        }, indent=2))
        pts = try_rewind_points(client)
        print("points:", json.dumps(pts, indent=2, default=str)[:5000])
        # if best has list, summarize
        best = (pts.get("best") or {}).get("result")
        if isinstance(best, dict):
            points = best.get("rewind_points") or best.get("points") or []
            print(f"\npoint count from ACP: {len(points)}")
            if points:
                sample = points[:3] + (points[-2:] if len(points) > 5 else [])
                print("sample:", json.dumps(sample, indent=2, default=str)[:2000])
                # compare disk
                if sdir:
                    disk_n = sum(1 for _ in (sdir / "rewind_points.jsonl").open())
                    print(f"disk rewind_points lines: {disk_n}")
    finally:
        client.close()


def cmd_acp_execute(args: argparse.Namespace) -> None:
    """Live ACP execute. Default: only against a sandbox *session id* is unsafe;
    we still call ACP on the real session id unless --sandbox-disk only.

    Safety: without --live-danger, we only dry-print what we would send and
    run execute against a freshly `session/new` empty session (expect fail).
    With --live-danger: execute on the real session (conversation_only).
    """
    sdir = resolve_session(args.session)
    target = args.target
    if target is None:
        humans = human_prompts(build_map(sdir))
        target = humans[-1].prompt_index if humans else 0
        print(f"(default target={target})")

    cwd = args.cwd or decode_cwd_from_session(sdir)
    print(f"ACP execute probe: sid={sdir.name} cwd={cwd} target={target}")
    print("disk dry-cut first:")
    print_dry(dry_cut_by_prompt_index(sdir, target))

    client = AcpClient(cwd, timeout=args.timeout)
    try:
        client.start()
        if args.live_danger:
            print("\n⚠ LIVE: loading real session and calling rewind/execute")
            boot = client.bootstrap(sdir.name)
        else:
            print("\nsafe mode: session/new only (execute expected to no-op/fail)")
            boot = client.bootstrap(None)
        print("boot session_id:", client.session_id)
        print("boot session err:", (boot.get("session") or {}).get("error"))

        pts = try_rewind_points(client)
        print("points best method:", (pts.get("best") or {}).get("method"))
        print("points preview:", _preview((pts.get("best") or {}).get("result"), 500))

        ex = try_rewind_execute(client, target, mode=args.mode)
        print("execute:", json.dumps(ex, indent=2, default=str)[:4000])
    finally:
        client.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Grok rewind PoC v2")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list"); p.set_defaults(func=cmd_list)

    p = sub.add_parser("analyze")
    p.add_argument("session", nargs="?")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("map", help="merged prompt_index map (chat+updates+rp)")
    p.add_argument("session", nargs="?")
    p.add_argument("--last", type=int, default=25)
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("dry-cut")
    p.add_argument("session", nargs="?")
    p.add_argument("--target", type=int, default=None)
    p.add_argument("--strategy", choices=("user_query", "prompt_index", "both"),
                   default="both")
    p.set_defaults(func=cmd_dry_cut)

    p = sub.add_parser("compare")
    p.add_argument("session", nargs="?")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("sandbox-cut", help="copy session → apply proposed cut")
    p.add_argument("session", nargs="?")
    p.add_argument("--target", type=int, default=None)
    p.add_argument("--skip-updates", action="store_true")
    p.set_defaults(func=cmd_sandbox_cut)

    p = sub.add_parser("acp-points")
    p.add_argument("session", nargs="?")
    p.add_argument("--session-id", default=None)
    p.add_argument("--cwd", default=None)
    p.add_argument("--timeout", type=float, default=25.0)
    p.set_defaults(func=cmd_acp_points)

    p = sub.add_parser("acp-execute")
    p.add_argument("session", nargs="?")
    p.add_argument("--target", type=int, default=None)
    p.add_argument("--mode", default="conversation_only")
    p.add_argument("--cwd", default=None)
    p.add_argument("--timeout", type=float, default=25.0)
    p.add_argument("--live-danger", action="store_true",
                   help="call execute on the real session (conversation_only)")
    p.set_defaults(func=cmd_acp_execute)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
