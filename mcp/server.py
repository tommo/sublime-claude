#!/usr/bin/env python3
"""
MCP server for Sublime Text integration.
Provides sublime_eval tool to execute Python in Sublime's context.
"""
import json
import os
import socket
import sys
from typing import Any

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tool_router import create_sublime_router, parse_tool_call

SOCKET_PATH = "/tmp/sublime_claude_mcp.sock"
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES_GUIDE = os.path.join(PLUGIN_DIR, "docs", "profiles.md")

# Initialize tool router
_router = create_sublime_router()


def send_to_sublime(code: str = "", tool: str = None) -> dict:
    """Send eval request to Sublime plugin via Unix socket."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCKET_PATH)
        sock.sendall((json.dumps({"code": code, "tool": tool}) + "\n").encode())

        # Receive all data until newline (responses are newline-terminated)
        response_bytes = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response_bytes += chunk
            if b"\n" in chunk:
                break

        sock.close()
        response = response_bytes.decode()
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
                    "name": "get_window_summary",
                    "description": "Get editor state: open files (with dirty/size), active file with selection, project folders, layout.",
                    "inputSchema": {"type": "object", "properties": {}}
                },
                {
                    "name": "find_file",
                    "description": "Fuzzy find files by partial name. Scores: exact > starts with > contains > path contains > fuzzy.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Partial filename to search for"},
                            "pattern": {"type": "string", "description": "Optional glob pattern to filter first (e.g. '*.py')"},
                            "limit": {"type": "number", "description": "Max results (default 20)"}
                        },
                        "required": ["query"]
                    }
                },
                {
                    "name": "get_symbols",
                    "description": "Batch lookup symbols in project index. Accepts single symbol, comma-separated, or JSON array.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {"description": "Symbol name(s): string, comma-separated, or JSON array"},
                            "file_path": {"type": "string", "description": "Optional: limit to specific file"},
                            "limit": {"type": "number", "description": "Max results per symbol (default 10)"}
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
                {
                    "name": "read_view",
                    "description": "Read content from any view (file buffer or scratch) in Sublime Text. Specify either file_path for file buffers or view_name for scratch buffers. Supports head/tail/grep filtering.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "File path to read (absolute or relative to project)"},
                            "view_name": {"type": "string", "description": "View name for scratch buffers (e.g. output panels)"},
                            "head": {"type": "integer", "description": "Read first N lines"},
                            "tail": {"type": "integer", "description": "Read last N lines"},
                            "grep": {"type": "string", "description": "Filter lines matching regex pattern (case-sensitive)"},
                            "grep_i": {"type": "string", "description": "Filter lines matching regex pattern (case-insensitive)"}
                        }
                    }
                },
                # ─── Session Tools ────────────────────────────────────────
                {
                    "name": "list_profiles",
                    "description": f"List available session profiles and checkpoints. Profiles configure model/context for different use cases. Setup guide: {PROFILES_GUIDE}",
                    "inputSchema": {"type": "object", "properties": {}}
                },
                {
                    "name": "spawn_session",
                    "description": "Spawn a new Claude session with the given prompt. Returns view_id. Always waits for initialization. Use profile for specialized configurations (e.g. 1M context model with preloaded docs).",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "description": "Initial prompt for the new session"},
                            "name": {"type": "string", "description": "Optional: name for the session"},
                            "profile": {"type": "string", "description": "Optional: profile name from list_profiles"},
                            "checkpoint": {"type": "string", "description": "Optional: checkpoint name to fork from"},
                            "wait_for_completion": {"type": "boolean", "description": "Optional: wait for prompt to finish processing (default: false). Set true only for quick tasks."}
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
                    "description": "List all active Claude sessions (including spawned subsessions) with their view_ids",
                    "inputSchema": {"type": "object", "properties": {}}
                },
                {
                    "name": "read_session_output",
                    "description": "Read the conversation output from a Claude session. Use to check results from spawned subsessions.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "view_id": {"type": "integer", "description": "The view_id from spawn_session or list_sessions"},
                            "lines": {"type": "integer", "description": "Number of lines to read from end (default: all)"}
                        },
                        "required": ["view_id"]
                    }
                },
                {
                    "name": "list_profile_docs",
                    "description": "List documentation files available from your session's profile. These are project-specific docs configured in the profile's preload_docs patterns. Use read_profile_doc to read their contents.",
                    "inputSchema": {"type": "object", "properties": {}}
                },
                {
                    "name": "read_profile_doc",
                    "description": "Read a documentation file from your session's profile docset. Use list_profile_docs to see available files. Path is relative to project root.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative path to the doc file (from list_profile_docs)"}
                        },
                        "required": ["path"]
                    }
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
                },
                # ─── Terminal Tools ──────────────────────────────────────────
                # For long-running commands, use terminal_run instead of Bash.
                # You can monitor output with terminal_read while command runs.
                {
                    "name": "terminal_list",
                    "description": "List open terminal views in the editor. Shows tag and title for each terminal.",
                    "inputSchema": {"type": "object", "properties": {}}
                },
                {
                    "name": "terminal_run",
                    "description": """Run a command in a terminal. PREFER THIS over Bash for: long-running commands, interactive commands, or when you need to see live output.

IMPORTANT: Your session automatically has a dedicated terminal that is reused across calls. DO NOT specify tag or target_id unless you need to share with other sessions. Just call terminal_run(command="...") and it will use your existing terminal.""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "Command to run"},
                            "wait": {"type": "number", "description": "Wait N seconds then return output. Use small values (1-2s) for quick commands, larger for builds. 0=fire and forget, use terminal_read later."},
                            "target_id": {"type": "string", "description": "RARELY NEEDED: Only use to share terminal with other sessions. Omit to use your session's dedicated terminal."},
                            "tag": {"type": "string", "description": "RARELY NEEDED: Advanced use only. Omit to use your session's dedicated terminal."}
                        },
                        "required": ["command"]
                    }
                },
                {
                    "name": "terminal_read",
                    "description": "Read recent output from a terminal. Use to check command progress or results. Without parameters, reads from your session's dedicated terminal.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "target_id": {"type": "string", "description": "RARELY NEEDED: Only use if reading from a shared terminal. Omit to read your session's terminal."},
                            "tag": {"type": "string", "description": "RARELY NEEDED: Advanced use only. Omit to read your session's terminal."},
                            "lines": {"type": "integer", "description": "Lines to read from end (default 100)"}
                        }
                    }
                },
                {
                    "name": "terminal_close",
                    "description": "Close a terminal view. Without parameters, closes your session's dedicated terminal. RARELY NEEDED - terminals auto-close with session.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "target_id": {"type": "string", "description": "RARELY NEEDED: Only use if closing a shared terminal. Omit to close your session's terminal."},
                            "tag": {"type": "string", "description": "RARELY NEEDED: Advanced use only. Omit to close your session's terminal."}
                        }
                    }
                },
                # ─── User Interaction ────────────────────────────────────────
                {
                    "name": "ask_user",
                    "description": """Ask the user a question and wait for their response.
Shows a quick panel with options. Use for clarifying requirements, getting preferences, or confirming actions.
User can always type a custom response.""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "The question to ask"},
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of options to choose from (user can also type custom answer)"
                            }
                        },
                        "required": ["question"]
                    }
                },
                # ─── Alarm Tools ─────────────────────────────────────────────
                {
                    "name": "set_alarm",
                    "description": """Set an alarm to wake this session when an event occurs.

Instead of polling for subsession completion, sessions can sleep and wake when events fire.
The alarm fires by injecting the wake_prompt into this session as a new query.

Event types:
- subsession_complete: Wake when a subsession finishes (requires subsession_id)
- time_elapsed: Wake after N seconds (requires seconds)
- agent_complete: Same as subsession_complete (requires agent_id)

Example usage:
  result = spawn_session("Run tests", name="tester")
  set_alarm(
      event_type="subsession_complete",
      event_params={"subsession_id": str(result["view_id"])},
      wake_prompt="Tests done! Summarize results from tester session."
  )
  # This query ends, alarm monitors in background
  # When subsession completes, alarm fires and wakes this session""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "event_type": {
                                "type": "string",
                                "enum": ["subsession_complete", "time_elapsed", "agent_complete"],
                                "description": "Type of event to wait for"
                            },
                            "event_params": {
                                "type": "object",
                                "description": "Event-specific params: {subsession_id: str} | {seconds: int} | {agent_id: str}"
                            },
                            "wake_prompt": {
                                "type": "string",
                                "description": "Prompt to inject when alarm fires (wakes this session)"
                            },
                            "alarm_id": {
                                "type": "string",
                                "description": "Optional alarm identifier (auto-generated if omitted)"
                            }
                        },
                        "required": ["event_type", "event_params", "wake_prompt"]
                    }
                },
                {
                    "name": "cancel_alarm",
                    "description": "Cancel a pending alarm by its ID. Returns confirmation when cancelled.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "alarm_id": {
                                "type": "string",
                                "description": "Alarm identifier from set_alarm response"
                            }
                        },
                        "required": ["alarm_id"]
                    }
                }
            ]
        })

    elif method == "tools/call":
        try:
            tool_name, args = parse_tool_call(method, params)

            # Route the tool call to get executable code
            if tool_name == "sublime_eval":
                # Special case: code is passed directly
                code = args.get("code", "")
                result = send_to_sublime(code=code)
            elif tool_name == "sublime_tool":
                # Special case: execute saved tool
                name = args.get("name", "")
                result = send_to_sublime(tool=name)
            else:
                # Use router for all other tools
                code = _router.route(tool_name, args)
                result = send_to_sublime(code=code)

        except ValueError as e:
            return make_response(id, error=str(e))

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
