"""Built-in Sublime MCP tool formatters.

Originally part of tool_formatters.py. Imported and re-exported from there so
all existing imports stay intact.
"""
import os
from typing import Callable, Dict, TYPE_CHECKING

from .tool_formatters import _ask_user, _clip, _read_image

if TYPE_CHECKING:
    from .output import OutputView, ToolCall


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
