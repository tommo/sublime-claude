#!/usr/bin/env python3
"""
MCP server for Sublime Text integration.
Provides sublime_eval tool to execute Python in Sublime's context.
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
from typing import Any, Optional, Tuple

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tool_router import create_sublime_router, parse_tool_call

SOCKET_PATH = "/tmp/sublime_claude_mcp.sock"
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES_GUIDE = os.path.join(PLUGIN_DIR, "docs", "profiles.md")

# Vision tool: agent-captured screenshots / UI renders as MCP image blocks.
# Grok's built-in read_file → fs/read_text_file rejects binary ("Cannot read
# binary file"); this tool returns real image content for vision.
_IMAGE_EXTS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".tif", ".tiff", ".heic", ".heif", ".ico",
)
_READ_IMAGE_MAX_BYTES = 4 * 1024 * 1024  # raw payload after optional shrink
_READ_IMAGE_MAX_EDGE = 1600  # long edge for resize

# Parse --view-id from command line args (passed by bridge)
CALLER_VIEW_ID = None
for arg in sys.argv[1:]:
    if arg.startswith("--view-id="):
        try:
            CALLER_VIEW_ID = int(arg.split("=", 1)[1])
        except ValueError:
            pass

# Initialize tool router
_router = create_sublime_router()


def send_to_sublime(code: str = "", tool: str = None, view_id: int = None) -> dict:
    """Send eval request to Sublime plugin via Unix socket.

    Args:
        code: Python code to execute
        tool: Named tool to execute
        view_id: Optional view_id to identify the calling session
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCKET_PATH)
        msg = {"code": code, "tool": tool}
        if view_id is not None:
            msg["view_id"] = view_id
        sock.sendall((json.dumps(msg) + "\n").encode())

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


def _image_mime(path: str, head: bytes = b"") -> str:
    if len(head) >= 8 and head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(head) >= 3 and head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(head) >= 6 and head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    low = path.lower()
    for ext, mime in (
        (".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
        (".gif", "image/gif"), (".webp", "image/webp"), (".bmp", "image/bmp"),
        (".tif", "image/tiff"), (".tiff", "image/tiff"),
        (".heic", "image/heic"), (".heif", "image/heif"), (".ico", "image/x-icon"),
    ):
        if low.endswith(ext):
            return mime
    return "image/png"


def _png_size(raw: bytes) -> Tuple[int, int]:
    if len(raw) >= 24 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", raw[16:24])
    return 0, 0


def _shrink_image(path: str, max_edge: int, max_bytes: int) -> Tuple[bytes, str, str]:
    """Return (bytes, mime, note). Prefer sips/PIL shrink when large."""
    with open(path, "rb") as f:
        raw = f.read()
    mime = _image_mime(path, raw[:64])
    w, h = _png_size(raw)
    note_bits = [f"path={path}", f"bytes={len(raw)}"]
    if w and h:
        note_bits.append(f"dim={w}x{h}")

    need_resize = (w and h and max(w, h) > max_edge) or len(raw) > max_bytes
    if not need_resize:
        if len(raw) > max_bytes:
            raise ValueError(
                f"image {path!r} is {len(raw)} bytes (max {max_bytes}); "
                f"resize/compress the screenshot and retry")
        return raw, mime, ", ".join(note_bits)

    # macOS sips → JPEG (good for screenshots, smaller than PNG)
    try:
        with tempfile.TemporaryDirectory(prefix="sc-read-image-") as td:
            out = os.path.join(td, "out.jpg")
            cmd = ["sips", "-Z", str(max_edge), "-s", "format", "jpeg",
                   path, "--out", out]
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if r.returncode == 0 and os.path.isfile(out):
                shrunk = open(out, "rb").read()
                if shrunk and len(shrunk) <= max_bytes:
                    note_bits.append(f"shrunk_via=sips edge≤{max_edge}")
                    note_bits.append(f"out_bytes={len(shrunk)}")
                    return shrunk, "image/jpeg", ", ".join(note_bits)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Pillow fallback
    try:
        from PIL import Image  # type: ignore
        import io
        im = Image.open(path)
        im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
        im.thumbnail((max_edge, max_edge))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85, optimize=True)
        shrunk = buf.getvalue()
        if shrunk and len(shrunk) <= max_bytes:
            note_bits.append(f"shrunk_via=pillow edge≤{max_edge}")
            note_bits.append(f"out_bytes={len(shrunk)}")
            return shrunk, "image/jpeg", ", ".join(note_bits)
    except Exception:
        pass

    if len(raw) > max_bytes:
        raise ValueError(
            f"image {path!r} is {len(raw)} bytes after failed shrink "
            f"(max {max_bytes})")
    return raw, mime, ", ".join(note_bits)


def handle_read_image(args: dict) -> dict:
    """Read an image file and return MCP image content for vision."""
    path = (args.get("path") or args.get("file_path") or args.get("target_file")
            or "").strip()
    if not path:
        return {
            "content": [{"type": "text", "text": "Error: path is required"}],
            "isError": True,
        }
    if not os.path.isabs(path):
        # resolve relative to CWD (agent session cwd)
        path = os.path.abspath(path)
    if not os.path.isfile(path):
        return {
            "content": [{"type": "text",
                         "text": f"Error: file not found: {path}"}],
            "isError": True,
        }

    low = path.lower()
    with open(path, "rb") as f:
        head = f.read(16)
    by_ext = any(low.endswith(e) for e in _IMAGE_EXTS)
    by_magic = (
        head[:8] == b"\x89PNG\r\n\x1a\n"
        or head[:3] == b"\xff\xd8\xff"
        or head[:6] in (b"GIF87a", b"GIF89a")
        or (len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP")
        or head[:2] == b"BM"
    )
    if not by_ext and not by_magic:
        return {
            "content": [{"type": "text",
                         "text": f"Error: not an image file: {path}"}],
            "isError": True,
        }

    try:
        max_edge = int(args.get("max_edge") or _READ_IMAGE_MAX_EDGE)
        max_bytes = int(args.get("max_bytes") or _READ_IMAGE_MAX_BYTES)
        max_edge = max(256, min(max_edge, 4096))
        max_bytes = max(64 * 1024, min(max_bytes, 8 * 1024 * 1024))
        raw, mime, note = _shrink_image(path, max_edge, max_bytes)
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"Error: {e}"}],
            "isError": True,
        }

    b64 = base64.b64encode(raw).decode("ascii")
    return {
        "content": [
            {"type": "text", "text": f"Image loaded ({note}, mime={mime})"},
            {"type": "image", "mimeType": mime, "data": b64},
        ]
    }


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
                    "description": "Fast project-wide symbol search — find classes, functions, methods, variables by exact or partial name. Use this FIRST to locate definitions before reading files. Accepts single symbol, comma-separated list, or JSON array for batch lookup. Uses Sublime's index first, then a lightweight partial-match project scan when needed.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {"description": "Symbol name(s) or partial name(s) to find: string, comma-separated, or JSON array. Examples: 'MyClass', 'handle_req', 'handle_request,process_event', '[\"foo\", \"bar\"]'"},
                            "file_path": {"type": "string", "description": "Optional: limit search to specific file"},
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
                {
                    "name": "read_image",
                    "description": (
                        "VISION: load a local image file (PNG/JPEG/WebP/GIF "
                        "screenshots, UI renders, app captures) so the model can "
                        "see pixels. Grok: call via use_tool tool_name="
                        "\"sublime__read_image\" tool_input={\"path\":\"/abs/file.png\"}; "
                        "if unknown, search_tool query=\"read_image\" first. "
                        "Never use read_file on images (ACP text FS → "
                        "'Cannot read binary file')."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute path to the image file"
                            },
                            "file_path": {
                                "type": "string",
                                "description": "Alias for path"
                            },
                            "max_edge": {
                                "type": "number",
                                "description": (
                                    "Optional max long-edge pixels before shrink "
                                    f"(default {_READ_IMAGE_MAX_EDGE})"
                                )
                            }
                        },
                        "required": ["path"]
                    }
                },
                # ─── Session Tools ────────────────────────────────────────
                {
                    "name": "list_backends",
                    "description": "List backends available for spawn_session's `backend` argument — built-ins (claude, codex, copilot, pi, dsr, grok, grok_cc) plus any custom Anthropic-compatible providers, each with live availability (auth/CLI resolved), kind, bridge family, and models. Call this before spawn_session to pick a valid backend instead of guessing. Also reports the default backend and the fork-family rule (fork_current only works within the same bridge family).",
                    "inputSchema": {"type": "object", "properties": {}}
                },
                {
                    "name": "list_profiles",
                    "description": f"List available session profiles and checkpoints. Profiles configure model/context for different use cases. Setup guide: {PROFILES_GUIDE}",
                    "inputSchema": {"type": "object", "properties": {}}
                },
                {
                    "name": "list_personas",
                    "description": "List available personas from the persona server. Shows alias, tags, lock status, and model.",
                    "inputSchema": {"type": "object", "properties": {}}
                },
                {
                    "name": "spawn_session",
                    "description": "Spawn a new Claude subsession. Returns view_id. Use fork_current=true to share your full conversation context with the subsession — it sees everything you've learned so far (files read, decisions made, codebase understanding) without re-discovering it. This is the preferred way to parallelize work. Use profile for specialized configurations (e.g. 1M context model with preloaded docs).",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "description": "Initial prompt for the new session"},
                            "name": {"type": "string", "description": "Optional: name for the session"},
                            "profile": {"type": "string", "description": "Optional: profile name from list_profiles"},
                            "checkpoint": {"type": "string", "description": "Optional: checkpoint name to fork from"},
                            "persona_id": {"type": "integer", "description": "Optional: persona ID from list_personas to acquire and use"},
                            "backend": {"type": "string", "description": "Optional: backend to use — a built-in (claude, codex, copilot, pi, dsr, grok, grok_cc) or a custom Anthropic-compatible provider from settings.custom_providers (default: claude)"},
                            "fork_current": {"type": "boolean", "description": "Optional: fork from the current session (preserves conversation history). Default: false"},
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
                    "description": "List spawned subsessions with their view_ids and status.",
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
                # ─── LSP Tools ───────────────────────────────────────────
                {
                    "name": "lsp",
                    "description": """Language server integration - get type info, definitions, references, diagnostics from running LSP servers. Commands:
- hover <file> <line> <col>         → type info and docs at position
- definition <file> <line> <col>    → jump to symbol definition
- references <file> <line> <col>    → find all usages of symbol
- symbols <file>                    → list all symbols in file
- workspace_symbols <query>         → search symbols across project
- diagnostics [file]                → errors/warnings (default: active file)

Line and col are 0-based. File can be a path or view name.

Examples:
  lsp("hover /path/to/file.py 42 10")
  lsp("definition /path/to/file.py 42 10")
  lsp("references /path/to/file.py 42 10")
  lsp("symbols /path/to/file.py")
  lsp("workspace_symbols MyClass")
  lsp("diagnostics")""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "cmd": {"type": "string", "description": "Command string"}
                        },
                        "required": ["cmd"]
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
                {
                    "name": "quick_done",
                    "description": """End the current Quick Agent slot when the short task is finished.
Only valid inside a Quick Agent (⚡) session — not a full Claude/Grok sheet.
Call with status='completed' and a one-line message when done, or status='blocked' if stuck.
This stops the slot's bridge so the host can free the slot.""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "description": "completed (default) or blocked",
                                "enum": ["completed", "blocked"]
                            },
                            "message": {
                                "type": "string",
                                "description": "One-line summary or blocked reason"
                            }
                        }
                    }
                },
                # ─── Terminal Tools ──────────────────────────────────────────
                # For interactive/long-running processes (dev servers, REPLs, watch modes).
                {
                    "name": "terminal_list",
                    "description": "List open terminal views in the editor. Shows tag and title for each terminal.",
                    "inputSchema": {"type": "object", "properties": {}}
                },
                {
                    "name": "terminal_run",
                    "description": """Run a command in a persistent terminal. Only use for interactive CLI sessions (e.g. dev servers, REPLs, watch modes, build monitors) where you need a live terminal. For normal one-shot commands, use Bash instead.

Your session has a dedicated terminal reused across calls. Use index to target a specific terminal the user has open (visible as #N in the tab title).""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "Command to run"},
                            "wait": {"type": "number", "description": "Max seconds to wait for command to finish (default 30). Blocks until output is available. Set 0 for fire-and-forget (use terminal_read later).", "default": 30},
                            "index": {"type": "integer", "description": "Target a specific user terminal by its tab number (#N). Omit to use your session's dedicated terminal."},
                            "target_id": {"type": "string", "description": "RARELY NEEDED: Only use to share terminal with other sessions."}
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
                            "index": {"type": "integer", "description": "Target a specific user terminal by its tab number (#N). Omit to read your session's terminal."},
                            "target_id": {"type": "string", "description": "RARELY NEEDED: Only use if reading from a shared terminal."},
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
                            "index": {"type": "integer", "description": "Target a specific user terminal by its tab number (#N). Omit to close your session's terminal."},
                            "target_id": {"type": "string", "description": "RARELY NEEDED: Only use if closing a shared terminal."}
                        }
                    }
                },
                # ─── User Interaction ────────────────────────────────────────
                # ask_user removed — Claude's native AskUserQuestion shows inline in session view
                # ─── Notification Tools (notalone2) ──────────────────────────
                # Timer and subsession notifications via notalone2 daemon
                # Session ID is embedded in bridge - no need to specify
                {
                    "name": "set_timer",
                    "description": """Set a timer to wake this session after specified seconds.

Example:
  set_timer(seconds=300, wake_prompt="⏰ 5 minutes elapsed!")

Returns notification_id for cancellation via unregister_notification().""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "seconds": {
                                "type": "integer",
                                "description": "Seconds until notification fires"
                            },
                            "wake_prompt": {
                                "type": "string",
                                "description": "Prompt to inject when timer fires"
                            }
                        },
                        "required": ["seconds", "wake_prompt"]
                    }
                },
                {
                    "name": "signal_complete",
                    "description": """Signal that this subsession has completed.

ONLY for subsessions (spawned via spawn_session). Notifies parent session.
The session_id is your view_id (shown in spawn result).

Example:
  signal_complete(session_id=12345, result_summary="Task done. See output above.")""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "session_id": {
                                "type": "integer",
                                "description": "Your session's view_id (from spawn result)"
                            },
                            "result_summary": {
                                "type": "string",
                                "description": "Brief summary of what was accomplished"
                            }
                        },
                        "required": ["session_id"]
                    }
                },
                {
                    "name": "wait_for_subsession",
                    "description": """Wait for a subsession to complete.

Example:
  result = spawn_session(prompt="Design solution", name="architect")
  wait_for_subsession(
      subsession_id=result['subsession_id'],
      wake_prompt="Architect done! Review the design."
  )

Returns notification_id for cancellation.""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "subsession_id": {
                                "type": "string",
                                "description": "Subsession ID from spawn_session() result"
                            },
                            "wake_prompt": {
                                "type": "string",
                                "description": "Prompt to inject when subsession completes"
                            }
                        },
                        "required": ["subsession_id", "wake_prompt"]
                    }
                },
                {
                    "name": "list_notifications",
                    "description": """List all active notifications for this session.

Shows timers, subsession waits, and service subscriptions.
Lists notifications registered with notalone2 daemon.""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {}
                    }
                },
                {
                    "name": "discover_services",
                    "description": """Discover available notification services from notalone2 daemon.

Returns registered services with their notification types and required params.
Use this first, then subscribe() to register for notifications.

Built-in types: timer, subsession
Service types vary by connected services (e.g. jar-kanban.card_update)

Example response:
  {
    "builtin": ["timer", "subsession"],
    "services": {
      "jar-kanban": {
        "card_update": {"params": ["card_id"]},
        "project_watch": {"params": ["project_slug"]}
      }
    }
  }""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {}
                    }
                },
                {
                    "name": "unregister_notification",
                    "description": """Cancel a notification by its notification_id.

Example:
  unregister_notification(notification_id="ntf-abc123")""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "notification_id": {
                                "type": "string",
                                "description": "Notification ID to cancel"
                            }
                        },
                        "required": ["notification_id"]
                    }
                },
                {
                    "name": "subscribe",
                    "description": """Subscribe to a notification service discovered via discover_services().

Example:
  services = discover_services()
  subscribe(
      notification_type="jar-kanban.card_update",
      params={"card_id": "abc123"},
      wake_prompt="Card updated!"
  )""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "notification_type": {
                                "type": "string",
                                "description": "Service type from discover_services()"
                            },
                            "params": {
                                "type": "object",
                                "description": "Service-specific parameters"
                            },
                            "wake_prompt": {
                                "type": "string",
                                "description": "Prompt to inject when notification fires"
                            }
                        },
                        "required": ["notification_type", "params", "wake_prompt"]
                    }
                },
                # ─── Chatroom Tools ────────────────────────────────────────
                {
                    "name": "chatroom",
                    "description": """Multi-agent chat rooms. Commands:
- list                    → list all rooms
- rooms                   → list rooms I've joined
- create <id> [name]      → create a room
- join <room_id>          → join a room
- leave <room_id>         → leave a room
- post <room_id> <msg>    → post a message (other agents wake automatically)
- history <room_id>       → get chat history

Messages posted to a room automatically wake other agent participants.

Examples:
  chatroom("list")
  chatroom("create dev-chat Development Chat")
  chatroom("join dev-chat")
  chatroom("post dev-chat Hello!")""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "cmd": {
                                "type": "string",
                                "description": "Command string, e.g. 'post dev-chat Hello!'"
                            }
                        },
                        "required": ["cmd"]
                    }
                },
                # ─── Garage Session Search ──────────────────────────────────
                {
                    "name": "garage_search",
                    "description": """Search indexed Claude sessions using semantic search.
Returns session IDs that can be used with spawn_session to fork/resume.

Requires garage CLI to be installed. Sessions are indexed from ~/.claude/projects/.

Example response:
  [{"session_id": "f400b570-...", "short_id": "f400b570", "score": 0.68, "project": "sublime-claude", "turns": 334, "summary": "Add slash command support..."}]""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search query (semantic)"
                            },
                            "k": {
                                "type": "integer",
                                "description": "Number of results (default: 5)"
                            }
                        },
                        "required": ["query"]
                    }
                },
                # ─── Order Table ─────────────────────────────────────────────
                {
                    "name": "order",
                    "description": """Order table - human→agent task assignments. Commands:
- list [state]         → list orders (optional: pending/done)
- pending              → list pending orders only
- claim <order_id>     → claim order (prevents other agents working on it)
- release <order_id>   → release claimed order
- complete <order_id>  → mark order as done (auto-releases claim)
- subscribe [prompt]   → subscribe to new order notifications

Claims auto-expire after 10 minutes or when agent session ends.
Orders are created by user via Cmd+Shift+O at cursor position.

Examples:
  order("pending")
  order("claim order_1")
  order("complete order_1")
  order("subscribe New order: {context[prompt]}")""",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "cmd": {
                                "type": "string",
                                "description": "Command string, e.g. 'pending' or 'complete order_1'"
                            }
                        },
                        "required": ["cmd"]
                    }
                }
            ]
        })

    elif method == "tools/call":
        try:
            tool_name, args = parse_tool_call(method, params)

            # Inject caller view_id for spawn_session (so subsession knows parent)
            if tool_name == "spawn_session" and CALLER_VIEW_ID and "_caller_view_id" not in args:
                args["_caller_view_id"] = CALLER_VIEW_ID

            # Local tools (no Sublime socket)
            if tool_name == "read_image":
                return make_response(id, handle_read_image(args))

            # Route the tool call to get executable code
            # Pass CALLER_VIEW_ID with every request so Sublime knows the session context
            if tool_name == "sublime_eval":
                # Special case: code is passed directly
                code = args.get("code", "")
                result = send_to_sublime(code=code, view_id=CALLER_VIEW_ID)
            elif tool_name == "sublime_tool":
                # Special case: execute saved tool
                name = args.get("name", "")
                result = send_to_sublime(tool=name, view_id=CALLER_VIEW_ID)
            else:
                # Use router for all other tools
                code = _router.route(tool_name, args)
                result = send_to_sublime(code=code, view_id=CALLER_VIEW_ID)

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
