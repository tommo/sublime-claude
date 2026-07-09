"""xAI Responses SSE -> Anthropic Messages SSE translation.

Ported from CLIProxyAPI:
  internal/translator/codex/claude/codex_claude_response.go        (state machine)
  internal/translator/codex/claude/codex_claude_response_web_search.go (web_search blocks)
  internal/translator/common/bytes.go                              (SSE framing)
  internal/util/claude_tool_id.go                                  (SanitizeClaudeToolID)
  internal/runtime/executor/xai_executor.go:1367-1587             (reasoning-event
    normalization + output_item.done collection / response.completed patching)

The StreamTranslator processes one normalized `data:` JSON dict per call and
returns a list of Anthropic SSE byte frames (`event: <name>\\ndata: <json>\\n\\n`).
StreamingPipeline wraps the translator with the executor's reasoning-event
normalization and output-item patching so callers just feed raw data lines.
"""
import json
import re
import threading
from typing import Any, Dict, List, Optional

from . import translate_request as tr

_TOOL_ID_SANITIZER = re.compile(r"[^a-zA-Z0-9_-]")
_tool_id_counter = 0
_tool_id_lock = threading.Lock()


def sanitize_tool_id(id_):
    """Conform a tool_use id to ^[a-zA-Z0-9_-]+$; generate a fallback if empty."""
    global _tool_id_counter
    s = _TOOL_ID_SANITIZER.sub("_", id_ or "")
    if s == "":
        with _tool_id_lock:
            _tool_id_counter += 1
            c = _tool_id_counter
        # no time.time in module scope reliably across py3.8; use counter only
        s = "toolu_%d" % c
    return s


# --- dotted-path access mirroring gjson -------------------------------------

def _get(obj, path, default=""):
    """Navigate a dotted path through dicts; return `default` (gjson .String()-like)."""
    if obj is None:
        return default
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return default
        if cur is None:
            return default
    return cur


def _get_int(obj, path, default=0):
    v = _get(obj, path, None)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        if isinstance(v, bool):
            return default
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


# --- SSE framing (translatorcommon.AppendSSEEventBytes) ---------------------

def sse_event(event, payload):
    """Build one SSE frame: `event: <e>\\ndata: <json>\\n\\n`."""
    body = json.dumps(payload, separators=(",", ":"))
    return ("event: " + event + "\ndata: " + body + "\n\n").encode("utf-8")


# --- reasoning-event normalization (xai_executor.go:1389-1532) --------------

def _normalize_summary_index(d):
    """Move content_index -> summary_index, delete content_index."""
    if "content_index" in d and d["content_index"] is not None and "summary_index" not in d:
        d["summary_index"] = d["content_index"]
    d.pop("content_index", None)
    return d


def _normalize_reasoning_item(item):
    """Normalize a `reasoning` item's content (reasoning_text -> summary in summary[])."""
    if not isinstance(item, dict) or item.get("type") != "reasoning":
        return item
    out = dict(item)
    summary = out.get("summary")
    if isinstance(summary, list):
        out["summary"] = [_set_type_summary_text(p) for p in summary]
    content = out.get("content")
    if isinstance(content, list):
        summary_items = [p for p in content if isinstance(p, dict) and p.get("type") == "reasoning_text"]
        if summary_items:
            out["summary"] = [_set_type_summary_text(p) for p in summary_items]
            out.pop("content", None)
    return out


def _set_type_summary_text(part):
    if isinstance(part, dict) and part.get("type") == "reasoning_text":
        p = dict(part)
        p["type"] = "summary_text"
        return p
    return part


def normalize_reasoning_data_events(data):
    """Apply xai executor reasoning normalization. Returns a list of data dicts.

    Most events normalize to a single dict; `response.reasoning_text.done` splits
    into two (a no-op `reasoning_summary_text.done` + the real `part.done`).
    """
    if not isinstance(data, dict):
        return [data]
    t = data.get("type")
    out = dict(data)

    if t == "response.reasoning_text.delta":
        out["type"] = "response.reasoning_summary_text.delta"
        out = _normalize_summary_index(out)
    elif t == "response.reasoning_text.done":
        # event A: reasoning_summary_text.done (translator has no case -> no-op)
        text_done = dict(data)
        text_done["type"] = "response.reasoning_summary_text.done"
        text_done = _normalize_summary_index(text_done)
        # event B: reasoning_summary_part.done with part shuffle
        out["type"] = "response.reasoning_summary_part.done"
        out["part"] = dict(out.get("part") or {})
        out["part"]["type"] = "summary_text"
        if "text" in out:
            out["part"]["text"] = out["text"]
        out.pop("text", None)
        out = _normalize_summary_index(out)
        return [text_done, _finalize_nested(out)]
    elif t == "response.content_part.added" and _get(data, "part.type") == "reasoning_text":
        out["type"] = "response.reasoning_summary_part.added"
        out["part"] = dict(out.get("part") or {})
        out["part"]["type"] = "summary_text"
        out = _normalize_summary_index(out)
    elif t == "response.content_part.done" and _get(data, "part.type") == "reasoning_text":
        out["type"] = "response.reasoning_summary_part.done"
        out["part"] = dict(out.get("part") or {})
        out["part"]["type"] = "summary_text"
        out = _normalize_summary_index(out)

    return [_finalize_nested(out)]


def _finalize_nested(d):
    """Normalize nested `item` and `response.output` reasoning items in place."""
    if not isinstance(d, dict):
        return d
    item = d.get("item")
    if isinstance(item, dict):
        d["item"] = _normalize_reasoning_item(item)
    resp_out = _get(d, "response.output")
    if isinstance(resp_out, list):
        d.setdefault("response", {})
        d["response"]["output"] = [_normalize_reasoning_item(it) for it in resp_out]
    return d


# --- stop reason / usage ----------------------------------------------------

def _codex_stop_sequence(response_data):
    seq = _get(response_data, "stop_sequence")
    return seq if seq != "" else None


def codex_stop_reason(response_data):
    sr = _get(response_data, "stop_reason")
    if sr != "":
        if sr == "stop" and _codex_stop_sequence(response_data):
            return "stop_sequence"
        return sr
    reason = _get(response_data, "incomplete_details.reason")
    if reason != "":
        return reason
    if _codex_stop_sequence(response_data):
        return "stop_sequence"
    return ""


def map_stop_reason(stop_reason, has_tool_call):
    if has_tool_call:
        return "tool_use"
    if stop_reason in ("", "stop", "completed"):
        return "end_turn"
    if stop_reason in ("max_tokens", "max_output_tokens"):
        return "max_tokens"
    if stop_reason in ("tool_use", "tool_calls", "function_call"):
        return "end_turn"
    if stop_reason in ("end_turn", "stop_sequence", "pause_turn", "refusal",
                       "model_context_window_exceeded"):
        return stop_reason
    if stop_reason == "content_filter":
        return "refusal"
    return "end_turn"


def extract_usage(usage):
    if not isinstance(usage, dict):
        return 0, 0, 0
    inp = _get_int(usage, "input_tokens")
    outp = _get_int(usage, "output_tokens")
    cached = _get_int(usage, "input_tokens_details.cached_tokens")
    if cached > 0:
        inp = max(0, inp - cached)
    return inp, outp, cached


# --- the streaming state machine -------------------------------------------

class StreamTranslator:
    """Translates normalized xAI Responses SSE data dicts into Anthropic SSE frames.

    Stateless across requests; holds streaming state within one response. Not
    thread-safe — one instance per /v1/messages request.
    """

    def __init__(self, short_to_original=None):
        self.short_to_original = short_to_original or {}
        self.block_index = 0
        self.has_emitted_tool_use = False
        self.has_received_args_delta = False
        self.function_call_open = False
        self.function_call_call_id = ""
        self.function_call_index = 0
        self.has_text_delta = False
        self.text_block_open = False
        self.thinking_open = False
        self.thinking_stop_pending = False
        self.thinking_signature = ""
        self.thinking_summary_seen = False
        self.web_search_use_ids = set()
        self.web_search_result_ids = set()
        self.last_web_search_use_id = ""
        self.pending = {}            # key -> dict(CallID, Arguments, HasArgs, StartEmitted)
        self.last_pending_key = ""

    # -- name resolution ----------------------------------------------------
    def _resolve_name(self, name):
        return self.short_to_original.get(name, name)

    def _call_id_key(self, call_id):
        return ("call:" + call_id) if call_id else ""

    def _func_call_key(self, root, item):
        oi = root.get("output_index")
        if oi is not None:
            return "output:" + json.dumps(oi, separators=(",", ":"))
        cid = item.get("call_id", "") if isinstance(item, dict) else ""
        if cid:
            return "call:" + cid
        return "last"

    # -- block helpers ------------------------------------------------------
    def _start_text_block(self):
        if self.text_block_open:
            return b""
        self.text_block_open = True
        return sse_event("content_block_start", {
            "type": "content_block_start", "index": self.block_index,
            "content_block": {"type": "text", "text": ""},
        })

    def _stop_text_block(self):
        if not self.text_block_open:
            return b""
        self.text_block_open = False
        idx = self.block_index
        self.block_index += 1
        return sse_event("content_block_stop", {
            "type": "content_block_stop", "index": idx,
        })

    def _start_thinking_block(self):
        if self.thinking_open:
            return b""
        self.thinking_open = True
        self.thinking_stop_pending = False
        return sse_event("content_block_start", {
            "type": "content_block_start", "index": self.block_index,
            "content_block": {"type": "thinking", "thinking": ""},
        })

    def _finalize_thinking_block(self):
        if not self.thinking_open:
            return b""
        frames = b""
        if self.thinking_signature:
            frames += sse_event("content_block_delta", {
                "type": "content_block_delta", "index": self.block_index,
                "delta": {"type": "signature_delta", "signature": self.thinking_signature},
            })
        idx = self.block_index
        self.block_index += 1
        self.thinking_open = False
        self.thinking_stop_pending = False
        frames += sse_event("content_block_stop", {
            "type": "content_block_stop", "index": idx,
        })
        return frames

    def _finalize_signature_only_thinking(self):
        if self.thinking_signature == "":
            return b""
        return self._start_thinking_block() + self._finalize_thinking_block()

    # -- function call helpers ---------------------------------------------
    def _func_call_start(self, call_id, name, block_index):
        self.has_emitted_tool_use = True
        return sse_event("content_block_start", {
            "type": "content_block_start", "index": block_index,
            "content_block": {
                "type": "tool_use",
                "id": tr.shorten_call_id(sanitize_tool_id(call_id)),
                "name": self._resolve_name(name),
                "input": {},
            },
        })

    def _func_call_arg_delta(self, partial_json, block_index):
        return sse_event("content_block_delta", {
            "type": "content_block_delta", "index": block_index,
            "delta": {"type": "input_json_delta", "partial_json": partial_json},
        })

    def _func_call_stop(self, block_index):
        return sse_event("content_block_stop", {
            "type": "content_block_stop", "index": block_index,
        })

    def _open_func_call_stop(self):
        if not self.function_call_open:
            return b""
        idx = self.function_call_index
        frames = self._func_call_stop(idx)
        if self.block_index <= idx:
            self.block_index = idx + 1
        self.function_call_open = False
        self.function_call_call_id = ""
        self.function_call_index = 0
        return frames

    # -- pending function call bookkeeping ---------------------------------
    def _record_pending(self, root, item):
        call_id = item.get("call_id", "")
        p = {"CallID": call_id, "Arguments": "", "HasArgs": False, "StartEmitted": False}
        key = self._func_call_key(root, item)
        self.pending[key] = p
        ck = self._call_id_key(call_id)
        if ck:
            self.pending[ck] = p
        self.last_pending_key = key

    def _keys_for_pending(self, pending):
        return [k for k, v in self.pending.items() if v is pending]

    def _delete_pending(self, keys):
        for k in keys:
            self.pending.pop(k, None)
            if self.last_pending_key == k:
                self.last_pending_key = ""

    def _pending_for_done(self, root, item):
        keys = [self._func_call_key(root, item)]
        call_id = item.get("call_id", "")
        if call_id:
            ck = self._call_id_key(call_id)
            if ck and ck not in keys:
                keys.append(ck)
        elif root.get("output_index") is None and self.last_pending_key:
            if self.last_pending_key not in keys:
                keys.append(self.last_pending_key)
        for k in keys:
            p = self.pending.get(k)
            if p is not None:
                return p, self._keys_for_pending(p)
        return None, []

    def _pending_for_key(self, key):
        if not key:
            return None
        return self.pending.get(key)

    def _pending_for_terminal(self, output_index, item):
        keys = []
        call_id = item.get("call_id", "")
        if call_id:
            ck = self._call_id_key(call_id)
            if ck:
                keys.append(ck)
        ioi = item.get("output_index")
        if ioi is not None:
            keys.append("output:" + json.dumps(ioi, separators=(",", ":")))
        if output_index is not None:
            k2 = "output:" + json.dumps(output_index, separators=(",", ":"))
            if k2 not in keys:
                keys.append(k2)
        for k in keys:
            p = self.pending.get(k)
            if p is not None:
                return p, self._keys_for_pending(p)
        return None, []

    # -- main dispatch ------------------------------------------------------
    def feed(self, root):
        """Process one normalized data dict; return bytes of SSE frames."""
        if not isinstance(root, dict):
            return b""
        frames = b""
        t = root.get("type", "")

        # finalize pending thinking on boundary events
        if self.thinking_open and self.thinking_stop_pending:
            if t in ("response.content_part.added", "response.completed", "response.incomplete"):
                frames += self._finalize_thinking_block()

        if t == "error":
            frames += self._stream_error(root)
        elif t == "response.created":
            msg = {
                "id": _get(root, "response.id"), "type": "message", "role": "assistant",
                "model": _get(root, "response.model"), "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "content": [], "stop_reason": None,
            }
            frames += sse_event("message_start", {"type": "message_start", "message": msg})
        elif t == "response.reasoning_summary_part.added":
            if self.thinking_open and self.thinking_stop_pending:
                frames += self._finalize_thinking_block()
            self.thinking_summary_seen = True
            frames += self._start_thinking_block()
        elif t == "response.reasoning_summary_text.delta":
            frames += sse_event("content_block_delta", {
                "type": "content_block_delta", "index": self.block_index,
                "delta": {"type": "thinking_delta", "thinking": _get(root, "delta")},
            })
        elif t == "response.reasoning_summary_part.done":
            self.thinking_stop_pending = True
        elif t == "response.content_part.added":
            if _get(root, "part.type") == "output_text":
                frames += self._start_text_block()
        elif t == "response.output_text.delta":
            self.has_text_delta = True
            frames += self._finalize_thinking_block()
            frames += self._start_text_block()
            frames += sse_event("content_block_delta", {
                "type": "content_block_delta", "index": self.block_index,
                "delta": {"type": "text_delta", "text": _get(root, "delta")},
            })
        elif t == "response.content_part.done":
            if _get(root, "part.type") == "output_text":
                frames += self._stop_text_block()
        elif t in ("response.completed", "response.incomplete"):
            frames = self._terminal(root, frames)
        elif t == "response.output_item.added":
            frames += self._output_item_added(root)
        elif t == "response.output_item.done":
            frames += self._output_item_done(root)
        elif t == "response.function_call_arguments.delta":
            frames += self._args_delta(root)
        elif t == "response.function_call_arguments.done":
            frames += self._args_done(root)
        # response.reasoning_summary_text.done, web_search_call.* -> no-op here

        return frames

    # -- sub-dispatches -----------------------------------------------------
    def _stream_error(self, root):
        err = _get_obj(root, "error") or {}
        err_type = (err.get("type") or "").strip()
        if not err_type:
            err_type = (_get(root, "error_type") or "").strip()
        if not err_type:
            err_type = "api_error"
        code = (err.get("code") or "").strip()
        message = (err.get("message") or "").strip()
        if not message:
            message = (_get(root, "message") or "").strip()
        if not message:
            message = code
        if not message:
            message = err_type
        if code == "cyber_policy" or err_type == "invalid_request":
            err_type = "invalid_request_error"
        return sse_event("error", {"type": "error", "error": {"type": err_type, "message": message}})

    def _terminal(self, root, frames):
        resp = _get_obj(root, "response") or {}
        # hydrate open function call from terminal output
        if self.function_call_open and not self.has_received_args_delta:
            for item in (resp.get("output") or []):
                if item.get("type") == "function_call" and item.get("call_id") == self.function_call_call_id:
                    args = item.get("arguments", "")
                    if args:
                        frames += self._func_call_arg_delta(args, self.function_call_index)
                        self.has_received_args_delta = True
                    break
        # finalize open content blocks
        frames += self._finalize_thinking_block()
        frames += self._stop_text_block()
        frames += self._open_func_call_stop()
        # append pending function calls discovered only at terminal
        for idx, item in enumerate(resp.get("output") or []):
            if item.get("type") != "function_call":
                continue
            p, pkeys = self._pending_for_terminal(idx, item)
            if p is None:
                continue
            if p["StartEmitted"]:
                self._delete_pending(pkeys)
                continue
            name = item.get("name", "")
            if not name:
                self._delete_pending(pkeys)
                continue
            call_id = p["CallID"] or item.get("call_id", "")
            bi = self.block_index
            frames += self._func_call_start(call_id, name, bi)
            p["StartEmitted"] = True
            args = item.get("arguments", "") or p["Arguments"]
            if args:
                frames += self._func_call_arg_delta(args, bi)
            frames += self._func_call_stop(bi)
            self.block_index += 1
            self._delete_pending(pkeys)
        self.pending.clear()
        self.last_pending_key = ""

        stop_reason = map_stop_reason(codex_stop_reason(resp), self.has_emitted_tool_use)
        inp, outp, cached = extract_usage(resp.get("usage"))
        delta = {"stop_reason": stop_reason, "stop_sequence": None}
        seq = _codex_stop_sequence(resp)
        if seq:
            delta["stop_sequence"] = seq
        usage = {"input_tokens": inp, "output_tokens": outp}
        if cached > 0:
            usage["cache_read_input_tokens"] = cached
        frames += sse_event("message_delta", {
            "type": "message_delta", "delta": delta, "usage": usage,
        })
        frames += sse_event("message_stop", {"type": "message_stop"})
        return frames

    def _output_item_added(self, root):
        frames = b""
        item = _get_obj(root, "item") or {}
        itype = item.get("type", "")
        if itype == "function_call":
            frames += self._finalize_thinking_block()
            frames += self._stop_text_block()
            self.has_received_args_delta = False
            call_id = item.get("call_id", "")
            name = item.get("name", "")
            if not name:
                self._record_pending(root, item)
                return frames
            p, pkeys = self._pending_for_done(root, item)
            if p is not None:
                self._delete_pending(pkeys)
            bi = self.block_index
            frames += self._func_call_start(call_id, name, bi)
            frames += self._func_call_arg_delta("", bi)
            self.function_call_open = True
            self.function_call_call_id = call_id
            self.function_call_index = bi
        elif itype == "reasoning":
            self.thinking_summary_seen = False
            self.thinking_signature = item.get("encrypted_content", "")
        # web_search_call: defer to output_item.done
        return frames

    def _output_item_done(self, root):
        frames = b""
        item = _get_obj(root, "item") or {}
        itype = item.get("type", "")
        if itype == "message":
            if self.has_text_delta:
                return frames
            content = item.get("content")
            if not isinstance(content, list):
                return frames
            text = "".join(p.get("text", "") for p in content
                           if isinstance(p, dict) and p.get("type") == "output_text")
            if text == "":
                return frames
            frames += self._finalize_thinking_block()
            frames += self._start_text_block()
            frames += sse_event("content_block_delta", {
                "type": "content_block_delta", "index": self.block_index,
                "delta": {"type": "text_delta", "text": text},
            })
            frames += self._stop_text_block()
            self.has_text_delta = True
        elif itype == "function_call":
            p, pkeys = self._pending_for_done(root, item)
            if p is not None and not p["StartEmitted"]:
                name = item.get("name", "")
                if not name:
                    return frames
                call_id = p["CallID"] or item.get("call_id", "")
                bi = self.block_index
                frames += self._func_call_start(call_id, name, bi)
                p["StartEmitted"] = True
                args = p["Arguments"] or item.get("arguments", "")
                if args:
                    frames += self._func_call_arg_delta(args, bi)
                frames += self._func_call_stop(bi)
                self.block_index += 1
                self._delete_pending(pkeys)
            elif self.function_call_open:
                if not self.has_received_args_delta:
                    args = item.get("arguments", "")
                    if args:
                        frames += self._func_call_arg_delta(args, self.function_call_index)
                        self.has_received_args_delta = True
                frames += self._open_func_call_stop()
        elif itype == "reasoning":
            sig = item.get("encrypted_content", "")
            if sig:
                self.thinking_signature = sig
            if self.thinking_summary_seen:
                frames += self._finalize_thinking_block()
            else:
                frames += self._finalize_signature_only_thinking()
            self.thinking_signature = ""
            self.thinking_summary_seen = False
        elif itype == "web_search_call":
            frames += self._web_search_result(root, item)
        return frames

    def _args_delta(self, root):
        delta = _get(root, "delta")
        key = self._args_key(root)
        p = self._pending_for_key(key)
        if p is not None and not p["StartEmitted"]:
            p["HasArgs"] = True
            p["Arguments"] += delta
            return b""
        self.has_received_args_delta = True
        return self._func_call_arg_delta(delta, self.block_index)

    def _args_done(self, root):
        key = self._args_key(root)
        p = self._pending_for_key(key)
        if p is not None and not p["StartEmitted"]:
            if not p["HasArgs"]:
                p["Arguments"] = _get(root, "arguments")
            return b""
        if not self.has_received_args_delta:
            args = _get(root, "arguments")
            if args:
                frames = self._func_call_arg_delta(args, self.block_index)
                self.has_received_args_delta = True
                return frames
        return b""

    def _args_key(self, root):
        oi = root.get("output_index")
        if oi is not None:
            return "output:" + json.dumps(oi, separators=(",", ":"))
        return self.last_pending_key

    # -- web search blocks --------------------------------------------------
    def _web_search_use_id(self, root, item):
        for path in ("id", "output_item_id", "call_id"):
            v = (item.get(path) if isinstance(item, dict) else "") or _get(root, path)
            if v:
                return v
        if self.last_web_search_use_id:
            return self.last_web_search_use_id
        for path in ("item_id",):
            v = (item.get(path) if isinstance(item, dict) else "") or _get(root, path)
            if v:
                return v
        wid = "web_search_%d" % self.block_index
        self.last_web_search_use_id = wid
        return wid

    def _web_search_query(self, root, item):
        for path in ("action.query", "query", "input.query"):
            v = _get(item, path) or _get(root, path)
            if v:
                return v
        return ""

    def _web_search_result_content(self, root, item):
        results = item.get("results")
        if not isinstance(results, list):
            results = root.get("results")
        if not isinstance(results, list):
            return []
        out = []
        for r in results:
            if not isinstance(r, dict):
                continue
            url = (r.get("url") or "").strip()
            if not url:
                continue
            title = (r.get("title") or "").strip() or url
            out.append({"type": "web_search_result", "title": title, "url": url, "page_age": None})
        return out

    def _web_search_server_tool_use(self, root, item):
        wid = self._web_search_use_id(root, item)
        if not wid:
            return b""
        query = self._web_search_query(root, item)
        already = wid in self.web_search_use_ids
        frames = b""
        if not already:
            frames += self._finalize_thinking_block()
            frames += sse_event("content_block_start", {
                "type": "content_block_start", "index": self.block_index,
                "content_block": {"type": "server_tool_use", "id": wid, "name": "web_search", "input": {}},
            })
        if query:
            frames += sse_event("content_block_delta", {
                "type": "content_block_delta", "index": self.block_index,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps({"query": query})},
            })
        if not already:
            frames += sse_event("content_block_stop", {
                "type": "content_block_stop", "index": self.block_index,
            })
            self.web_search_use_ids.add(wid)
            self.block_index += 1
        return frames

    def _web_search_result(self, root, item):
        wid = self._web_search_use_id(root, item)
        if not wid:
            return b""
        frames = self._web_search_server_tool_use(root, item)
        if wid in self.web_search_result_ids:
            return frames
        query = self._web_search_query(root, item)
        results = self._web_search_result_content(root, item)
        if not query and not results and "action" not in item:
            return frames
        frames += sse_event("content_block_start", {
            "type": "content_block_start", "index": self.block_index,
            "content_block": {"type": "web_search_tool_result", "tool_use_id": wid, "content": results},
        })
        frames += sse_event("content_block_stop", {
            "type": "content_block_stop", "index": self.block_index,
        })
        self.web_search_result_ids.add(wid)
        self.block_index += 1
        if wid == self.last_web_search_use_id:
            self.last_web_search_use_id = ""
        return frames


def _get_obj(d, path):
    """Like _get but returns the raw object (dict/list) at path, or None if absent."""
    cur = d
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


# --- streaming pipeline (wraps translator + executor normalization) --------

class StreamingPipeline:
    """Feed raw `data:` JSON strings; yields Anthropic SSE byte frames.

    Applies the executor's reasoning-event normalization, collects
    output_item.done items, and patches an empty response.completed.output.
    """

    def __init__(self, short_to_original=None):
        self.tr = StreamTranslator(short_to_original)
        self._by_index = {}
        self._fallback = []
        self.last_completed = None  # patched terminal data dict (for replay caching)

    def feed_data_line(self, data_str):
        data_str = data_str.strip()
        if not data_str:
            return b""
        try:
            data = json.loads(data_str)
        except ValueError:
            return b""
        frames = b""
        for nd in normalize_reasoning_data_events(data):
            # collect output_item.done by index (xaiCollectOutputItemDone)
            if isinstance(nd, dict) and nd.get("type") == "response.output_item.done":
                item = nd.get("item")
                if isinstance(item, dict):
                    oi = nd.get("output_index")
                    if oi is not None:
                        self._by_index[oi] = item
                    else:
                        self._fallback.append(item)
            # patch empty response.completed output (xaiPatchCompletedOutput)
            if isinstance(nd, dict) and nd.get("type") in ("response.completed", "response.incomplete"):
                self._patch_completed(nd)
                self.last_completed = nd
            frames += self.tr.feed(nd)
        return frames

    def _patch_completed(self, nd):
        resp = nd.get("response")
        if not isinstance(resp, dict):
            return
        out = resp.get("output")
        if isinstance(out, list) and len(out) > 0:
            return
        if not self._by_index and not self._fallback:
            return
        items = [self._by_index[k] for k in sorted(self._by_index.keys())]
        items += list(self._fallback)
        resp["output"] = items
        nd["response"] = resp


def collect_nonstream_terminal(data_line_iterable):
    """Consume upstream `data:` payloads; return the (patched) terminal
    response.completed/response.incomplete dict, or None if none arrived.

    Mirrors StreamingPipeline: normalizes reasoning events, collects
    output_item.done items by output_index, and patches a terminal event whose
    response.output is missing/empty (xAI frequently sends output:[] at
    completion, with the real items only on the output_item.done events).
    """
    by_index = {}
    fallback = []
    terminal = None
    for data_str in data_line_iterable:
        data_str = (data_str or "").strip()
        if not data_str:
            continue
        try:
            data = json.loads(data_str)
        except ValueError:
            continue
        for nd in normalize_reasoning_data_events(data):
            if not isinstance(nd, dict):
                continue
            if nd.get("type") == "response.output_item.done":
                item = nd.get("item")
                if isinstance(item, dict):
                    oi = nd.get("output_index")
                    if oi is not None:
                        by_index[oi] = item
                    else:
                        fallback.append(item)
            if nd.get("type") in ("response.completed", "response.incomplete"):
                resp = nd.get("response")
                if isinstance(resp, dict):
                    out = resp.get("output")
                    if (not isinstance(out, list) or len(out) == 0) and (by_index or fallback):
                        items = [by_index[k] for k in sorted(by_index.keys())] + list(fallback)
                        resp["output"] = items
                        nd["response"] = resp
                terminal = nd
    return terminal


# --- non-streaming synthesis (ConvertCodexResponseToClaudeNonStream) --------

def non_stream_response(completed_data, short_to_original=None):
    """Synthesize one Anthropic Messages JSON response from a response.completed data dict.

    Returns a dict, or None if the data isn't a terminal event.
    """
    short_to_original = short_to_original or {}
    if not isinstance(completed_data, dict):
        return None
    if completed_data.get("type") not in ("response.completed", "response.incomplete"):
        return None
    resp = completed_data.get("response") or {}
    out = {
        "id": resp.get("id", ""), "type": "message", "role": "assistant",
        "model": resp.get("model", ""), "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    inp, outp, cached = extract_usage(resp.get("usage"))
    out["usage"]["input_tokens"] = inp
    out["usage"]["output_tokens"] = outp
    if cached > 0:
        out["usage"]["cache_read_input_tokens"] = cached

    has_tool_call = False
    web_seen = set()
    for item in (resp.get("output") or []):
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "reasoning":
            thinking = ""
            signature = item.get("encrypted_content", "")
            summary = item.get("summary")
            if isinstance(summary, list):
                thinking = "".join(p.get("text", p) if isinstance(p, dict) else str(p) for p in summary)
            elif isinstance(summary, str):
                thinking = summary
            if not thinking:
                content = item.get("content")
                if isinstance(content, list):
                    thinking = "".join(p.get("text", p) if isinstance(p, dict) else str(p) for p in content)
                elif isinstance(content, str):
                    thinking = content
            if thinking or signature:
                block = {"type": "thinking", "thinking": thinking}
                if signature:
                    block["signature"] = signature
                out["content"].append(block)
        elif itype == "message":
            content = item.get("content")
            parts = content if isinstance(content, list) else []
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    txt = part.get("text", "")
                    if txt:
                        out["content"].append({"type": "text", "text": txt})
            if not parts and isinstance(content, str) and content:
                out["content"].append({"type": "text", "text": content})
        elif itype == "web_search_call":
            _append_web_search_nonstream(out, item, web_seen)
        elif itype == "function_call":
            has_tool_call = True
            name = short_to_original.get(item.get("name", ""), item.get("name", ""))
            block = {
                "type": "tool_use",
                "id": tr.shorten_call_id(sanitize_tool_id(item.get("call_id", ""))),
                "name": name, "input": {},
            }
            args = item.get("arguments", "")
            if args:
                try:
                    parsed = json.loads(args)
                    if isinstance(parsed, dict):
                        block["input"] = parsed
                except ValueError:
                    pass
            out["content"].append(block)

    out["stop_reason"] = map_stop_reason(codex_stop_reason(resp), has_tool_call)
    seq = _codex_stop_sequence(resp)
    if seq:
        out["stop_sequence"] = seq
    return out


def _append_web_search_nonstream(out, item, seen):
    wid = (item.get("id") or "").strip()
    if not wid or wid in seen:
        return
    query = ""
    for path in ("action.query", "query", "input.query"):
        v = _get(item, path)
        if v:
            query = v
            break
    results = []
    raw_results = item.get("results")
    if isinstance(raw_results, list):
        for r in raw_results:
            if not isinstance(r, dict):
                continue
            url = (r.get("url") or "").strip()
            if not url:
                continue
            title = (r.get("title") or "").strip() or url
            results.append({"type": "web_search_result", "title": title, "url": url, "page_age": None})
    if not query and not results:
        return
    use_block = {"type": "server_tool_use", "id": wid, "name": "web_search", "input": {}}
    if query:
        use_block["input"] = {"query": query}
    out["content"].append(use_block)
    out["content"].append({"type": "web_search_tool_result", "tool_use_id": wid, "content": results})
    seen.add(wid)
