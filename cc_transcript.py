"""Tail a Claude Code session transcript (.jsonl) and turn appended records
into bridge-shaped notification params.

Used by PtyEngineSession: instead of receiving JSON-RPC notifications from the
SDK bridge, we run the real interactive `claude` in a hidden PTY and reconstruct
the same event stream by tailing the transcript it writes to disk.

The params dicts produced here are exactly what Session._on_notification(
"message", params) expects, so the existing renderer/dispatch is reused as-is.

Sublime-agnostic on purpose (no `import sublime`) so it can be unit-tested and
so the caller controls main-thread marshalling.
"""
import json
import os
import re
import threading
import time

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")


# ── path resolution ──────────────────────────────────────────────────────────
def project_slug(cwd):
    """Claude Code slug: every non-alphanumeric char in the real abs path -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))


def transcript_dir(cwd):
    return os.path.join(PROJECTS, project_slug(cwd))


def transcript_path(cwd, session_id):
    return os.path.join(transcript_dir(cwd), session_id + ".jsonl")


# ── record -> bridge-shaped params ────────────────────────────────────────────
def _format_ask_user(inp):
    """Format an AskUserQuestion tool_use input into human-readable text."""
    questions = inp.get("questions") or []
    if not questions:
        return ""
    out = ["\n\n*❓ AskUserQuestion:*"]
    for i, q in enumerate(questions):
        head = q.get("header") or ""
        out.append("  Q{}{}: {}".format(
            i + 1, " [" + head + "]" if head else "",
            (q.get("question") or "").strip()))
        for j, opt in enumerate(q.get("options") or []):
            out.append("       {}) {}".format(j + 1, opt.get("label", "")))
        if q.get("multiSelect"):
            out.append("       (multi-select)")
    out.append("")
    return "\n".join(out)


def _flatten_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
        return "\n".join(out)
    return ""


def record_to_params(record, id2name):
    """Yield bridge-shaped param dicts for one transcript record.

    id2name is a mutable dict (tool_use id -> name) maintained across records so
    tool_result lookups can be labeled if ever needed. We skip user *prompts*
    (the engine renders those itself in query()) and skip thinking/system/meta.
    """
    t = record.get("type")
    if record.get("isMeta") or record.get("isSidechain"):
        return

    if t == "assistant":
        msg = record.get("message") or {}
        for b in msg.get("content", []) or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and b.get("text", "").strip():
                yield {"type": "text", "text": b["text"]}
            elif bt == "tool_use":
                id2name[b.get("id")] = b.get("name")
                yield {"type": "tool_use", "name": b.get("name"),
                       "input": b.get("input", {}), "id": b.get("id")}
                # AskUserQuestion renders an interactive dialog INSIDE the TUI —
                # invisible in our hidden PTY and would silently hang the
                # session. Surface the question text so the user can at least
                # see what was asked and interrupt if needed. Fully native
                # answer flow is a Phase 5 item (needs TUI dialog driving).
                if b.get("name") == "AskUserQuestion":
                    txt = _format_ask_user(b.get("input") or {})
                    if txt:
                        yield {"type": "text", "text": txt}
        usage = msg.get("usage")
        if usage:
            yield {"type": "turn_usage", "usage": usage}

    elif t == "user":
        msg = record.get("message") or {}
        c = msg.get("content")
        if not isinstance(c, list):
            return  # plain string = user prompt; engine already rendered it
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                yield {"type": "tool_result",
                       "tool_use_id": b.get("tool_use_id"),
                       "content": _flatten_text(b.get("content")),
                       "is_error": bool(b.get("is_error"))}
            elif b.get("type") == "text" and \
                    "[Request interrupted by user" in (b.get("text") or ""):
                # User interrupted (often mid-tool); claude may not emit a
                # turn_duration here, so synthesize a turn-end so we finalize.
                yield {"type": "result", "duration_ms": 0, "total_cost_usd": 0}

    elif t == "system":
        st = record.get("subtype")
        if st == "turn_duration":
            # Authoritative turn-end. Synthesized as a bridge-style "result" so
            # the existing _on_msg_result fires output.meta() with the real
            # duration, AND the engine can finalize the turn deterministically.
            yield {"type": "result",
                   "duration_ms": record.get("durationMs", 0),
                   "total_cost_usd": 0}
        elif st == "compact_boundary":
            # Resets context_usage so the status bar isn't stuck on the
            # pre-compaction figure.
            yield {"type": "system", "subtype": "compact_boundary", "data": {}}
        elif st == "api_error":
            err = (record.get("error") or {}).get("message") or "API error"
            attempt = record.get("retryAttempt")
            max_r = record.get("maxRetries")
            retry = " (retry {}/{})".format(attempt, max_r) if attempt else ""
            yield {"type": "text", "text": "\n\n*⚠ API error{}: {}*\n".format(retry, err)}
        elif st == "stop_hook_summary":
            # Only surface when something actually went wrong — most hooks are
            # silent and successful.
            if record.get("hookErrors") or record.get("preventedContinuation"):
                errs = record.get("hookErrors") or []
                reason = record.get("stopReason") or "prevented continuation"
                yield {"type": "text",
                       "text": "\n\n*⚠ Hook: {} ({})*\n".format(reason, errs)}
        elif st == "local_command":
            c = (record.get("content") or "").strip()
            if c:
                yield {"type": "text", "text": "\n\n*[command] {}*\n".format(c)}


# ── tailer thread ──────────────────────────────────────────────────────────────
class Tailer(threading.Thread):
    """Daemon thread: tail an append-only jsonl, fire on_event(params) per event.

    on_event is called from this thread — the caller is responsible for
    marshalling to the Sublime main thread.
    """

    def __init__(self, path, on_event, poll=0.15):
        super().__init__(daemon=True)
        self.path = path
        self.on_event = on_event
        self.poll = poll
        self._stop = threading.Event()
        self._id2name = {}

    def stop(self):
        self._stop.set()

    def run(self):
        # wait for the file to appear (claude creates it on first turn)
        while not self._stop.is_set() and not os.path.exists(self.path):
            time.sleep(self.poll)
        if self._stop.is_set():
            return
        buf = ""
        try:
            f = open(self.path, "r")
        except OSError:
            return
        with f:
            while not self._stop.is_set():
                chunk = f.read()
                if not chunk:
                    time.sleep(self.poll)
                    continue
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    try:
                        for params in record_to_params(record, self._id2name):
                            self.on_event(params)
                    except Exception as e:
                        print("[cc_transcript] record error: {}".format(e))
