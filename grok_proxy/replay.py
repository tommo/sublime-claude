"""Reasoning-replay cache for xAI/Grok.

Grok's reasoning `encrypted_content` IS replayable across turns, but only when
sent back as the COMPLETE reasoning item ({id, summary, type, status,
encrypted_content}) — exactly `*response.output`. The Claude Code transcript
only carries the thinking `signature` (= encrypted_content), NOT the item id,
so naive forwarding fails ("Could not decode the compaction blob").

This module is the Python proxy's analogue of CLIProxyAPI's
internal/runtime/executor/xai_reasoning_replay.go + codex_executor.go's
insertCodexReasoningReplayItems: an in-memory, per-session cache of the full
reasoning items from the latest response, re-injected into the next request.

  session key   helps.ExtractClaudeCodeSessionID  (X-Claude-Code-Session-Id
                 header, or metadata.user_id's `_session_<uuid>` suffix / JSON
                 session_id)
  cache write   cacheXAIReasoningReplayFromCompleted  (reasoning items from
                 response.output — REPLACES per session, latest only)
  inject        insertCodexReasoningReplayItems  (before the last assistant
                 message; reasoning-only — function calls round-trip via the
                 transcript as tool_use/tool_result)
"""
import json
import re
import threading
from typing import Dict, List, Optional

_CC_SESSION_SUFFIX = re.compile(r"_session_([a-f0-9-]+)$")
_MAX_ENTRIES = 512


class ReplayCache:
    """Thread-safe, in-process, per-session cache of the latest reasoning items."""

    def __init__(self, max_entries=_MAX_ENTRIES):
        self._lock = threading.Lock()
        self._entries = {}  # session_key -> [reasoning_item_dict, ...]
        self._max = max_entries

    def get(self, session_key):
        if not session_key:
            return []
        with self._lock:
            return list(self._entries.get(session_key, []))

    def set(self, session_key, items):
        if not session_key:
            return
        with self._lock:
            self._entries[session_key] = list(items)
            if len(self._entries) > self._max:
                # evict oldest by insertion order (dict preserves it)
                for k in list(self._entries.keys())[: len(self._entries) - self._max]:
                    self._entries.pop(k, None)

    def clear(self, session_key=None):
        with self._lock:
            if session_key is None:
                self._entries.clear()
            else:
                self._entries.pop(session_key, None)


# process-wide singleton (one proxy process serves all sessions)
cache = ReplayCache()


def session_key_from_request(req, headers=None):
    """Derive a stable per-conversation key (helps.ExtractClaudeCodeSessionID).

    Returns "" if no session id can be found (replay disabled for that request).
    """
    sid = ""
    if headers:
        sid = (headers.get("X-Claude-Code-Session-Id") or "").strip()
    if not sid and isinstance(req, dict):
        meta = req.get("metadata") or {}
        uid = meta.get("user_id") if isinstance(meta, dict) else ""
        if isinstance(uid, str) and uid:
            m = _CC_SESSION_SUFFIX.search(uid)
            if m:
                sid = m.group(1)
            elif uid[:1] == "{":
                try:
                    s = json.loads(uid).get("session_id")
                    if isinstance(s, str):
                        sid = s.strip()
                except ValueError:
                    pass
    return ("claude:" + sid) if sid else ""


def extract_reasoning_items(completed_data):
    """Pull the full reasoning items out of a response.completed data dict."""
    if not isinstance(completed_data, dict):
        return []
    resp = completed_data.get("response") or {}
    out = resp.get("output") or []
    items = []
    for it in out:
        if (isinstance(it, dict) and it.get("type") == "reasoning"
                and it.get("encrypted_content")):
            items.append(it)
    return items


def _insert_index(input_items):
    """Where to splice replayed reasoning items (codexReasoningReplayInsertIndex,
    reasoning-only simplification: before the last assistant message; else before
    the first non-developer/system item; else at the end)."""
    for i in range(len(input_items) - 1, -1, -1):
        it = input_items[i]
        if isinstance(it, dict) and it.get("type") == "message" \
                and it.get("role") == "assistant":
            return i
    for i, it in enumerate(input_items):
        if not isinstance(it, dict):
            return i
        if it.get("type") != "message":
            return i
        if it.get("role") not in ("developer", "system"):
            return i
    return len(input_items)


def inject_reasoning_replay(body, items):
    """Splice cached reasoning items into a translated xAI request body."""
    if not items:
        return body
    inp = body.get("input")
    if not isinstance(inp, list) or not inp:
        return body
    idx = _insert_index(inp)
    body["input"] = inp[:idx] + list(items) + inp[idx:]
    return body
