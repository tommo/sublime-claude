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
    # Surface session edit target (set from media preview) when call has no path
    if not extract_media_path(getattr(tool, "result", None), tool.tool_input):
        try:
            from . import claude_code
            v = getattr(view, "view", None)
            sess = claude_code.get_session_for_view(v) if v else None
            et = getattr(sess, "edit_target", None) if sess else None
            if et:
                out += f" · target {media_display_path(et) or et}"
        except Exception:
            pass
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


def _read_image_path(tool: "ToolCall") -> str:
    """Resolve on-disk path from flat or nested use_tool input / result text."""
    inp = tool.tool_input if isinstance(tool.tool_input, dict) else {}
    # Flat: {path} / {file_path}. Nested Grok UseTool: {tool_input: {path}}.
    nested = inp.get("tool_input") or inp.get("arguments") or inp.get("input")
    if isinstance(nested, dict):
        path = (
            nested.get("path")
            or nested.get("file_path")
            or nested.get("target_file")
            or ""
        )
    else:
        path = ""
    if not path:
        path = (
            inp.get("path")
            or inp.get("file_path")
            or inp.get("target_file")
            or ""
        )
    if not path and tool.result:
        # MCP text: "Image loaded (path=/abs/file.png, bytes=…)"
        m = re.search(r"path=([^\s,)]+)", str(tool.result))
        if m:
            path = m.group(1)
    return path or ""


def _read_image(view: "OutputView", tool: "ToolCall") -> str:
    path = _read_image_path(tool)
    label = media_display_path(path) if path else ""
    # Prefer a useful short path; fall back to full if basename is useless
    # (e.g. tmpXXXX.png) — still never emit a fake "✓ vision" mark.
    if path and (not label or label == os.path.basename(path)):
        # Keep last 2 path segments when not images/N.jpg style
        parts = path.replace("\\", "/").rstrip("/").split("/")
        if len(parts) >= 2:
            label = "/".join(parts[-2:])
        else:
            label = parts[-1] if parts else path
    out = f": {label}" if label else ""
    if tool.status == "error" and tool.result:
        out += f" ✗ {_clip(str(tool.result), 60)}"
    return out


def _scheduler_create(view: "OutputView", tool: "ToolCall") -> str:
    inp = tool.tool_input or {}
    interval = inp.get("interval") or inp.get("cron") or ""
    prompt = _clip(str(inp.get("prompt") or ""), 50)
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
    inp = tool.tool_input or {}
    tid = inp.get("id") or inp.get("task_id") or ""
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
    inp = tool.tool_input or {}
    path = (
        inp.get("file_path")
        or inp.get("path")
        or inp.get("target_file")
        or inp.get("filePath")
        or ""
    )
    content = (
        inp.get("content")
        or inp.get("contents")
        or inp.get("new_string")
        or inp.get("newText")
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


from .tool_formatters_sublime import (
    SUBLIME_MCP_FORMATTERS,
    _mcp_call_args_fallback,
    _mcp_short_name,
    _tool_input,
)


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
    """Grok use_tool / CallMcpTool → inner sublime tool name + args.

    Also peels nested `{tool_name, tool_input:{…}}` when the outer name was
    already rewritten to `sublime__*` / `mcp__*` (bridge renames title but
    leaves UseTool-shaped rawInput).
    """
    name = tool.name or ""
    inp = _tool_input(tool)
    nested = inp.get("tool_input") or inp.get("arguments") or inp.get("input")
    has_nested_args = isinstance(nested, dict)
    is_wrapper_name = name in ("use_tool", "CallMcpTool", "call_mcp_tool")
    is_use_tool_shape = bool(
        has_nested_args and (inp.get("tool_name") or inp.get("name")
                             or inp.get("variant") == "UseTool"
                             or inp.get("variant") == "use_tool"))
    if not is_wrapper_name and not is_use_tool_shape:
        return tool
    inner_name = inp.get("tool_name") or inp.get("name") or name
    if has_nested_args:
        inner_input = nested
    elif any(k in inp for k in (
            "query", "path", "cmd", "command", "prompt", "file_path",
            "code", "view_id", "seconds", "target_file")):
        # tool_input may be the args themselves without nesting
        inner_input = {k: v for k, v in inp.items()
                       if k not in ("tool_name", "name", "server", "variant")}
    else:
        inner_input = inp
    short = _mcp_short_name(str(inner_name))
    display = f"mcp__sublime__{short}" if short in SUBLIME_MCP_FORMATTERS else str(inner_name)
    try:
        from .output_models import ToolCall as _ToolCall
        ctor = _ToolCall
    except Exception:
        ctor = type(tool)
    return ctor(
        name=display,
        tool_input=inner_input if isinstance(inner_input, dict) else {},
        status=getattr(tool, "status", "pending"),
        result=getattr(tool, "result", None),
        id=getattr(tool, "id", None),
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
