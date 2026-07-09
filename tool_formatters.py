"""Per-tool detail formatter registry.

The single big if/elif chain that used to live in OutputView._format_tool_detail
is replaced by a name → formatter dict. Each formatter receives the tool input
dict (and optionally the OutputView for access to helpers like _find_line_number,
_format_bash_result, etc.) and returns the detail string starting with ": ".

Adding a tool = one entry in TOOL_FORMATTERS instead of editing a 90-line chain.
Unknown tool names fall through to the MCP-style result formatter (when applicable).
"""
import json
import os
import re
from typing import Callable, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .output import OutputView, ToolCall

# Grok Imagine / video tools — result is a saved file path.
MEDIA_TOOLS = frozenset({
    "image_gen", "image_edit", "image_to_video", "reference_to_video", "video_gen",
})
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv")
MEDIA_EXTS = IMAGE_EXTS + VIDEO_EXTS

# X / Twitter search tools.
X_SEARCH_TOOLS = frozenset({
    "x_keyword_search", "x_semantic_search", "x_user_search", "x_thread_fetch",
})


def extract_media_path(result: Optional[str], tool_input: Optional[dict] = None) -> Optional[str]:
    """Best-effort absolute path from media tool result / input."""
    candidates = []
    if isinstance(tool_input, dict):
        for k in ("path", "file_path", "output_path", "image", "filename"):
            v = tool_input.get(k)
            if isinstance(v, str) and v.strip():
                candidates.append(v.strip())
        # image_edit may pass image as list of refs
        imgs = tool_input.get("images")
        if isinstance(imgs, list):
            for v in imgs:
                if isinstance(v, str) and v.strip():
                    candidates.append(v.strip())

    text = (result or "").strip()
    if text:
        # Whole-result JSON: {"path":"…","filename":"1.jpg",…}
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                for k in ("path", "file_path", "output_path", "filename"):
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        candidates.append(v.strip())
            elif isinstance(obj, str):
                candidates.append(obj)
        except Exception:
            pass
        # Embedded JSON object
        for m in re.finditer(r'\{[^{}]*"(?:path|file_path|filename)"[^{}]*\}', text):
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    for k in ("path", "file_path", "filename"):
                        v = obj.get(k)
                        if isinstance(v, str) and v.strip():
                            candidates.append(v.strip())
            except Exception:
                pass
        # Bare absolute/relative media paths in text
        for m in re.finditer(
                r'(?:/|~/)[^\s"\']+?\.(?:png|jpe?g|webp|gif|bmp|mp4|webm|mov|mkv)\b',
                text, re.I):
            candidates.append(m.group(0))
        for m in re.finditer(
                r'(?:images|videos)/\S+?\.(?:png|jpe?g|webp|gif|bmp|mp4|webm|mov|mkv)\b',
                text, re.I):
            candidates.append(m.group(0))

    for c in candidates:
        p = os.path.expanduser(c)
        if os.path.isfile(p):
            return p
        # session-relative images/1.jpg — leave to caller with session root
        if c.startswith(("images/", "videos/")) or not os.path.isabs(p):
            # Prefer absolute paths that exist; keep first absolute even if missing
            # (file may still be writing).
            pass
    # Prefer first absolute-looking candidate even if not yet on disk
    for c in candidates:
        p = os.path.expanduser(c)
        if os.path.isabs(p) and p.lower().endswith(MEDIA_EXTS):
            return p
    for c in candidates:
        if c.lower().endswith(MEDIA_EXTS):
            return os.path.expanduser(c)
    return None


def media_display_path(path: str) -> str:
    """Short label: images/1.jpg or basename."""
    if not path:
        return ""
    norm = path.replace("\\", "/")
    for marker in ("/images/", "/videos/"):
        idx = norm.find(marker)
        if idx >= 0:
            return norm[idx + 1:]  # images/1.jpg
    if norm.startswith(("images/", "videos/")):
        return norm
    return os.path.basename(path)


def is_image_path(path: str) -> bool:
    return bool(path) and path.lower().endswith(IMAGE_EXTS)


def is_video_path(path: str) -> bool:
    return bool(path) and path.lower().endswith(VIDEO_EXTS)


def _clip(s: str, n: int = 70) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _media_path_suffix(tool: "ToolCall") -> str:
    path = None
    if isinstance(tool.tool_input, dict):
        path = tool.tool_input.get("_media_path") or tool.tool_input.get("path")
    if not path:
        path = extract_media_path(tool.result, tool.tool_input)
    if not path:
        return ""
    if tool.status in ("done", "error"):
        label = media_display_path(path)
        # Images get an inline minihtml phantom; videos: path only (reveal via click).
        return f" → {label}"
    return ""


def _image_gen(view: "OutputView", tool: "ToolCall") -> str:
    prompt = _clip(tool.tool_input.get("prompt", "") if tool.tool_input else "")
    out = f": {prompt}" if prompt else ""
    out += _media_path_suffix(tool)
    return out


def _image_edit(view: "OutputView", tool: "ToolCall") -> str:
    prompt = _clip(tool.tool_input.get("prompt", "") if tool.tool_input else "")
    out = f": {prompt}" if prompt else ": edit"
    out += _media_path_suffix(tool)
    return out


def _image_to_video(view: "OutputView", tool: "ToolCall") -> str:
    prompt = _clip(tool.tool_input.get("prompt", "") if tool.tool_input else "")
    out = f": {prompt}" if prompt else ": animate"
    out += _media_path_suffix(tool)
    return out


def _reference_to_video(view: "OutputView", tool: "ToolCall") -> str:
    prompt = _clip(tool.tool_input.get("prompt", "") if tool.tool_input else "")
    out = f": {prompt}" if prompt else ": refs→video"
    out += _media_path_suffix(tool)
    return out


def _video_gen(view: "OutputView", tool: "ToolCall") -> str:
    prompt = _clip(tool.tool_input.get("prompt", "") if tool.tool_input else "")
    out = f": {prompt}" if prompt else ""
    out += _media_path_suffix(tool)
    return out


def _x_search(view: "OutputView", tool: "ToolCall") -> str:
    """x_keyword_search / x_semantic_search — query + optional result count."""
    inp = tool.tool_input or {}
    q = inp.get("query") or inp.get("q") or ""
    out = f": {_clip(str(q), 80)}" if q else ""
    if tool.result and tool.status == "done":
        out += view._format_x_search_result(tool.result)
    return out


def _x_user_search(view: "OutputView", tool: "ToolCall") -> str:
    inp = tool.tool_input or {}
    q = inp.get("query") or inp.get("q") or inp.get("username") or ""
    out = f": {_clip(str(q), 60)}" if q else ""
    if tool.result and tool.status == "done":
        out += view._format_x_search_result(tool.result)
    return out


def _x_thread_fetch(view: "OutputView", tool: "ToolCall") -> str:
    inp = tool.tool_input or {}
    tid = inp.get("post_id") or inp.get("tweet_id") or inp.get("id") or ""
    out = f": #{tid}" if tid else ""
    if tool.result and tool.status == "done":
        out += view._format_x_search_result(tool.result)
    return out


def _bash(view: "OutputView", tool: "ToolCall") -> str:
    cmd = tool.tool_input.get("command", "")
    # The agent emits multi-line bash; the tool line is a single-line syntax
    # scope (^\s*☐ .+$), so flatten newlines or they spill below it unscoped.
    if "\n" in cmd:
        cmd = " ⏎ ".join(s.strip() for s in cmd.splitlines() if s.strip())
    out = f": {cmd}"
    if tool.result and tool.status in ("done", "error"):
        out += view._format_bash_result(tool.result)
    return out


def _read(view: "OutputView", tool: "ToolCall") -> str:
    out = f": {tool.tool_input.get('file_path', '')}"
    if tool.result and tool.status == "done":
        out += view._format_read_result(tool.result)
    return out


def _read_image(view: "OutputView", tool: "ToolCall") -> str:
    ti = tool.tool_input or {}
    path = ti.get("path") or ti.get("file_path") or ti.get("target_file") or ""
    out = f": {path}" if path else ""
    if tool.status == "error" and tool.result:
        out += f" ✗ {_clip(str(tool.result), 60)}"
    elif tool.status == "done":
        out += " ✓ vision"
    return out


def _scheduler_create(view: "OutputView", tool: "ToolCall") -> str:
    ti = tool.tool_input or {}
    interval = ti.get("interval") or ti.get("cron") or ""
    prompt = _clip(str(ti.get("prompt") or ""), 50)
    bits = []
    if interval:
        bits.append(str(interval))
    if prompt:
        bits.append(prompt)
    out = (": " + " · ".join(bits)) if bits else ""
    if tool.status == "done":
        out += " ↻ armed"
    elif tool.status == "error" and tool.result:
        out += f" ✗ {_clip(str(tool.result), 40)}"
    return out


def _scheduler_list(view: "OutputView", tool: "ToolCall") -> str:
    if tool.status == "done" and tool.result:
        return f": {_clip(str(tool.result), 60)}"
    return ""


def _scheduler_delete(view: "OutputView", tool: "ToolCall") -> str:
    ti = tool.tool_input or {}
    tid = ti.get("id") or ti.get("task_id") or ""
    out = f": {tid}" if tid else ""
    if tool.status == "done":
        out += " ✓ cancelled"
    return out


def _edit(view: "OutputView", tool: "ToolCall") -> str:
    file_path = tool.tool_input.get("file_path", "")
    # A failed edit never applied — its diff is misleading noise, so hide it.
    if tool.status == "error":
        return f": {file_path}"
    old = tool.tool_input.get("old_string", "")
    new = tool.tool_input.get("new_string", "")
    unified = tool.tool_input.get("unified_diff", "")
    if unified:
        diff_str = view._format_unified_diff(unified)
        line_num = view._extract_diff_line_num(unified)
    else:
        diff_str = view._format_edit_diff(old, new)
        line_num = view._find_line_number(file_path, old, new)
    out = f": {file_path}:{line_num}" if line_num else f": {file_path}"
    if diff_str:
        out += diff_str
    return out


def _write(view: "OutputView", tool: "ToolCall") -> str:
    """Write: path + size, same spirit as Claude Code Read (lines/bytes).

    Claude SDK puts body in tool_input.content. ACP/Grok often only expose
    path + newText (diff) or `contents` / `new_string` — accept all.
    """
    import os
    ti = tool.tool_input or {}
    path = (
        ti.get("file_path")
        or ti.get("path")
        or ti.get("target_file")
        or ti.get("filePath")
        or ""
    )
    content = (
        ti.get("content")
        or ti.get("contents")
        or ti.get("new_string")
        or ti.get("newText")
        or ""
    )
    if not isinstance(content, str):
        content = str(content) if content is not None else ""
    out = f": {path}" if path else ""
    nbytes = 0
    lines = 0
    if content:
        lines = len(content.splitlines()) or (1 if content else 0)
        nbytes = len(content.encode("utf-8", "replace"))
    elif tool.status == "done" and path and os.path.isfile(path):
        # Content stripped from tool stream — measure written file on disk.
        try:
            nbytes = os.path.getsize(path)
            with open(path, "rb") as f:
                # Count newlines without loading whole file into str if huge
                data = f.read()
            lines = data.count(b"\n")
            if data and not data.endswith(b"\n"):
                lines += 1
        except OSError:
            pass
    if nbytes or lines:
        size = f"{nbytes / 1024:.1f} KB" if nbytes >= 1024 else f"{nbytes} B"
        out += f" → {lines} lines, {size}"
    return out


def _glob(view: "OutputView", tool: "ToolCall") -> str:
    out = f": {tool.tool_input.get('pattern', '')}"
    if tool.result and tool.status == "done":
        out += view._format_glob_result(tool.result)
    return out


def _grep(view: "OutputView", tool: "ToolCall") -> str:
    out = f": {tool.tool_input.get('pattern', '')}"
    if tool.result and tool.status == "done":
        out += view._format_grep_result(tool.result)
    return out


def _websearch(view: "OutputView", tool: "ToolCall") -> str:
    q = tool.tool_input.get("query", "") if tool.tool_input else ""
    out = f": {q}" if q else ""
    if tool.result and tool.status in ("done", "error"):
        out += view._format_websearch_result(tool.result)
    return out


def _search_tool(view: "OutputView", tool: "ToolCall") -> str:
    """Grok search_tool — discover MCP tools by query (not WebSearch)."""
    q = tool.tool_input.get("query", "")
    return f": {q}" if q else ""


def _update_goal(view: "OutputView", tool: "ToolCall") -> str:
    """Grok update_goal — single active-goal progress (not a Task list)."""
    inp = tool.tool_input or {}
    if inp.get("blocked_reason"):
        return f": blocked — {inp['blocked_reason']}"
    if inp.get("completed") is True:
        msg = (inp.get("message") or "").strip()
        return f": completed — {msg}" if msg else ": completed"
    msg = (inp.get("message") or "").strip()
    return f": {msg}" if msg else ": progress"


def _webfetch(view: "OutputView", tool: "ToolCall") -> str:
    return f": {tool.tool_input.get('url', '')}"


def _task(view: "OutputView", tool: "ToolCall") -> str:
    sub = tool.tool_input.get("subagent_type", "") or ""
    desc = tool.tool_input.get("description", "") or ""
    if sub and desc:
        return f": {sub} - {desc}"
    if sub or desc:
        return f": {sub or desc}"
    return ""


def _notebook_edit(view: "OutputView", tool: "ToolCall") -> str:
    return f": {tool.tool_input.get('notebook_path', '')}"


def _todo_write(view: "OutputView", tool: "ToolCall") -> str:
    todos = tool.tool_input.get("todos", [])
    count = len(todos) if isinstance(todos, list) else "?"
    return f": {count} task{'s' if count != 1 else ''}"


def _task_create(view: "OutputView", tool: "ToolCall") -> str:
    subject = tool.tool_input.get("subject", "") or tool.tool_input.get("description", "")
    return f": {subject}" if subject else ""


def _task_update(view: "OutputView", tool: "ToolCall") -> str:
    tid = tool.tool_input.get("taskId", "")
    status = tool.tool_input.get("status")
    subject = tool.tool_input.get("subject")
    bits = []
    if tid:
        bits.append(f"#{tid}")
    if status:
        bits.append(status)
    if subject:
        bits.append(subject)
    return f": {' '.join(bits)}" if bits else ""


def _task_list(view: "OutputView", tool: "ToolCall") -> str:
    return ""


def _task_get(view: "OutputView", tool: "ToolCall") -> str:
    tid = tool.tool_input.get("taskId", "")
    return f": #{tid}" if tid else ""


def _ask_user(view: "OutputView", tool: "ToolCall") -> str:
    question = tool.tool_input.get("question", "")
    out = f": {question}"
    if tool.result and tool.status == "done":
        out += view._format_ask_user_result(tool.result, question)
    return out


def _skill(view: "OutputView", tool: "ToolCall") -> str:
    return f": {tool.tool_input.get('skill', '')}"


def _enter_plan_mode(view: "OutputView", tool: "ToolCall") -> str:
    return ": entering plan mode..."


def _exit_plan_mode(view: "OutputView", tool: "ToolCall") -> str:
    allowed = tool.tool_input.get("allowedPrompts", [])
    if allowed:
        return f": {len(allowed)} requested permissions"
    return ": awaiting approval..."


# ── Sublime built-in MCP tools (mcp/server.py) ─────────────────────────
# Names arrive as mcp__sublime__X, sublime__X, bare X, or use_tool wrapper.

def _ti(tool: "ToolCall") -> dict:
    return tool.tool_input if isinstance(tool.tool_input, dict) else {}


def _join_bits(*parts) -> str:
    bits = [p for p in parts if p]
    return (": " + " · ".join(bits)) if bits else ""


def _basename(path: str) -> str:
    if not path:
        return ""
    return os.path.basename(str(path).rstrip("/")) or str(path)


def _mcp_short_name(name: str) -> str:
    """mcp__sublime__find_file / sublime__find_file → find_file."""
    n = (name or "").strip()
    if n.startswith("mcp__sublime__"):
        return n[len("mcp__sublime__"):]
    if n.startswith("sublime__"):
        return n[len("sublime__"):]
    if n.startswith("mcp__") and "__" in n[4:]:
        # mcp__other__tool → keep full for non-sublime
        return n
    return n


def _terminal_run(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    cmd = (ti.get("command") or "").strip()
    idx = ti.get("index")
    tag = ti.get("tag") or ti.get("target_id") or ""
    bits = []
    if idx is not None:
        bits.append(f"#{idx}")
    if tag:
        bits.append(str(tag)[:24])
    if cmd:
        bits.append(_clip(cmd, 70))
    out = _join_bits(*bits)
    if tool.status == "done" and tool.result and "[timed out]" in str(tool.result):
        out += " (timed out)"
    return out


def _terminal_read(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    idx = ti.get("index")
    lines = ti.get("lines")
    tag = ti.get("tag") or ti.get("target_id") or ""
    bits = []
    if idx is not None:
        bits.append(f"#{idx}")
    if tag:
        bits.append(str(tag)[:24])
    if lines:
        bits.append(f"{lines} lines")
    return _join_bits(*bits) if bits else ""


def _terminal_list(view: "OutputView", tool: "ToolCall") -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _terminal_close(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    idx = ti.get("index")
    tag = ti.get("tag") or ti.get("target_id") or ""
    bits = []
    if idx is not None:
        bits.append(f"#{idx}")
    if tag:
        bits.append(str(tag)[:24])
    return _join_bits(*bits) if bits else ": session terminal"


def _find_file(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    q = ti.get("query") or ""
    pat = ti.get("pattern") or ""
    lim = ti.get("limit")
    bits = [_clip(str(q), 50)] if q else []
    if pat:
        bits.append(f"glob {pat}")
    if lim:
        bits.append(f"limit {lim}")
    out = _join_bits(*bits)
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _get_symbols(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    q = ti.get("query") or ""
    if isinstance(q, list):
        q = ", ".join(str(x) for x in q[:5])
    fp = ti.get("file_path") or ""
    bits = [_clip(str(q), 50)] if q else []
    if fp:
        bits.append(_basename(fp))
    out = _join_bits(*bits)
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _goto_symbol(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    q = ti.get("query") or ""
    out = _join_bits(_clip(str(q), 60)) if q else ""
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _read_view(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    path = ti.get("file_path") or ti.get("path") or ti.get("view_name") or ""
    bits = []
    if path:
        bits.append(_basename(path) if "/" in str(path) or "\\" in str(path)
                    else _clip(str(path), 40))
    if ti.get("head"):
        bits.append(f"head {ti['head']}")
    if ti.get("tail"):
        bits.append(f"tail {ti['tail']}")
    if ti.get("grep"):
        bits.append(f"grep {_clip(str(ti['grep']), 30)}")
    if ti.get("grep_i"):
        bits.append(f"grep -i {_clip(str(ti['grep_i']), 30)}")
    out = _join_bits(*bits)
    if tool.status == "done" and tool.result:
        # Prefer line count over dumping buffer text.
        text = str(tool.result)
        n = text.count("\n") + (1 if text.strip() else 0)
        if n > 1 or len(text) > 80:
            out += f" → {n} lines"
        else:
            out += view._format_mcp_result(tool.result)
    return out


def _get_window_summary(view: "OutputView", tool: "ToolCall") -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ": window"
    return ": window"


def _list_backends(view: "OutputView", tool: "ToolCall") -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _list_profiles(view: "OutputView", tool: "ToolCall") -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _list_personas(view: "OutputView", tool: "ToolCall") -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _spawn_session(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    name = ti.get("name") or ""
    backend = ti.get("backend") or ""
    profile = ti.get("profile") or ""
    prompt = ti.get("prompt") or ""
    bits = []
    if name:
        bits.append(str(name)[:30])
    if backend and backend != "claude":
        bits.append(str(backend))
    if profile:
        bits.append(f"profile={profile}")
    if ti.get("fork_current"):
        bits.append("fork")
    if prompt:
        bits.append(_clip(str(prompt), 45))
    out = _join_bits(*bits)
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _send_to_session(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    vid = ti.get("view_id")
    prompt = ti.get("prompt") or ""
    bits = []
    if vid is not None:
        bits.append(f"view {vid}")
    if prompt:
        bits.append(_clip(str(prompt), 50))
    return _join_bits(*bits)


def _list_sessions(view: "OutputView", tool: "ToolCall") -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _read_session_output(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    vid = ti.get("view_id")
    lines = ti.get("lines")
    bits = []
    if vid is not None:
        bits.append(f"view {vid}")
    if lines:
        bits.append(f"{lines} lines")
    out = _join_bits(*bits)
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _list_profile_docs(view: "OutputView", tool: "ToolCall") -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _read_profile_doc(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    path = ti.get("path") or ""
    out = _join_bits(path) if path else ""
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _lsp(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    cmd = ti.get("cmd") or ti.get("command") or ""
    out = _join_bits(_clip(str(cmd), 70)) if cmd else ""
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _sublime_eval(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    code = ti.get("code") or ""
    out = _join_bits(_clip(str(code), 60)) if code else ""
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _sublime_tool(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    name = ti.get("name") or ""
    return _join_bits(name) if name else ""


def _list_tools(view: "OutputView", tool: "ToolCall") -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _set_timer(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    sec = ti.get("seconds")
    wake = ti.get("wake_prompt") or ""
    bits = []
    if sec is not None:
        bits.append(f"{sec}s")
    if wake:
        bits.append(_clip(str(wake), 40))
    return _join_bits(*bits)


def _signal_complete(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    sid = ti.get("session_id")
    summary = ti.get("result_summary") or ""
    bits = []
    if sid is not None:
        bits.append(f"session {sid}")
    if summary:
        bits.append(_clip(str(summary), 45))
    return _join_bits(*bits)


def _wait_for_subsession(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    sid = ti.get("subsession_id") or ""
    wake = ti.get("wake_prompt") or ""
    bits = []
    if sid:
        bits.append(_clip(str(sid), 24))
    if wake:
        bits.append(_clip(str(wake), 40))
    return _join_bits(*bits)


def _list_notifications(view: "OutputView", tool: "ToolCall") -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _discover_services(view: "OutputView", tool: "ToolCall") -> str:
    if tool.status == "done" and tool.result:
        return view._format_mcp_result(tool.result) or ""
    return ""


def _unregister_notification(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    nid = ti.get("notification_id") or ""
    return _join_bits(str(nid)[:40]) if nid else ""


def _subscribe(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    ntype = ti.get("notification_type") or ""
    wake = ti.get("wake_prompt") or ""
    bits = []
    if ntype:
        bits.append(str(ntype)[:40])
    if wake:
        bits.append(_clip(str(wake), 35))
    return _join_bits(*bits)


def _chatroom(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    cmd = ti.get("cmd") or ""
    out = _join_bits(_clip(str(cmd), 70)) if cmd else ""
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _garage_search(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    q = ti.get("query") or ""
    out = _join_bits(_clip(str(q), 55)) if q else ""
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _order(view: "OutputView", tool: "ToolCall") -> str:
    ti = _ti(tool)
    # order tool takes varied args depending on action
    action = ti.get("action") or ti.get("cmd") or ti.get("op") or ""
    prompt = ti.get("prompt") or ti.get("message") or ""
    path = ti.get("file_path") or ti.get("path") or ""
    bits = []
    if action:
        bits.append(str(action)[:20])
    if path:
        bits.append(_basename(path))
    if prompt:
        bits.append(_clip(str(prompt), 40))
    if not bits:
        # dump a few non-empty keys
        for k, v in list(ti.items())[:3]:
            if v is not None and v != "":
                bits.append(f"{k}={_clip(str(v), 24)}")
    out = _join_bits(*bits)
    if tool.status == "done" and tool.result:
        out += view._format_mcp_result(tool.result)
    return out


def _mcp_call_args_fallback(tool: "ToolCall") -> str:
    """Show key call args for unknown sublime MCP tools."""
    ti = _ti(tool)
    if not ti:
        return ""
    prefer = (
        "query", "cmd", "command", "path", "file_path", "prompt", "name",
        "view_id", "code", "pattern", "seconds", "notification_type",
        "subsession_id", "session_id", "wake_prompt", "backend", "profile",
    )
    bits = []
    seen = set()
    for k in prefer:
        if k not in ti or k in seen:
            continue
        v = ti[k]
        if v is None or v == "" or v == {} or v == []:
            continue
        seen.add(k)
        if k in ("path", "file_path"):
            bits.append(_basename(str(v)))
        else:
            bits.append(_clip(str(v), 40))
        if len(bits) >= 3:
            break
    if not bits:
        for k, v in list(ti.items())[:3]:
            if v is None or v == "" or str(k).startswith("_"):
                continue
            bits.append(f"{k}={_clip(str(v), 28)}")
    return _join_bits(*bits)


# Short-name registry for built-in sublime MCP tools.
SUBLIME_MCP_FORMATTERS: Dict[str, Callable] = {
    "get_window_summary": _get_window_summary,
    "find_file": _find_file,
    "get_symbols": _get_symbols,
    "goto_symbol": _goto_symbol,
    "read_view": _read_view,
    "read_image": _read_image,
    "list_backends": _list_backends,
    "list_profiles": _list_profiles,
    "list_personas": _list_personas,
    "spawn_session": _spawn_session,
    "send_to_session": _send_to_session,
    "list_sessions": _list_sessions,
    "read_session_output": _read_session_output,
    "list_profile_docs": _list_profile_docs,
    "read_profile_doc": _read_profile_doc,
    "lsp": _lsp,
    "sublime_eval": _sublime_eval,
    "sublime_tool": _sublime_tool,
    "list_tools": _list_tools,
    "terminal_list": _terminal_list,
    "terminal_run": _terminal_run,
    "terminal_read": _terminal_read,
    "terminal_send": _terminal_read,
    "terminal_close": _terminal_close,
    "set_timer": _set_timer,
    "signal_complete": _signal_complete,
    "wait_for_subsession": _wait_for_subsession,
    "list_notifications": _list_notifications,
    "discover_services": _discover_services,
    "unregister_notification": _unregister_notification,
    "subscribe": _subscribe,
    "chatroom": _chatroom,
    "garage_search": _garage_search,
    "order": _order,
    "ask_user": _ask_user,
}


def _register_sublime_mcp_aliases(registry: Dict[str, Callable]) -> None:
    for short, fmt in SUBLIME_MCP_FORMATTERS.items():
        registry.setdefault(short, fmt)
        registry.setdefault(f"mcp__sublime__{short}", fmt)
        registry.setdefault(f"sublime__{short}", fmt)


TOOL_FORMATTERS: Dict[str, Callable] = {
    "Bash": _bash,
    "Read": _read,
    "read_image": _read_image,
    "Edit": _edit,
    "Write": _write,
    "Glob": _glob,
    "Grep": _grep,
    "WebSearch": _websearch,
    "search_tool": _search_tool,
    "update_goal": _update_goal,
    "WebFetch": _webfetch,
    "Task": _task,
    "NotebookEdit": _notebook_edit,
    "TodoWrite": _todo_write,
    "TaskCreate": _task_create,
    "TaskUpdate": _task_update,
    "TaskList": _task_list,
    "TaskGet": _task_get,
    "ask_user": _ask_user,
    "Skill": _skill,
    "EnterPlanMode": _enter_plan_mode,
    "ExitPlanMode": _exit_plan_mode,
    # Media generation
    "image_gen": _image_gen,
    "image_edit": _image_edit,
    "image_to_video": _image_to_video,
    "reference_to_video": _reference_to_video,
    "video_gen": _video_gen,
    # X / Twitter
    "x_keyword_search": _x_search,
    "x_semantic_search": _x_search,
    "x_user_search": _x_user_search,
    "x_thread_fetch": _x_thread_fetch,
    # Scheduler / /loop
    "scheduler_create": _scheduler_create,
    "CronCreate": _scheduler_create,
    "scheduler_list": _scheduler_list,
    "CronList": _scheduler_list,
    "scheduler_delete": _scheduler_delete,
    "CronDelete": _scheduler_delete,
}
_register_sublime_mcp_aliases(TOOL_FORMATTERS)


def _unwrap_use_tool(tool: "ToolCall") -> "ToolCall":
    """Grok use_tool / CallMcpTool → inner sublime tool name + args."""
    name = tool.name or ""
    ti = _ti(tool)
    if name not in ("use_tool", "CallMcpTool", "call_mcp_tool"):
        # Still unwrap if tool_name is nested (some bridges keep outer name)
        if not (ti.get("tool_name") or ti.get("name")) or name.startswith(
                ("mcp__", "sublime__")):
            return tool
    inner_name = ti.get("tool_name") or ti.get("name") or name
    inner_input = ti.get("tool_input") or ti.get("arguments") or ti.get("input")
    if not isinstance(inner_input, dict):
        # tool_input may be the args themselves without nesting
        if any(k in ti for k in (
                "query", "path", "cmd", "command", "prompt", "file_path",
                "code", "view_id", "seconds")):
            inner_input = {k: v for k, v in ti.items()
                           if k not in ("tool_name", "name", "server")}
        else:
            inner_input = ti
    short = _mcp_short_name(str(inner_name))
    display = f"mcp__sublime__{short}" if short in SUBLIME_MCP_FORMATTERS else str(inner_name)
    return type(tool)(
        name=display,
        tool_input=inner_input if isinstance(inner_input, dict) else {},
        status=tool.status,
        result=tool.result,
        id=tool.id,
    )


def format_tool_detail(view: "OutputView", tool: "ToolCall") -> str:
    """Dispatch a tool to its formatter; return the full detail string.

    Built-in sublime MCP tools show call args (query, path, cmd, …) plus a
    compact result summary when done. Unknown mcp__sublime__* tools fall back
    to key args + generic result formatting.
    """
    t = _unwrap_use_tool(tool)
    short = _mcp_short_name(t.name)
    fmt = (
        TOOL_FORMATTERS.get(t.name)
        or SUBLIME_MCP_FORMATTERS.get(short)
        or TOOL_FORMATTERS.get(short)
    )

    detail = fmt(view, t) if fmt is not None else ""

    is_sublime_mcp = (
        t.name.startswith("mcp__sublime__")
        or t.name.startswith("sublime__")
        or short in SUBLIME_MCP_FORMATTERS
    )
    if not detail and is_sublime_mcp:
        detail = _mcp_call_args_fallback(t)
        if t.result and t.status == "done":
            detail += view._format_mcp_result(t.result)
    elif (
        not fmt
        and t.name.startswith("mcp__")
        and t.result
        and t.status == "done"
    ):
        detail = _mcp_call_args_fallback(t) + view._format_mcp_result(t.result)

    if tool.status == "background":
        detail += " (background)"
    return detail
