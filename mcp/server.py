#!/usr/bin/env python3
"""
MCP server for Sublime Text integration.
Provides sublime_eval tool to execute Python in Sublime's context.
"""
import json
import socket
import sys
from typing import Any

SOCKET_PATH = "/tmp/sublime_claude_mcp.sock"


def send_to_sublime(code: str = "", tool: str = None) -> dict:
    """Send eval request to Sublime plugin via Unix socket."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCKET_PATH)
        sock.sendall((json.dumps({"code": code, "tool": tool}) + "\n").encode())
        response = sock.recv(65536).decode()
        sock.close()
        return json.loads(response)
    except FileNotFoundError:
        return {"error": "Sublime Text not connected. Make sure the plugin is running."}
    except Exception as e:
        return {"error": str(e)}


def make_response(id: Any, result: Any = None, error: Any = None) -> dict:
    """Create JSON-RPC response."""
    resp = {"jsonrpc": "2.0", "id": id}
    if error:
        resp["error"] = {"code": -32000, "message": str(error)}
    else:
        resp["result"] = result
    return resp


def handle_request(request: dict) -> dict:
    """Handle incoming MCP request."""
    id = request.get("id")
    method = request.get("method", "")
    params = request.get("params", {})

    if method == "initialize":
        return make_response(id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sublime-mcp", "version": "0.1.0"}
        })

    elif method == "notifications/initialized":
        return None

    elif method == "tools/list":
        return make_response(id, {
            "tools": [
                # ─── Editor Tools ─────────────────────────────────────────
                {
                    "name": "get_open_files",
                    "description": "Get list of open file paths in Sublime Text",
                    "inputSchema": {"type": "object", "properties": {}}
                },
                {
                    "name": "get_symbols",
                    "description": "Search project symbol index",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Symbol name to search"},
                            "file_path": {"type": "string", "description": "Optional: limit to specific file"}
                        },
                        "required": ["query"]
                    }
                },
                {
                    "name": "goto_symbol",
                    "description": "Navigate to a symbol definition in Sublime Text",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Symbol name to navigate to"}
                        },
                        "required": ["query"]
                    }
                },
                # ─── Blackboard Tools ─────────────────────────────────────
                # Shared scratchpad for persistent artifacts across sessions.
                # Common patterns:
                #   bb_write("plan", {steps: [...], status: "in_progress"})
                #   bb_write("walkthrough", "## Progress\n- Done X\n- Working on Y")
                #   bb_write("decisions", [{what: "...", why: "..."}])
                #   bb_write("commands", {build: "pil load:app", test: "nim check ..."})
                {
                    "name": "bb_write",
                    "description": """Write to shared blackboard. Persists across sessions and context loss.

Use for:
- plan: Implementation steps, architecture decisions
- walkthrough: Progress report for user (markdown)
- decisions: Key choices made and rationale
- commands: Project-specific commands that work
- context: Important details that might be forgotten""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "Key (e.g. 'plan', 'walkthrough', 'commands')"},
                            "value": {"description": "Value (string, object, or array)"}
                        },
                        "required": ["key", "value"]
                    }
                },
                {
                    "name": "bb_read",
                    "description": "Read from blackboard. Use after context loss to restore important state.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "Key to read"}
                        },
                        "required": ["key"]
                    }
                },
                {
                    "name": "bb_list",
                    "description": "List all blackboard keys. Check this after context loss to see what's saved.",
                    "inputSchema": {"type": "object", "properties": {}}
                },
                {
                    "name": "bb_delete",
                    "description": "Delete a blackboard key when no longer needed.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "Key to delete"}
                        },
                        "required": ["key"]
                    }
                },
                # ─── Session Tools ────────────────────────────────────────
                {
                    "name": "spawn_session",
                    "description": "Spawn a new Claude session with the given prompt. Returns view_id.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "description": "Initial prompt for the new session"},
                            "name": {"type": "string", "description": "Optional: name for the session"}
                        },
                        "required": ["prompt"]
                    }
                },
                {
                    "name": "send_to_session",
                    "description": "Send a message to an existing session by view_id. Use this to continue a spawned session.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "view_id": {"type": "integer", "description": "The view_id from spawn_session or list_sessions"},
                            "prompt": {"type": "string", "description": "Message to send"}
                        },
                        "required": ["view_id", "prompt"]
                    }
                },
                {
                    "name": "list_sessions",
                    "description": "List all active Claude sessions in the current window with their view_ids",
                    "inputSchema": {"type": "object", "properties": {}}
                },
                # ─── Custom Tools ─────────────────────────────────────────
                {
                    "name": "sublime_eval",
                    "description": """Execute custom Python code in Sublime Text's context.

Available modules: sublime, sublime_plugin
Use 'return <value>' to return results.

For simple operations, prefer the dedicated tools above.""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "description": "Python code to execute"}
                        },
                        "required": ["code"]
                    }
                },
                {
                    "name": "sublime_tool",
                    "description": "Run a saved tool from .claude/sublime_tools/<name>.py",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Tool name (without .py)"}
                        },
                        "required": ["name"]
                    }
                },
                {
                    "name": "list_tools",
                    "description": "List saved tools in .claude/sublime_tools/ with descriptions",
                    "inputSchema": {"type": "object", "properties": {}}
                }
            ]
        })

    elif method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments", {})

        # Route to appropriate handler
        if tool_name == "sublime_eval":
            code = args.get("code", "")
            result = send_to_sublime(code=code)
        elif tool_name == "sublime_tool":
            name = args.get("name", "")
            result = send_to_sublime(tool=name)
        elif tool_name == "list_tools":
            result = send_to_sublime(code="return list_tools()")
        # Editor tools
        elif tool_name == "get_open_files":
            result = send_to_sublime(code="return get_open_files()")
        elif tool_name == "get_symbols":
            query = args.get("query", "")
            file_path = args.get("file_path")
            if file_path:
                result = send_to_sublime(code=f"return get_symbols({query!r}, {file_path!r})")
            else:
                result = send_to_sublime(code=f"return get_symbols({query!r})")
        elif tool_name == "goto_symbol":
            query = args.get("query", "")
            result = send_to_sublime(code=f"return goto_symbol({query!r})")
        # Blackboard tools
        elif tool_name == "bb_write":
            key = args.get("key", "")
            value = args.get("value")
            result = send_to_sublime(code=f"return bb_write({key!r}, {json.dumps(value)})")
        elif tool_name == "bb_read":
            key = args.get("key", "")
            result = send_to_sublime(code=f"return bb_read({key!r})")
        elif tool_name == "bb_list":
            result = send_to_sublime(code="return bb_list()")
        elif tool_name == "bb_delete":
            key = args.get("key", "")
            result = send_to_sublime(code=f"return bb_delete({key!r})")
        # Session tools
        elif tool_name == "spawn_session":
            prompt = args.get("prompt", "")
            name = args.get("name")
            if name:
                result = send_to_sublime(code=f"return spawn_session({prompt!r}, {name!r})")
            else:
                result = send_to_sublime(code=f"return spawn_session({prompt!r})")
        elif tool_name == "send_to_session":
            view_id = args.get("view_id")
            prompt = args.get("prompt", "")
            result = send_to_sublime(code=f"return send_to_session({view_id}, {prompt!r})")
        elif tool_name == "list_sessions":
            result = send_to_sublime(code="return list_sessions()")
        else:
            return make_response(id, error=f"Unknown tool: {tool_name}")

        if result.get("error"):
            return make_response(id, {
                "content": [{"type": "text", "text": f"Error: {result['error']}"}],
                "isError": True
            })
        else:
            output = result.get("result")
            if output is None:
                text = "(no return value)"
            elif isinstance(output, str):
                text = output
            else:
                text = json.dumps(output, indent=2)
            return make_response(id, {
                "content": [{"type": "text", "text": text}]
            })

    else:
        # Ignore unknown methods
        return None


def main():
    """Main loop - read JSON-RPC from stdin, write to stdout."""
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = handle_request(request)
            if response:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError as e:
            sys.stderr.write(f"JSON parse error: {e}\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"Error: {e}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    main()
