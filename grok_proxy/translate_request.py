"""Anthropic Messages -> xAI Responses request translation.

Ported from CLIProxyAPI/internal/translator/codex/claude/codex_claude_request.go,
with helpers from internal/thinking/convert.go, internal/signature/gpt_validation.go,
internal/util/claude_attribution.go and internal/translator/common/claude_system.go.

translate_request(model_name, raw_dict) -> (body_dict, short_to_original_name_map).
The reverse name map is returned so the response translator can restore original
tool names on emitted tool_use blocks.
"""
import base64
import hashlib
import json
from typing import Any, Dict, List, Tuple

NAME_LIMIT = 64
CALL_ID_LIMIT = 64

ATTRIBUTION_PREFIX = "x-anthropic-billing-header:"
MAX_GPT_SIG_LEN = 32 * 1024 * 1024

# budget_tokens -> reasoning effort level (internal/thinking/convert.go)
THRESHOLD_MINIMAL = 512
THRESHOLD_LOW = 1024
THRESHOLD_MEDIUM = 8192
THRESHOLD_HIGH = 24576


# --- small helpers ----------------------------------------------------------

def is_claude_code_attribution(text):
    return text.lstrip().startswith(ATTRIBUTION_PREFIX)


def _gpt_sig_invalid_char(sig):
    for i, ch in enumerate(sig):
        if not (("A" <= ch <= "Z") or ("a" <= ch <= "z") or ("0" <= ch <= "9")
                or ch in "-_="):
            return i, ch
    return -1, None


def is_valid_gpt_reasoning_signature(sig):
    """Fernet-like transport-shape check for GPT/Codex/xAI reasoning encrypted_content.

    Ported from internal/signature/gpt_validation.go InspectGPTReasoningSignature.
    Returns True iff the signature is replay-safe to forward back as
    encrypted_content. A thinking block whose signature fails this is dropped.
    """
    sig = (sig or "").strip()
    if not sig:
        return False
    if len(sig) > MAX_GPT_SIG_LEN:
        return False
    idx, _ = _gpt_sig_invalid_char(sig)
    if idx >= 0:
        return False
    if not sig.startswith("gAAAA"):
        return False
    decoded = None
    try:
        decoded = base64.urlsafe_b64decode(sig.rstrip("=").replace("=", "") + "==")
    except Exception:
        pass
    if decoded is None:
        try:
            decoded = base64.urlsafe_b64decode(sig)
        except Exception:
            decoded = None
    # Go tries RawURLEncoding then URLEncoding; urlsafe_b64decode wants padding.
    if decoded is None:
        try:
            pad = "=" * (-len(sig) % 4)
            decoded = base64.urlsafe_b64decode(sig + pad)
        except Exception:
            return False
    if len(decoded) < 73:
        return False
    if decoded[0] != 0x80:
        return False
    ciphertext_len = len(decoded) - 1 - 8 - 16 - 32
    if ciphertext_len <= 0 or ciphertext_len % 16 != 0:
        return False
    return True


# NOTE: Grok's native reasoning encrypted_content is intentionally NOT forwarded
# back to xAI (see the thinking-block handler in translate_request). It is not
# replayable via the transcript — xAI rejects it ("Could not decode the
# compaction blob"). Only GPT/Fernet signatures pass; Grok thinking blocks are
# dropped and xAI re-reasons each turn. This matches the Go translator exactly.


# --- name / call_id shortening (codex_claude_request.go:359-522) -----------

def _base_short_candidate(name):
    if len(name) <= NAME_LIMIT:
        return name
    if name.startswith("mcp__"):
        idx = name.rfind("__")
        if idx > 0:
            cand = "mcp__" + name[idx + 2:]
            return cand[:NAME_LIMIT] if len(cand) > NAME_LIMIT else cand
    return name[:NAME_LIMIT]


def shorten_name_if_needed(name):
    """Standalone shortening of a single name (no uniqueness suffix)."""
    return _base_short_candidate(name)


def build_short_name_map(names):
    """original -> short, ensuring uniqueness within the request (buildShortNameMap)."""
    used = set()
    out = {}
    for n in names:
        cand = _base_short_candidate(n)
        if cand in used:
            base = cand
            i = 1
            while True:
                suffix = "_" + str(i)
                allowed = max(NAME_LIMIT - len(suffix), 0)
                tmp = base[:allowed] + suffix if len(base) > allowed else base + suffix
                if tmp not in used:
                    cand = tmp
                    break
                i += 1
        used.add(cand)
        out[n] = cand
    return out


def shorten_call_id(id_):
    """Keep Claude tool_use ids within the Responses call_id limit (shortenCodexCallIDIfNeeded)."""
    if len(id_) <= CALL_ID_LIMIT:
        return id_
    suffix = "_" + hashlib.sha256(id_.encode("utf-8")).hexdigest()[:16]
    prefix_len = CALL_ID_LIMIT - len(suffix)
    if prefix_len <= 0:
        return suffix[-CALL_ID_LIMIT:]
    return id_[:prefix_len] + suffix


def _map_tool_name(name, original_to_short):
    if name in original_to_short:
        return original_to_short[name]
    return shorten_name_if_needed(name)


# --- tool parameters normalization (codex_claude_request.go:545-562) --------

def normalize_tool_parameters(schema):
    """Ensure an object schema has type:object and a properties map."""
    if schema is None:
        return {"type": "object", "properties": {}}
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = dict(schema)
    st = out.get("type")
    if not st:
        out["type"] = "object"
        st = "object"
    if st == "object" and "properties" not in out:
        out["properties"] = {}
    out.pop("$schema", None)
    return out


# --- budget -> level (internal/thinking/convert.go) -------------------------

def convert_budget_to_level(budget):
    if budget < -1:
        return None
    if budget == -1:
        return "auto"
    if budget == 0:
        return "none"
    if budget <= THRESHOLD_MINIMAL:
        return "minimal"
    if budget <= THRESHOLD_LOW:
        return "low"
    if budget <= THRESHOLD_MEDIUM:
        return "medium"
    if budget <= THRESHOLD_HIGH:
        return "high"
    return "xhigh"


# --- role:system reminder extraction (common/claude_system.go) --------------

SYSTEM_REMINDER_START = "<system-reminder>"
SYSTEM_REMINDER_END = "</system-reminder>"


def _claude_system_text_parts(content):
    if content is None:
        return []
    if isinstance(content, str):
        if content == "" or is_claude_code_attribution(content):
            return []
        return [content]
    if not isinstance(content, list):
        return []
    parts = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text", "")
        if text == "" or is_claude_code_attribution(text):
            continue
        parts.append(text)
    return parts


def claude_system_reminder_text(content):
    parts = _claude_system_text_parts(content)
    if not parts:
        return None
    text = "\n".join(parts)
    if not text.strip():
        return None
    return SYSTEM_REMINDER_START + "\n" + text + "\n" + SYSTEM_REMINDER_END


# --- image source -> data URL (codex_claude_request.go:165-183) ------------

def _image_data_url(source):
    if not isinstance(source, dict):
        return None
    data = source.get("data") or source.get("base64") or ""
    if not data:
        return None
    media = source.get("media_type") or source.get("mime_type") or "application/octet-stream"
    return "data:%s;base64,%s" % (media, data)


def is_web_search_tool_type(t):
    return t in ("web_search_20250305", "web_search_20260209")


def convert_tool_choice(tool_choice, original_to_short, web_search_names):
    """Anthropic tool_choice -> xAI Responses tool_choice (convertClaudeToolChoiceToCodex)."""
    if not tool_choice or not isinstance(tool_choice, dict):
        return "auto"
    choice_type = tool_choice.get("type")
    if not choice_type and isinstance(tool_choice.get("type"), str):
        choice_type = tool_choice["type"]
    # Anthropic sometimes sends tool_choice as a bare string (handled by caller path);
    # if it's a string at top-level it arrives as a non-dict, handled above.
    if choice_type in ("auto", "", None):
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool":
        name = tool_choice.get("name", "")
        if name in web_search_names:
            return {"type": "web_search"}
        name = _map_tool_name(name, original_to_short)
        if not name:
            return "auto"
        return {"type": "function", "name": name}
    return "auto"


def normalize_service_tier(service_tier):
    if not isinstance(service_tier, str):
        return ""
    st = service_tier.strip().lower()
    if st in ("fast", "priority"):
        return "priority"
    return ""


# --- main translator --------------------------------------------------------

def translate_request(model_name, raw):
    """Translate an Anthropic Messages request dict into an xAI Responses body dict.

    Returns (body, short_to_original) where short_to_original maps the shortened
    tool/call names back to the originals (for the response translator).
    """
    raw = raw or {}

    # Build original->short name map from the tools list (buildReverseMapFromClaudeOriginalToShort)
    tool_names = []
    tools_raw = raw.get("tools")
    if isinstance(tools_raw, list):
        for t in tools_raw:
            if isinstance(t, dict) and t.get("name"):
                tool_names.append(t["name"])
    original_to_short = build_short_name_map(tool_names) if tool_names else {}
    short_to_original = {v: k for k, v in original_to_short.items()}

    body = {"model": model_name, "instructions": "", "input": []}
    inp = body["input"]

    # --- system -> developer input item (codex_claude_request.go:51-81) -----
    system = raw.get("system")
    system_parts = []
    if isinstance(system, str):
        if system and not is_claude_code_attribution(system):
            system_parts.append(system)
    elif isinstance(system, list):
        for blk in system:
            if isinstance(blk, dict) and blk.get("type") == "text":
                txt = blk.get("text", "")
                if txt and not is_claude_code_attribution(txt):
                    system_parts.append(txt)
    if system_parts:
        inp.append({
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": t} for t in system_parts],
        })

    # --- messages -> input items (codex_claude_request.go:84-258) -----------
    messages = raw.get("messages")
    if isinstance(messages, list):
        for message in messages:
            role = message.get("role", "")
            content = message.get("content")

            if role == "system":
                reminder = claude_system_reminder_text(content)
                if reminder:
                    inp.append({
                        "type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": reminder}],
                    })
                continue

            def new_message():
                return {"type": "message", "role": role, "content": []}

            current = new_message()

            def flush():
                # closure over `current` via nonlocal
                pass

            # We can't reassign `current` from a nested closure cleanly in a loop,
            # so inline the flush logic with an index-based holder.
            holder = {"msg": current}

            def _append_text(text):
                part_type = "output_text" if role == "assistant" else "input_text"
                holder["msg"]["content"].append({"type": part_type, "text": text})

            def _append_image(data_url):
                holder["msg"]["content"].append({"type": "input_image", "image_url": data_url})

            def _flush():
                if holder["msg"]["content"]:
                    inp.append(holder["msg"])
                    holder["msg"] = new_message()

            if isinstance(content, list):
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    ctype = blk.get("type")
                    if ctype == "text":
                        _append_text(blk.get("text", ""))
                    elif ctype == "thinking":
                        # Only forward GPT/Fernet (gAAAA) reasoning signatures.
                        # Grok's native encrypted_content is NOT replayable via
                        # the transcript — xAI rejects it ("Could not decode the
                        # compaction blob"), so Grok thinking blocks are dropped
                        # here and xAI re-reasons each turn. (Matches the Go
                        # translator's SignatureProviderGPT check.)
                        if role == "assistant":
                            sig = blk.get("signature", "")
                            if is_valid_gpt_reasoning_signature(sig):
                                _flush()
                                inp.append({
                                    "type": "reasoning", "summary": [], "content": None,
                                    "encrypted_content": sig,
                                })
                    elif ctype == "image":
                        url = _image_data_url(blk.get("source"))
                        if url:
                            _append_image(url)
                    elif ctype == "tool_use":
                        _flush()
                        name = _map_tool_name(blk.get("name", ""), original_to_short)
                        # Go uses gjson `.Raw` of `input` -> the raw JSON string of
                        # the input object. The Responses API expects arguments as
                        # a JSON string.
                        if "input" in blk and blk["input"] is not None:
                            args = json.dumps(blk["input"])
                        else:
                            args = ""
                        inp.append({
                            "type": "function_call",
                            "call_id": shorten_call_id(blk.get("id", "")),
                            "name": name,
                            "arguments": args,
                        })
                    elif ctype == "tool_result":
                        _flush()
                        out_content = blk.get("content")
                        if isinstance(out_content, list):
                            parts = []
                            for sub in out_content:
                                if not isinstance(sub, dict):
                                    continue
                                st = sub.get("type")
                                if st == "image":
                                    url = _image_data_url(sub.get("source"))
                                    if url:
                                        parts.append({"type": "input_image", "image_url": url})
                                elif st == "text":
                                    parts.append({"type": "input_text", "text": sub.get("text", "")})
                            output = parts if parts else blk.get("content", "")
                        else:
                            output = out_content
                        inp.append({
                            "type": "function_call_output",
                            "call_id": shorten_call_id(blk.get("tool_use_id", "")),
                            "output": output,
                        })
                _flush()
            elif isinstance(content, str):
                _append_text(content)
                _flush()

    # --- tools (codex_claude_request.go:260-294) ---------------------------
    web_search_names = set()
    if isinstance(tools_raw, list) and tools_raw:
        out_tools = []
        for t in tools_raw:
            if not isinstance(t, dict):
                continue
            if is_web_search_tool_type(t.get("type")):
                ws = {"type": "web_search"}
                if isinstance(t.get("allowed_domains"), list):
                    ws["filters"] = {"allowed_domains": t["allowed_domains"]}
                if isinstance(t.get("user_location"), dict):
                    ws["user_location"] = t["user_location"]
                out_tools.append(ws)
                if t.get("name"):
                    web_search_names.add(t["name"])
                continue
            name = _map_tool_name(t.get("name", ""), original_to_short)
            tool = {"type": "function", "name": name}
            if "description" in t:
                tool["description"] = t["description"]
            tool["parameters"] = normalize_tool_parameters(t.get("input_schema"))
            tool["strict"] = False
            out_tools.append(tool)
        body["tools"] = out_tools
        body["tool_choice"] = convert_tool_choice(raw.get("tool_choice"), original_to_short, web_search_names)

    # --- parallel tool calls (default true unless disabled) -----------------
    parallel = True
    tc = raw.get("tool_choice")
    if isinstance(tc, dict) and tc.get("disable_parallel_tool_use") is True:
        parallel = False
    body["parallel_tool_calls"] = parallel

    # --- thinking -> reasoning effort (codex_claude_request.go:305-341) ----
    reasoning_effort = "medium"
    thinking = raw.get("thinking")
    if isinstance(thinking, dict):
        ttype = thinking.get("type")
        if ttype == "enabled":
            budget = thinking.get("budget_tokens")
            if isinstance(budget, (int, float)):
                lvl = convert_budget_to_level(int(budget))
                if lvl:
                    reasoning_effort = lvl
        elif ttype in ("adaptive", "auto"):
            effort = ""
            oc = raw.get("output_config")
            if isinstance(oc, dict) and isinstance(oc.get("effort"), str):
                effort = oc["effort"].strip().lower()
            reasoning_effort = effort if effort else "xhigh"
        elif ttype == "disabled":
            reasoning_effort = convert_budget_to_level(0)  # "none"
    body["reasoning"] = {"effort": reasoning_effort, "summary": "auto"}

    stier = normalize_service_tier(raw.get("service_tier"))
    if stier:
        body["service_tier"] = stier

    body["stream"] = True
    body["store"] = False
    body["include"] = ["reasoning.encrypted_content"]

    return body, short_to_original
