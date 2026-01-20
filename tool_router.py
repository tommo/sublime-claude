"""Tool routing and command parsing - self-contained unit for MCP tool dispatch."""
import json
from typing import Dict, Callable, Any, Optional


class ToolRouter:
    """Routes MCP tool calls to appropriate handlers."""

    def __init__(self):
        self.handlers: Dict[str, Callable] = {}

    def register(self, tool_name: str, handler: Callable) -> None:
        """Register a handler for a tool name."""
        self.handlers[tool_name] = handler

    def route(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Route a tool call to its handler, returning Python code to execute."""
        handler = self.handlers.get(tool_name)
        if not handler:
            raise ValueError(f"Unknown tool: {tool_name}")
        return handler(args)

    def has_tool(self, tool_name: str) -> bool:
        """Check if a tool is registered."""
        return tool_name in self.handlers


# Pre-built handlers for common patterns
def simple_call_handler(func_name: str) -> Callable:
    """Create a handler that calls a function with no args."""
    def handler(args: Dict[str, Any]) -> str:
        return f"return {func_name}()"
    return handler


def kwargs_handler(func_name: str, *param_names: str, required: Optional[list] = None) -> Callable:
    """Create a handler that passes kwargs to a function.

    Args:
        func_name: Function name to call
        param_names: Parameter names to extract from args
        required: List of required parameter names
    """
    required = required or []

    def handler(args: Dict[str, Any]) -> str:
        # Build kwargs string
        kwargs_parts = []
        for param in param_names:
            value = args.get(param)
            if value is not None:
                kwargs_parts.append(f"{param}={value!r}")
            elif param in required:
                raise ValueError(f"Missing required parameter: {param}")

        kwargs_str = ", ".join(kwargs_parts)
        return f"return {func_name}({kwargs_str})"

    return handler


def custom_handler(func_name: str, builder: Callable[[Dict[str, Any]], str]) -> Callable:
    """Create a custom handler with full control over code generation.

    Args:
        func_name: Function name (for documentation)
        builder: Function that takes args dict and returns Python code string
    """
    return builder


# Default router with common Sublime tools
def create_sublime_router() -> ToolRouter:
    """Create a router pre-configured with Sublime Text tools."""
    router = ToolRouter()

    # Editor tools
    router.register("get_window_summary", simple_call_handler("get_window_summary"))
    router.register("list_tools", simple_call_handler("list_tools"))
    router.register("goto_symbol", kwargs_handler("goto_symbol", "query", required=["query"]))

    router.register("find_file", lambda args:
        f"return find_file({args.get('query', '')!r}, {args.get('pattern')!r}, {args.get('limit', 20)})")

    router.register("get_symbols", lambda args:
        f"return get_symbols({args.get('query', '')!r}, {args.get('file_path')!r}, {args.get('limit', 10)})")

    router.register("read_view", lambda args:
        f"return read_view({args.get('file_path')!r}, {args.get('view_name')!r}, "
        f"{args.get('head')}, {args.get('tail')}, {args.get('grep')!r}, {args.get('grep_i')!r})")

    # Session tools
    router.register("list_profiles", simple_call_handler("list_profiles"))
    router.register("list_sessions", simple_call_handler("list_sessions"))
    router.register("list_profile_docs", simple_call_handler("list_profile_docs"))

    router.register("spawn_session", lambda args:
        f"return spawn_session({args.get('prompt', '')!r}, {args.get('name')!r}, "
        f"{args.get('profile')!r}, {args.get('checkpoint')!r}, {args.get('fork_current', False)}, "
        f"{args.get('wait_for_completion', False)}, _caller_view_id={args.get('_caller_view_id')})")

    router.register("send_to_session", lambda args:
        f"return send_to_session({args.get('view_id')}, {args.get('prompt', '')!r})")

    router.register("read_session_output", lambda args:
        f"return read_session_output({args.get('view_id')}, {args.get('lines')})"
        if args.get('lines') else
        f"return read_session_output({args.get('view_id')})")

    router.register("read_profile_doc", lambda args:
        f"return read_profile_doc({args.get('path', '')!r})")

    # Terminal tools
    router.register("terminal_list", simple_call_handler("terminus_list"))

    router.register("terminal_run", lambda args: (
        cmd := args.get('command', ''),
        cmd_with_newline := cmd if cmd.endswith('\n') else cmd + '\n',
        f"return terminus_run({cmd_with_newline!r}, tag={args.get('tag')!r}, "
        f"wait={args.get('wait', 0)}, target_id={args.get('target_id')!r})"
    )[-1])  # Return last element of tuple

    router.register("terminal_read", lambda args:
        f"return terminus_read(tag={args.get('tag')!r}, lines={args.get('lines', 100)}, "
        f"target_id={args.get('target_id')!r})")

    router.register("terminal_close", lambda args:
        f"return terminus_close(tag={args.get('tag')!r}, target_id={args.get('target_id')!r})")

    # User interaction
    router.register("ask_user", lambda args:
        f"return ask_user({args.get('question', '')!r}, {args.get('options', [])!r})")

    # ─── Notification System (notalone2) ───────────────────────────
    # Uses notalone2 daemon for timers and subsession notifications
    router.register("register_notification", lambda args:
        f"return register_notification("
        f"notification_type={args.get('notification_type')!r}, "
        f"params={args.get('params', {})!r}, "
        f"wake_prompt={args.get('wake_prompt')!r})")

    router.register("unregister_notification", lambda args:
        f"return unregister_notification({args.get('notification_id')!r})")

    router.register("list_notifications", lambda args:
        f"return list_notifications()")

    router.register("discover_services", lambda args:
        f"return discover_services()")

    # Generic subscribe - works with any service from discover_services()
    # For channel services with HTTP endpoints, also POST to the endpoint
    router.register("subscribe", lambda args:
        f"return subscribe_to_service("
        f"notification_type={args.get('notification_type')!r}, "
        f"params={args.get('params', {})!r}, "
        f"wake_prompt={args.get('wake_prompt')!r})")

    # Convenience shortcuts for common notification types
    router.register("set_timer", lambda args:
        f"return register_notification("
        f"'timer', "
        f"{{'seconds': {args.get('seconds')}}}, "
        f"{args.get('wake_prompt')!r})")

    router.register("wait_for_subsession", lambda args:
        f"return register_notification("
        f"'subsession_complete', "
        f"{{'subsession_id': {args.get('subsession_id')!r}}}, "
        f"{args.get('wake_prompt')!r})")

    router.register("signal_complete", lambda args:
        f"return signal_subsession_complete(session_id={args.get('session_id')}, result_summary={args.get('result_summary')!r})")

    # Custom tools
    router.register("sublime_eval", lambda args: args.get("code", ""))
    router.register("sublime_tool", lambda args: args.get("name", ""))

    # ─── Chatroom ──────────────────────────────────────────────────────
    # Parse command string and route to appropriate chatroom function
    def chatroom_handler(args: Dict[str, Any]) -> str:
        import shlex
        cmd = args.get("cmd", "").strip()
        if not cmd:
            return "return {'error': 'Empty command'}"

        try:
            parts = shlex.split(cmd)
        except ValueError as e:
            return f"return {{'error': 'Parse error: {e}'}}"

        if not parts:
            return "return {'error': 'Empty command'}"

        action = parts[0].lower()

        if action == 'list':
            return "return chatroom_list()"
        elif action == 'rooms':
            return "return chatroom_rooms_for_session(view.id())"
        elif action == 'create':
            if len(parts) < 2:
                return "return {'error': 'Usage: create <room_id> [name]'}"
            room_id = parts[1]
            name = ' '.join(parts[2:]) if len(parts) > 2 else room_id
            return f"return chatroom_create(room_id={room_id!r}, name={name!r})"
        elif action == 'join':
            if len(parts) < 2:
                return "return {'error': 'Usage: join <room_id>'}"
            return f"return chatroom_join(view.id(), {parts[1]!r})"
        elif action == 'leave':
            if len(parts) < 2:
                return "return {'error': 'Usage: leave <room_id>'}"
            return f"return chatroom_leave(view.id(), {parts[1]!r})"
        elif action == 'post':
            if len(parts) < 3:
                return "return {'error': 'Usage: post <room_id> <message>'}"
            room_id = parts[1]
            content = ' '.join(parts[2:])
            return f"return chatroom_post(view.id(), {room_id!r}, {content!r})"
        elif action == 'history':
            if len(parts) < 2:
                return "return {'error': 'Usage: history <room_id> [limit]'}"
            room_id = parts[1]
            limit = int(parts[2]) if len(parts) > 2 else 50
            return f"return chatroom_history({room_id!r}, {limit})"
        else:
            return f"return {{'error': 'Unknown command: {action}. Try: list, rooms, create, join, leave, post, history'}}"

    router.register("chatroom", chatroom_handler)

    # ─── Order Table ───────────────────────────────────────────────────────
    def order_handler(args: Dict[str, Any]) -> str:
        import shlex
        cmd = args.get("cmd", "").strip()
        if not cmd:
            return "return {'error': 'Empty command'}"

        try:
            parts = shlex.split(cmd)
        except ValueError as e:
            return f"return {{'error': 'Parse error: {e}'}}"

        if not parts:
            return "return {'error': 'Empty command'}"

        action = parts[0].lower()

        if action == 'list':
            state = parts[1] if len(parts) > 1 else None
            return f"return order_table_cmd('list', state={state!r})"
        elif action == 'complete':
            if len(parts) < 2:
                return "return {'error': 'Usage: complete <order_id>'}"
            return f"return order_table_cmd('complete', order_id={parts[1]!r})"
        elif action == 'pending':
            return "return order_table_cmd('list', state='pending')"
        elif action == 'subscribe':
            wake_prompt = ' '.join(parts[1:]) if len(parts) > 1 else None
            return f"return order_table_cmd('subscribe', wake_prompt={wake_prompt!r})"
        else:
            return f"return {{'error': 'Unknown command: {action}. Try: list, pending, complete <id>, subscribe'}}"

    router.register("order", order_handler)

    return router


def parse_tool_call(method: str, params: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Parse an MCP tool call request.

    Returns:
        Tuple of (tool_name, arguments)
    """
    if method != "tools/call":
        raise ValueError(f"Invalid method: {method}")

    tool_name = params.get("name")
    if not tool_name:
        raise ValueError("Missing tool name")

    arguments = params.get("arguments", {})
    return tool_name, arguments
