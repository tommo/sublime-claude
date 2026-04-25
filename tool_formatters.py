"""Per-tool detail formatter registry.

The single big if/elif chain that used to live in OutputView._format_tool_detail
is replaced by a name → formatter dict. Each formatter receives the tool input
dict (and optionally the OutputView for access to helpers like _find_line_number,
_format_bash_result, etc.) and returns the detail string starting with ": ".

Adding a tool = one entry in TOOL_FORMATTERS instead of editing a 90-line chain.
Unknown tool names fall through to the MCP-style result formatter (when applicable).
"""
from typing import Callable, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from .output import OutputView, ToolCall


def _bash(view: "OutputView", tool: "ToolCall") -> str:
    cmd = tool.tool_input.get("command", "")
    out = f": {cmd}"
    if tool.result and tool.status in ("done", "error"):
        out += view._format_bash_result(tool.result)
    return out


def _read(view: "OutputView", tool: "ToolCall") -> str:
    out = f": {tool.tool_input.get('file_path', '')}"
    if tool.result and tool.status == "done":
        out += view._format_read_result(tool.result)
    return out


def _edit(view: "OutputView", tool: "ToolCall") -> str:
    file_path = tool.tool_input.get("file_path", "")
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
    return f": {tool.tool_input.get('file_path', '')}"


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
    return f": {tool.tool_input.get('query', '')}"


def _webfetch(view: "OutputView", tool: "ToolCall") -> str:
    return f": {tool.tool_input.get('url', '')}"


def _task(view: "OutputView", tool: "ToolCall") -> str:
    sub = tool.tool_input.get("subagent_type", "")
    desc = tool.tool_input.get("description", "")
    return f": {sub}" + (f" - {desc}" if desc else "")


def _notebook_edit(view: "OutputView", tool: "ToolCall") -> str:
    return f": {tool.tool_input.get('notebook_path', '')}"


def _todo_write(view: "OutputView", tool: "ToolCall") -> str:
    todos = tool.tool_input.get("todos", [])
    count = len(todos) if isinstance(todos, list) else "?"
    return f": {count} task{'s' if count != 1 else ''}"


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


# Registry — one place to add a tool formatter.
# Required: a callable (view, tool) -> str returning the detail string.
TOOL_FORMATTERS: Dict[str, Callable] = {
    "Bash": _bash,
    "Read": _read,
    "Edit": _edit,
    "Write": _write,
    "Glob": _glob,
    "Grep": _grep,
    "WebSearch": _websearch,
    "WebFetch": _webfetch,
    "Task": _task,
    "NotebookEdit": _notebook_edit,
    "TodoWrite": _todo_write,
    "ask_user": _ask_user,
    "mcp__sublime__ask_user": _ask_user,
    "Skill": _skill,
    "EnterPlanMode": _enter_plan_mode,
    "ExitPlanMode": _exit_plan_mode,
}


def format_tool_detail(view: "OutputView", tool: "ToolCall") -> str:
    """Dispatch a tool to its formatter; return the full detail string.

    For unregistered tools we fall through to the MCP-style result formatter
    (when the tool name has the mcp__sublime__ prefix and a result is present).
    A trailing " (background)" suffix is added for tools in BACKGROUND state.
    """
    fmt = TOOL_FORMATTERS.get(tool.name)
    detail = fmt(view, tool) if fmt is not None else ""

    # Generic MCP fallback: any unregistered mcp__sublime__* tool with a done result
    if not fmt and tool.name.startswith("mcp__sublime__") and tool.result and tool.status == "done":
        detail += view._format_mcp_result(tool.result)

    if tool.status == "background":
        detail += " (background)"
    return detail
