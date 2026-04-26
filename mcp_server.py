"""MCP socket server for Sublime Text integration."""
import json
import os
import socket
import threading
import time

import sublime
import sublime_plugin

from .settings import load_profiles_and_checkpoints
from .constants import MCP_SOCKET_PATH, USER_PROFILES_DIR, PROFILES_FILE

SOCKET_PATH = MCP_SOCKET_PATH
USER_PROFILES_PATH = str(USER_PROFILES_DIR / PROFILES_FILE)

_server = None


def _get_project_profiles_path() -> str:
    """Get project-level profiles path."""
    window = sublime.active_window()
    if window and window.folders():
        return os.path.join(window.folders()[0], ".claude", "profiles.json")
    return ""


def _save_checkpoint(name: str, session_id: str, description: str, to_project: bool = True) -> bool:
    """Save a checkpoint to profiles.json."""
    if to_project:
        path = _get_project_profiles_path()
        if not path:
            return False
    else:
        path = USER_PROFILES_PATH

    # Ensure directory exists
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Load existing
    data = {"profiles": {}, "checkpoints": {}}
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except:
            pass

    # Add checkpoint
    if "checkpoints" not in data:
        data["checkpoints"] = {}
    data["checkpoints"][name] = {
        "session_id": session_id,
        "description": description,
    }

    # Save
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"[Claude MCP] Error saving checkpoint: {e}")
        return False


def start():
    """Start the MCP socket server."""
    global _server
    if _server:
        return
    _server = MCPSocketServer()
    _server.start()


def stop():
    """Stop the MCP socket server."""
    global _server
    if _server:
        _server.stop()
        _server = None


class MCPSocketServer:
    """Unix socket server for MCP eval requests."""

    def __init__(self):
        self.socket = None
        self.running = False
        self.thread = None

    def start(self):
        """Start the server in a background thread."""
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        """Stop the server."""
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
        try:
            os.unlink(SOCKET_PATH)
        except:
            pass

    def _run(self):
        """Server main loop."""
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass

        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(SOCKET_PATH)
        self.socket.listen(5)
        self.socket.settimeout(1.0)

        print(f"[Claude MCP] Listening on {SOCKET_PATH}")

        while self.running:
            try:
                conn, _ = self.socket.accept()
                self._handle_connection(conn)
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"[Claude MCP] Error: {e}")

    def _handle_connection(self, conn: socket.socket):
        """Handle a single connection."""
        try:
            data = conn.recv(65536).decode()
            if not data:
                return

            request = json.loads(data.strip())
            code = request.get("code", "")
            tool = request.get("tool")
            view_id = request.get("view_id")  # Caller's view_id from mcp/server.py

            result = {"result": None, "error": None}
            done = threading.Event()

            def do_eval():
                try:
                    result["result"] = self._eval(code, tool, caller_view_id=view_id)
                    done.set()
                except Exception as e:
                    result["error"] = str(e)
                    done.set()

            sublime.set_timeout(do_eval, 0)
            timeout = 30
            done.wait(timeout=timeout)

            # Handle spawn_session wait - poll for initialization
            eval_result = result.get("result")
            if isinstance(eval_result, dict) and eval_result.get("_wait_for_init"):
                session = eval_result.pop("_session")
                prompt = eval_result.pop("_prompt")
                wait_for_completion = eval_result.pop("_wait_for_completion", False)
                eval_result.pop("_wait_for_init")

                # Wait for initialization (in this background thread, not main thread)
                import time
                max_wait = 30
                start = time.time()
                while not session.initialized and time.time() - start < max_wait:
                    time.sleep(0.1)

                if not session.initialized:
                    eval_result["error"] = "Session failed to initialize within 30 seconds"
                else:
                    # Send the prompt from main thread
                    def send_prompt():
                        session.query(prompt)
                    sublime.set_timeout(send_prompt, 0)

                    # Optionally wait for completion
                    if wait_for_completion:
                        start = time.time()
                        while session.working and time.time() - start < max_wait:
                            time.sleep(0.1)
                        if session.working:
                            eval_result["warning"] = "Session still processing after 30 seconds"

                    eval_result["working"] = session.working
                    eval_result["initialized"] = True

            # Handle terminus_run wait - poll for completion marker
            eval_result = result.get("result")
            if isinstance(eval_result, dict) and eval_result.get("_wait_requested"):
                wait_secs = eval_result.pop("_wait_requested")
                wait_tag = eval_result.pop("_wait_tag")
                opened_new = eval_result.pop("_wait_opened_new", False)
                markers = eval_result.pop("_wait_markers", {})

                # Initial delay for terminal to start
                # New terminals need more time: open view + start shell + post_hooks + command start
                startup_delay_ms = 3000 if opened_new else 200

                # Unique markers for this command
                start_marker = markers.get("start", ":::CLAUDE_CMD_START:::")
                end_marker = markers.get("end", ":::CLAUDE_CMD_DONE:::")

                read_result = {"result": None}
                read_done = threading.Event()
                poll_count = [0]
                max_polls = int(wait_secs * 4)  # Poll every 250ms

                def extract_output(content):
                    """Extract only the output between start and end markers."""
                    # Find last occurrence of start marker (in case of multiple commands)
                    start_idx = content.rfind(start_marker)
                    if start_idx != -1:
                        content = content[start_idx + len(start_marker):]
                    # Remove end marker
                    content = content.replace(end_marker, "")
                    return content.strip()

                def do_poll():
                    try:
                        data = self._terminus_read(wait_tag, 200)  # Read more lines
                        content = data.get("content", "")
                        poll_count[0] += 1

                        # Check for our completion marker
                        if end_marker in content or poll_count[0] >= max_polls:
                            # Wait a bit more for buffer to settle, then read final output
                            def do_final_read():
                                final_data = self._terminus_read(wait_tag, 200)
                                final_content = final_data.get("content", "")
                                # Extract only output between markers
                                clean_content = extract_output(final_content)
                                final_data["content"] = clean_content
                                read_result["result"] = final_data
                                read_done.set()
                            sublime.set_timeout(do_final_read, 100)  # 100ms settle time
                        else:
                            # Poll again in 250ms
                            sublime.set_timeout(do_poll, 250)
                    except Exception as e:
                        read_result["result"] = {"error": str(e)}
                        read_done.set()

                # Start polling after startup delay
                sublime.set_timeout(do_poll, startup_delay_ms)
                # Wait for completion (with buffer)
                read_done.wait(timeout=wait_secs + 5)

                read_data = read_result.get("result", {})
                if read_data.get("error"):
                    eval_result["read_error"] = read_data["error"]
                else:
                    eval_result["output"] = read_data.get("content", "")
                    eval_result["total_lines"] = read_data.get("total_lines", 0)

            conn.sendall((json.dumps(result) + "\n").encode())

        except Exception as e:
            try:
                conn.sendall((json.dumps({"error": str(e)}) + "\n").encode())
            except:
                pass
        finally:
            conn.close()

    def _get_window(self):
        """Get the window for the calling session, falling back to active window."""
        # Cache per-eval invocation (caller_view_id doesn't change within one tool call)
        cached = getattr(self, '_cached_window', None)
        cached_vid = getattr(self, '_cached_window_vid', None)
        if cached and cached_vid == self._caller_view_id and cached.is_valid():
            return cached
        if self._caller_view_id:
            for w in sublime.windows():
                for v in w.views():
                    if v.id() == self._caller_view_id:
                        self._cached_window = w
                        self._cached_window_vid = self._caller_view_id
                        return w
        return sublime.active_window()

    def _eval(self, code: str, tool: str = None, caller_view_id: int = None):
        """Execute code in Sublime's context.

        Args:
            code: Python code to execute
            tool: Named tool to load and execute
            caller_view_id: View ID of the calling session (from MCP server)
        """
        # Store caller_view_id for _get_session_for_tool to use
        self._caller_view_id = caller_view_id

        # Load saved tool if specified
        if tool:
            window = self._get_window()
            if window and window.folders():
                tool_path = os.path.join(window.folders()[0], ".claude", "sublime_tools", f"{tool}.py")
                if os.path.exists(tool_path):
                    with open(tool_path, "r") as f:
                        code = f.read()
                else:
                    raise FileNotFoundError(f"Tool not found: {tool_path}")
            else:
                raise RuntimeError("No project folder open")

        exec_globals = {
            "sublime": sublime,
            "sublime_plugin": sublime_plugin,
            "get_open_files": self._get_open_files,
            "get_window_summary": self._get_window_summary,
            "find_file": self._find_file,
            "get_symbols": self._get_symbols,
            "goto_symbol": self._goto_symbol,
            "read_view": self._read_view,
            "terminus_run": self._terminus_run,
            "list_tools": self._list_tools,
            # Session tools
            "list_profiles": self._list_profiles,
            "list_personas": self._list_personas,
            "spawn_session": self._spawn_session,
            "send_to_session": self._send_to_session,
            "list_sessions": self._list_sessions,
            "read_session_output": self._read_session_output,
            "list_profile_docs": self._list_profile_docs,
            "read_profile_doc": self._read_profile_doc,
            # Terminus tools
            "terminus_list": self._terminus_list,
            "terminus_send": self._terminus_send,
            "terminus_read": self._terminus_read,
            "terminus_close": self._terminus_close,
            # Notification tools (notalone2)
            "register_notification": self._register_notification,
            "subscribe_to_service": self._subscribe_to_service,
            "signal_subsession_complete": self._signal_subsession_complete,
            "list_notifications": self._list_notifications,
            "discover_services": self._discover_services,
            # Order table
            "order_table_cmd": self._order_table_cmd,
            # LSP tools
            "lsp_hover": self._lsp_hover,
            "lsp_definition": self._lsp_definition,
            "lsp_references": self._lsp_references,
            "lsp_symbols": self._lsp_symbols,
            "lsp_workspace_symbols": self._lsp_workspace_symbols,
            "lsp_diagnostics": self._lsp_diagnostics,
        }

        # Add context variables
        window = self._get_window()
        exec_globals["cwd"] = window.folders()[0] if window and window.folders() else None
        exec_globals["AGENT_ID"] = str(caller_view_id) if caller_view_id else None

        # Handle return statements
        if "return " in code:
            lines = code.split("\n")
            new_lines = []
            for line in lines:
                stripped = line.lstrip()
                if stripped.startswith("return "):
                    indent = line[:len(line) - len(stripped)]
                    new_lines.append(f"{indent}__result__ = {stripped[7:]}")
                else:
                    new_lines.append(line)
            code = "__result__ = None\n" + "\n".join(new_lines)
        else:
            code = f"__result__ = None\n{code}"

        exec(code, exec_globals)
        return exec_globals.get("__result__")

    def _get_open_files(self) -> list:
        """Get list of open files."""
        window = self._get_window()
        if not window:
            return []
        return [v.file_name() for v in window.views() if v.file_name()]

    def _get_window_summary(self) -> dict:
        """Get summary of current window state (formatted for reduced context)."""
        window = self._get_window()
        if not window:
            return {"error": "No window"}

        # Build formatted output
        lines = []

        # Project folders
        folders = window.folders()
        if folders:
            lines.append(f"Project: {folders[0]}")
            for f in folders[1:]:
                lines.append(f"  + {f}")

        # Active file
        active_view = window.active_view()
        if active_view and active_view.file_name():
            row, col = active_view.rowcol(active_view.sel()[0].begin()) if active_view.sel() else (0, 0)
            lines.append(f"Active: {active_view.file_name()}:{row+1}:{col+1}")

        # Open files (compact list)
        all_views = window.views()
        open_files = [v.file_name() for v in all_views if v.file_name()]
        dirty_files = [v.file_name() for v in all_views if v.file_name() and v.is_dirty()]

        lines.append(f"Open files ({len(open_files)}):")
        for f in open_files[:20]:  # Limit to 20 files
            marker = " *" if f in dirty_files else ""
            lines.append(f"  • {os.path.basename(f)}{marker}")
        if len(open_files) > 20:
            lines.append(f"  ... and {len(open_files) - 20} more")

        return {"summary": "\n".join(lines), "open_count": len(open_files), "dirty_count": len(dirty_files)}

    def _find_file(self, query: str, pattern: str = None, limit: int = 20) -> list:
        """Fuzzy find files by name, optionally filtered by glob pattern."""
        import fnmatch

        window = self._get_window()
        if not window:
            return []

        folders = window.folders()
        if not folders:
            return []

        # Directories to skip
        skip_dirs = {'.git', 'node_modules', '__pycache__', 'venv', '.venv',
                     'env', '.env', 'dist', 'build', '.cache', '.tox'}

        all_files = []
        root_folder = folders[0]

        for folder in folders:
            for dirpath, dirnames, filenames in os.walk(folder):
                dirnames[:] = [d for d in dirnames
                               if not d.startswith('.') and d not in skip_dirs]

                for filename in filenames:
                    if filename.startswith('.'):
                        continue

                    full_path = os.path.join(dirpath, filename)
                    rel_path = os.path.relpath(full_path, root_folder)

                    # Apply pattern filter if provided
                    if pattern:
                        if '**' in pattern:
                            if not fnmatch.fnmatch(rel_path, pattern):
                                continue
                        elif not (fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(rel_path, pattern)):
                            continue

                    all_files.append(rel_path)

        if not all_files:
            return []

        query_lower = query.lower()

        # Score each file - fuzzy matching
        scored = []
        for path in all_files:
            filename = os.path.basename(path).lower()
            path_lower = path.lower()

            if filename == query_lower:
                scored.append((0, path))
            elif filename.startswith(query_lower):
                scored.append((1, path))
            elif query_lower in filename:
                scored.append((2, path))
            elif query_lower in path_lower:
                scored.append((3, path))
            else:
                idx = 0
                for char in query_lower:
                    idx = path_lower.find(char, idx)
                    if idx == -1:
                        break
                    idx += 1
                else:
                    scored.append((4, path))

        scored.sort(key=lambda x: (x[0], x[1]))
        return [path for _, path in scored[:limit]]

    def _get_symbols(self, query, file_path: str = None, limit: int = 10) -> dict:
        """Batch lookup symbols in project index.

        Args:
            query: Single symbol (str), comma-separated, or JSON array
            file_path: Optional file to limit search to
            limit: Max results per symbol (default 10)
        """
        window = self._get_window()
        if not window:
            return {"success": False, "error": "No window"}

        # Normalize query to list
        if isinstance(query, str):
            query = query.strip()
            if query.startswith('['):
                try:
                    symbols = json.loads(query)
                except:
                    symbols = [query]
            elif ',' in query:
                symbols = [s.strip() for s in query.split(',') if s.strip()]
            else:
                symbols = [query]
        elif isinstance(query, list):
            symbols = [str(s).strip() for s in query if str(s).strip()]
        else:
            symbols = []

        if not symbols:
            return {"success": False, "error": "No symbols provided"}

        # Collect and format results
        lines = []
        all_locations = []

        for sym in symbols:
            locations = window.lookup_symbol_in_index(sym)

            # Filter by file_path if provided
            if file_path:
                locations = [loc for loc in locations if loc[0] == file_path]

            if not locations:
                lines.append(f"'{sym}': not found")
                continue

            lines.append(f"'{sym}' ({len(locations)} matches):")
            for loc in locations[:limit] if limit > 0 else locations:
                fp, display_name, (row, col) = loc[0], loc[1], loc[2]
                lines.append(f"  • {os.path.basename(fp)}:{row}:{col} - {display_name}")
                all_locations.append({"symbol": sym, "file": fp, "row": row, "col": col})

            if len(locations) > limit:
                lines.append(f"  ... and {len(locations) - limit} more")

        return {"summary": "\n".join(lines), "locations": all_locations[:50]}

    def _list_tools(self) -> list:
        """List available saved tools."""
        window = self._get_window()
        if not window or not window.folders():
            return []

        tools_dir = os.path.join(window.folders()[0], ".claude", "sublime_tools")
        if not os.path.exists(tools_dir):
            return []

        tools = []
        for filename in os.listdir(tools_dir):
            if not filename.endswith('.py'):
                continue

            name = filename[:-3]  # strip .py
            tool_path = os.path.join(tools_dir, filename)

            # Extract docstring as description
            try:
                with open(tool_path, 'r') as f:
                    code = f.read()
                # Simple docstring extraction - first triple-quoted string
                desc = "No description"
                if code.startswith('"""'):
                    end = code.find('"""', 3)
                    if end > 0:
                        desc = code[3:end].strip()
                elif code.startswith("'''"):
                    end = code.find("'''", 3)
                    if end > 0:
                        desc = code[3:end].strip()

                tools.append({"name": name, "description": desc})
            except:
                tools.append({"name": name, "description": "No description"})

        return tools

    def _goto_symbol(self, query: str) -> dict:
        """Navigate to a symbol definition. Returns the symbol info or error."""
        window = self._get_window()
        if not window:
            return {"error": "No window"}

        locations = window.lookup_symbol_in_index(query)
        if not locations:
            return {"error": f"Symbol '{query}' not found"}

        # Use first match
        loc = locations[0]
        path, name, (row, col) = loc[0], loc[1], loc[2]
        view = window.open_file(f"{path}:{row}:{col}", sublime.ENCODED_POSITION)
        return {"file": path, "name": name, "row": row, "col": col}

    def _read_view(self, file_path: str = None, view_name: str = None, head: int = None, tail: int = None, grep: str = None, grep_i: str = None, max_chars: int = 50000) -> dict:
        """Read content from any view by file path or view name with head/tail/grep filtering.

        Args:
            max_chars: Maximum characters to return (default 50000). Use -1 for unlimited.
        """
        import os
        import re
        window = self._get_window()
        if not window:
            return {"error": "No window"}

        view = None
        identifier = None

        # Search by view name (for scratch buffers)
        if view_name:
            for v in window.views():
                if v.name() == view_name:
                    view = v
                    identifier = view_name
                    break
            if not view:
                return {"error": f"View not found: {view_name}"}

        # Search by file path
        elif file_path:
            # Resolve file path (handle relative paths)
            if not os.path.isabs(file_path):
                # Try relative to project folders
                for folder in window.folders():
                    full_path = os.path.join(folder, file_path)
                    if os.path.exists(full_path):
                        file_path = full_path
                        break

            # Normalize path
            file_path = os.path.normpath(file_path)
            identifier = file_path

            # Find existing view with this file
            for v in window.views():
                if v.file_name() and os.path.normpath(v.file_name()) == file_path:
                    view = v
                    break

            # If not found, try to open it (won't focus, just load)
            if not view:
                if os.path.exists(file_path):
                    view = window.open_file(file_path, sublime.TRANSIENT)
                    # Wait a bit for file to load
                    import time
                    max_wait = 2.0
                    start = time.time()
                    while view.is_loading() and time.time() - start < max_wait:
                        time.sleep(0.05)
                else:
                    return {"error": f"File not found: {file_path}"}

        else:
            return {"error": "Must provide either file_path or view_name"}

        if not view:
            return {"error": f"Could not open view for: {identifier}"}

        # Read content
        content = view.substr(sublime.Region(0, view.size()))
        all_lines = content.split('\n')
        original_line_count = len(all_lines)

        # Apply grep filter first
        if grep or grep_i:
            pattern = grep if grep else grep_i
            flags = re.IGNORECASE if grep_i else 0
            try:
                regex = re.compile(pattern, flags)
                all_lines = [line for line in all_lines if regex.search(line)]
            except re.error as e:
                return {"error": f"Invalid regex pattern: {e}"}

        # Apply head/tail filter
        if head is not None and tail is not None:
            return {"error": "Cannot specify both head and tail"}
        elif head is not None:
            all_lines = all_lines[:head]
        elif tail is not None:
            all_lines = all_lines[-tail:] if tail < len(all_lines) else all_lines

        content = '\n'.join(all_lines)

        # Truncate if content exceeds max_chars
        truncated = False
        if max_chars > 0 and len(content) > max_chars:
            content = content[:max_chars]
            truncated = True

        result = {
            "content": content,
            "size": len(content),
            "line_count": len(all_lines),
            "original_line_count": original_line_count,
            "truncated": truncated,
        }

        # Include identifier in response
        if file_path:
            result["file_path"] = file_path
        if view_name:
            result["view_name"] = view_name

        # Include filter info if applied
        if grep or grep_i:
            result["grep_pattern"] = grep if grep else grep_i
            result["grep_case_insensitive"] = bool(grep_i)
        if head is not None:
            result["head"] = head
        if tail is not None:
            result["tail"] = tail

        return result

    # ─── Session Spawn ────────────────────────────────────────────────────

    def _list_profiles(self) -> dict:
        """List available profiles and checkpoints (formatted)."""
        project_path = _get_project_profiles_path()
        profiles, checkpoints = load_profiles_and_checkpoints(project_path)

        lines = []
        profile_names = []
        checkpoint_names = []

        if profiles:
            lines.append("Profiles:")
            for name, config in profiles.items():
                model = config.get("model", "default")
                desc = config.get("description", "")
                desc_short = f" - {desc[:40]}..." if len(desc) > 40 else f" - {desc}" if desc else ""
                lines.append(f"  • {name} ({model}){desc_short}")
                profile_names.append(name)

        if checkpoints:
            lines.append("Checkpoints:")
            for name, config in checkpoints.items():
                desc = config.get("description", "")
                desc_short = f" - {desc[:40]}..." if len(desc) > 40 else f" - {desc}" if desc else ""
                lines.append(f"  • {name}{desc_short}")
                checkpoint_names.append(name)

        if not lines:
            lines.append("No profiles or checkpoints configured")

        return {"summary": "\n".join(lines), "profiles": profile_names, "checkpoints": checkpoint_names}

    def _list_personas(self) -> dict:
        """List available personas from the persona server."""
        from .persona_client import list_personas as _list_personas
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        persona_url = settings.get("persona_url", "http://localhost:5002/personas")
        personas = _list_personas(persona_url)
        if not personas:
            return {"error": "Failed to fetch personas or none available"}
        lines = []
        for p in personas:
            alias = p.get("alias", "?")
            pid = p.get("id", "?")
            locked = p.get("is_locked", False)
            locked_by = p.get("locked_by_session", "")
            tags = ", ".join(p.get("tags", []))
            status = f"🔒 {locked_by}" if locked else "available"
            line = f"  [{pid}] {alias} ({status})"
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        if not lines:
            return {"summary": "No personas available", "personas": []}
        return {
            "summary": f"Personas ({len(lines)}):\n" + "\n".join(lines),
            "personas": [{"id": p.get("id"), "alias": p.get("alias"), "is_locked": p.get("is_locked", False)} for p in personas]
        }

    def _spawn_session(self, prompt: str, name: str = None, profile: str = None, checkpoint: str = None, persona_id: int = None, fork_current: bool = False, wait_for_completion: bool = False, backend: str = "claude", _caller_view_id: int = None) -> dict:
        """Spawn a new Claude session with the given prompt. Returns with _wait_for_init flag.

        Args:
            _caller_view_id: The view_id of the calling session. If provided, used as parent_view_id.
                             This ensures subsession signals go to the correct parent.
        """
        from .core import create_session, get_active_session
        import uuid

        window = self._get_window()
        if not window:
            return {"error": "No window"}

        project_path = _get_project_profiles_path()
        profiles, checkpoints = load_profiles_and_checkpoints(project_path)
        profile_config = None
        resume_id = None
        fork = False

        # Load profile config if specified
        if profile:
            if profile not in profiles:
                return {"error": f"Profile '{profile}' not found"}
            profile_config = profiles[profile]

        # Load persona config if specified (overrides profile)
        if persona_id:
            from .persona_client import get_persona
            settings = sublime.load_settings("ClaudeCode.sublime-settings")
            persona_url = settings.get("persona_url", "http://localhost:5002/personas")
            persona = get_persona(persona_id, persona_url)
            if not persona:
                return {"error": f"Failed to fetch persona {persona_id}"}
            # Get system_prompt from ability_version or persona top-level
            ability_version = persona.get("ability_version") or {}
            profile_config = {
                "model": ability_version.get("model") or "sonnet",
                "system_prompt": ability_version.get("system_prompt") or persona.get("system_prompt") or "",
            }
            if not name:
                name = persona.get("alias", f"persona-{persona_id}")

        # Fork from caller session if requested
        if fork_current:
            caller_session = sublime._claude_sessions.get(_caller_view_id) if _caller_view_id else None
            if not caller_session:
                caller_session = get_active_session(window)
            current_session = caller_session
            if current_session and current_session.session_id:
                # Session IDs aren't portable across backends — codex thread IDs
                # can't be resumed by the Claude bridge, etc. Only allow fork
                # within the same backend family (claude+deepseek share the Claude
                # bridge's local storage, but codex/copilot live in their own).
                same_family = current_session.backend == backend or (
                    current_session.backend in ("claude", "deepseek") and backend in ("claude", "deepseek")
                )
                if not same_family:
                    return {"error": f"Cannot fork {current_session.backend!r} session into {backend!r} backend; session IDs are not portable. Spawn fresh (fork_current=False)."}
                resume_id = current_session.session_id
                fork = True
            else:
                return {"error": "Cannot fork: current session has no session_id"}

        # Load checkpoint if specified (overrides fork_current)
        if checkpoint:
            if checkpoint not in checkpoints:
                return {"error": f"Checkpoint '{checkpoint}' not found"}
            resume_id = checkpoints[checkpoint].get("session_id")
            fork = True

        # Generate unique subsession ID for notalone2 completion tracking
        subsession_id = f"subsession-{uuid.uuid4().hex[:8]}"

        # Get parent view_id - prefer explicit _caller_view_id, fall back to inference
        if _caller_view_id:
            parent_view_id = _caller_view_id
        else:
            # Fall back to inferring from execution context (less reliable with multiple sessions)
            current_session, _ = self._get_session_for_tool()
            parent_view_id = current_session.output.view.id() if current_session and current_session.output.view else None

        # Prepare initial context for subsession
        initial_context = {
            "subsession_id": subsession_id,
            "parent_view_id": parent_view_id,
        }

        # Create new session with initial context
        session = create_session(window, resume_id=resume_id, fork=fork, profile=profile_config, initial_context=initial_context, backend=backend)
        if name:
            session.name = name
            session.output.set_name(name)

        view_id = session.output.view.id() if session.output.view else None

        # Return with flags for background thread to handle waiting
        return {
            "_wait_for_init": True,  # Signal to wait for initialization
            "_session": session,  # Session object for polling
            "_prompt": prompt,  # Prompt to send after init
            "_wait_for_completion": wait_for_completion,  # Whether to also wait for completion
            "spawned": True,
            "name": name or "(unnamed)",
            "view_id": view_id,
            "subsession_id": subsession_id,  # Return subsession_id for parent to track
            "profile": profile,
            "checkpoint": checkpoint,
        }

    def _send_to_session(self, view_id: int, prompt: str) -> dict:
        """Send a message to an existing session."""

        session = sublime._claude_sessions.get(view_id)
        if not session:
            return {"error": f"Session not found for view_id {view_id}"}

        if session.working:
            return {"error": "Session is busy", "view_id": view_id}

        if not session.initialized:
            return {"error": "Session not initialized", "view_id": view_id}

        session.query(prompt)
        return {
            "sent": True,
            "view_id": view_id,
            "name": session.name or "(unnamed)",
        }

    def _list_sessions(self) -> dict:
        """List subsessions only (spawned via spawn_session)."""
        caller_id = self._caller_view_id
        sessions = []
        lines = []
        for view_id, session in sublime._claude_sessions.items():
            # Skip the calling session itself
            if view_id == caller_id:
                continue
            # Only show subsessions (spawned sessions have a parent_view_id)
            if not getattr(session, 'parent_view_id', None):
                continue
            status = "⏳" if session.working else "✓"
            cost = f"${session.total_cost:.4f}" if session.total_cost else ""
            name = session.name or "(unnamed)"
            lines.append(f"{status} [{view_id}] {name} ({session.query_count} queries) {cost}")
            sessions.append({"view_id": view_id, "name": name, "working": session.working})

        if not lines:
            return {"summary": "No subsessions", "sessions": []}
        return {"summary": "\n".join(lines), "sessions": sessions, "count": len(sessions)}

    def _read_session_output(self, view_id: int, lines: int = None, max_chars: int = 30000) -> dict:
        """Read conversation output from a session's view.

        Args:
            lines: Limit to last N lines
            max_chars: Maximum characters to return (default 30000). Use -1 for unlimited.
                       Smart truncation preserves message boundaries.
        """
        # Debug: log all session view_ids
        all_view_ids = list(sublime._claude_sessions.keys())
        print(f"[Claude] read_session_output: looking for {view_id}, _sessions={id(sublime._claude_sessions)}, available: {all_view_ids}")

        session = sublime._claude_sessions.get(view_id)
        if not session:
            return {
                "error": f"Session not found for view_id {view_id}",
                "available_sessions": all_view_ids,
            }

        if not session.output or not session.output.view:
            return {"error": "Session output view not found", "view_id": view_id}

        # Read text content from the output view
        view = session.output.view
        content = view.substr(sublime.Region(0, view.size()))

        # Optionally limit to last N lines
        if lines:
            all_lines = content.split('\n')
            if len(all_lines) > lines:
                content = '\n'.join(all_lines[-lines:])

        # Smart truncation: preserve message boundaries
        truncated = False
        skipped_messages = 0
        if max_chars > 0 and len(content) > max_chars:
            # Split by message separator (─── or blank lines between messages)
            import re
            # Messages are typically separated by horizontal lines or double newlines
            message_pattern = r'\n(?=───|╭|▸|⚠|✓|✗|\n\n)'
            parts = re.split(message_pattern, content)

            # Keep messages from the end until we exceed max_chars
            kept_parts = []
            total_len = 0
            for part in reversed(parts):
                if total_len + len(part) > max_chars and kept_parts:
                    skipped_messages += 1
                    continue
                kept_parts.insert(0, part)
                total_len += len(part) + 1  # +1 for separator

            content = '\n'.join(kept_parts)
            truncated = True

            # Add truncation notice at the beginning
            if skipped_messages > 0:
                content = f"[... {skipped_messages} earlier messages truncated ...]\n\n{content}"

        return {
            "view_id": view_id,
            "name": session.name or "(unnamed)",
            "working": session.working,
            "output": content,
            "line_count": content.count('\n') + 1 if content else 0,
            "truncated": truncated,
        }

    def _list_profile_docs(self) -> dict:
        """List documentation files available from current session's profile."""
        window = self._get_window()
        if not window:
            return {"error": "No active window"}

        # Get active session
        active_view_id = window.settings().get("claude_active_view")
        if not active_view_id or active_view_id not in sublime._claude_sessions:
            return {"error": "No active Claude session"}

        session = sublime._claude_sessions[active_view_id]

        if not session.profile_docs:
            return {
                "docs": [],
                "count": 0,
                "note": "No profile docs configured for this session"
            }

        return {
            "docs": session.profile_docs,
            "count": len(session.profile_docs),
            "profile": session.profile.get("description", "") if session.profile else ""
        }

    def _read_profile_doc(self, path: str) -> dict:
        """Read a documentation file from current session's profile docset."""
        window = self._get_window()
        if not window:
            return {"error": "No active window"}

        # Get active session
        active_view_id = window.settings().get("claude_active_view")
        if not active_view_id or active_view_id not in sublime._claude_sessions:
            return {"error": "No active Claude session"}

        session = sublime._claude_sessions[active_view_id]

        if path not in session.profile_docs:
            return {
                "error": f"File '{path}' not in profile docset",
                "available": session.profile_docs[:10],  # Show first 10
                "total": len(session.profile_docs)
            }

        # Read the file
        import os
        cwd = session._cwd()
        full_path = os.path.join(cwd, path)

        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            # Ensure content is JSON-serializable by replacing problematic characters
            # This shouldn't be necessary since json.dumps handles escaping,
            # but we ensure clean UTF-8 just in case
            import json
            # Test that it can be serialized
            try:
                json.dumps(content)
            except (TypeError, ValueError) as e:
                return {"error": f"Content not JSON-serializable: {str(e)}"}

            return {
                "path": path,
                "content": content,
                "size": len(content),
            }
        except Exception as e:
            return {"error": f"Failed to read {path}: {str(e)}"}

    # ─── Terminus Tools ───────────────────────────────────────────────────

    def _terminus_list(self) -> list:
        """List all Terminus terminal views in the current window."""
        window = self._get_window()
        if not window:
            return []

        result = []
        for view in window.views():
            if view.settings().get("terminus_view"):
                tag = view.settings().get("terminus_view.tag", "")
                title = view.name() or "(unnamed)"
                result.append({
                    "view_id": view.id(),
                    "tag": tag,
                    "title": title,
                })
        return result

    def _terminus_run(self, command: str, tag: str = None, wait: float = 30, target_id: str = None) -> dict:
        """Run command in terminal, block until done and return output.

        Args:
            command: Command to run (newline appended if missing)
            tag: Terminal tag (default: claude-agent)
            wait: Max seconds to wait for output (default 30, 0 = fire and forget)
            target_id: Optional terminal ID for sharing across sessions
        """
        # Priority: target_id > tag > session-specific default
        if target_id:
            tag = f"claude-agent-{target_id}"
        elif not tag:
            # Default tag uses active Claude session's view ID for isolation
            # Each session gets its own terminal to avoid state pollution
            from . import core
            window = self._get_window()
            # Find active session via window's active view setting
            active_view_id = window.settings().get("claude_active_view") if window else None
            if active_view_id and active_view_id in sublime._claude_sessions:
                session = sublime._claude_sessions[active_view_id]
                tag = f"claude-agent-{active_view_id}"
            else:
                # Fallback to window ID if no session
                window_id = window.id() if window else 0
                tag = f"claude-agent-{window_id}"

        # If wait requested, wrap command with unique start/end markers
        # Unique ID prevents collision when multiple commands run in same terminal
        import uuid
        cmd_id = uuid.uuid4().hex[:8]
        start_marker = f":::CLAUDE_CMD_START_{cmd_id}:::"
        end_marker = f":::CLAUDE_CMD_DONE_{cmd_id}:::"
        if wait and wait > 0:
            # Echo start marker, run in subshell, echo end marker
            cmd = command.rstrip('\n')
            command = f"echo '{start_marker}'; ( {cmd} ); echo '{end_marker}'\n"
            # Store markers for polling
            result_markers = {"start": start_marker, "end": end_marker}

        # Send the command (tag already resolved, don't pass target_id again)
        result = self._terminus_send(command, tag)

        if result.get("error"):
            return result

        # If we opened a new terminal, we need extra startup time
        opened_new = result.get("opened_new", False)

        # Flag for caller to handle wait on background thread
        if wait and wait > 0:
            result["_wait_requested"] = wait
            result["_wait_tag"] = tag
            result["_wait_opened_new"] = opened_new
            result["_wait_markers"] = result_markers

        return result

    def _terminus_send(self, text: str, tag: str = None, target_id: str = None) -> dict:
        """Send text/command to a Terminus terminal.

        If no tag specified, uses "claude-agent-{window_id}" tag to keep agent commands
        in a dedicated terminal per window (won't hijack user's terminals).
        """
        window = self._get_window()
        if not window:
            return {"error": "No window"}

        # Priority: target_id > tag > session-specific default
        if target_id:
            tag = f"claude-agent-{target_id}"
        elif not tag:
            # Default tag uses active Claude session's view ID for isolation
            from . import core
            active_view_id = window.settings().get("claude_active_view") if window else None
            if active_view_id and active_view_id in sublime._claude_sessions:
                tag = f"claude-agent-{active_view_id}"
            else:
                tag = f"claude-agent-{window.id()}"

        def find_terminal():
            for view in window.views():
                if view.settings().get("terminus_view"):
                    if view.settings().get("terminus_view.tag") == tag:
                        # Check if terminal is still alive (not orphaned after restart)
                        # Terminus sets terminus_view.finished when terminal exits
                        if not view.settings().get("terminus_view.finished"):
                            return view
            return None

        target = find_terminal()

        # Open terminal if not found
        if not target:
            # Check if Terminus is available
            if not hasattr(sublime, 'find_resources') or not sublime.find_resources("Terminus.sublime-settings"):
                return {"error": "Terminus plugin not installed"}

            cwd = window.folders()[0] if window.folders() else None
            open_args = {
                "tag": tag,
                "title": "Claude Agent",
                "post_window_hooks": [
                    # Send the command after terminal is ready
                    ["terminus_send_string", {"string": text, "tag": tag}]
                ],
                # Set env var so scripts can detect they're running under Claude agent
                "env": {"CLAUDE_AGENT": "1"},
            }
            if cwd:
                open_args["cwd"] = cwd
            # Don't auto-focus terminal - let it open in background
            open_args["focus"] = False
            print(f"[Claude] terminus_send: opening terminal with args={open_args}")

            # Schedule terminus_open to run after current call stack clears
            # This ensures the command actually executes
            # Capture window_id to find correct window even if focus changed
            window_id = window.id()
            def do_open():
                # Find window by ID in case focus changed
                target_window = None
                for w in sublime.windows():
                    if w.id() == window_id:
                        target_window = w
                        break
                if target_window:
                    print(f"[Claude] terminus_send: do_open executing in window {window_id}")
                    target_window.run_command("terminus_open", open_args)
                else:
                    print(f"[Claude] terminus_send: window {window_id} not found")
            sublime.set_timeout(do_open, 10)

            return {
                "sent": True,
                "opened_new": True,
                "tag": tag,
            }

        # Terminal exists - send command directly
        print(f"[Claude] terminus_send: sending to terminal {target.id()}")
        window.run_command("terminus_send_string", {"string": text, "tag": tag})

        return {
            "sent": True,
            "view_id": target.id(),
            "tag": tag,
        }

    def _terminus_read(self, tag: str = None, lines: int = 100, target_id: str = None) -> dict:
        """Read output from a Terminus terminal."""
        # Priority: target_id > tag > session-specific default
        if target_id:
            tag = f"claude-agent-{target_id}"
        elif not tag:
            # Default tag uses active Claude session's view ID for isolation
            from . import core
            window = self._get_window()
            active_view_id = window.settings().get("claude_active_view") if window else None
            if active_view_id and active_view_id in sublime._claude_sessions:
                tag = f"claude-agent-{active_view_id}"
            else:
                window_id = window.id() if window else 0
                tag = f"claude-agent-{window_id}"

        # Find matching terminal across ALL windows (might be in different window)
        target = None
        for window in sublime.windows():
            for view in window.views():
                if view.settings().get("terminus_view"):
                    if view.settings().get("terminus_view.tag") == tag:
                        if not view.settings().get("terminus_view.finished"):
                            target = view
                            break
            if target:
                break

        if not target:
            return {"error": f"No terminal found with tag '{tag}'"}

        # Read last N lines
        content = target.substr(sublime.Region(0, target.size()))
        content_lines = content.split("\n")
        if lines and len(content_lines) > lines:
            content_lines = content_lines[-lines:]

        return {
            "view_id": target.id(),
            "tag": target.settings().get("terminus_view.tag", ""),
            "content": "\n".join(content_lines),
            "total_lines": len(content.split("\n")),
        }

    def _terminus_close(self, tag: str = None, target_id: str = None) -> dict:
        """Close a Terminus terminal."""
        # Priority: target_id > tag > session-specific default
        if target_id:
            tag = f"claude-agent-{target_id}"
        elif not tag:
            # Default tag uses active Claude session's view ID for isolation
            from . import core
            window = self._get_window()
            active_view_id = window.settings().get("claude_active_view") if window else None
            if active_view_id and active_view_id in sublime._claude_sessions:
                tag = f"claude-agent-{active_view_id}"
            else:
                window_id = window.id() if window else 0
                tag = f"claude-agent-{window_id}"

        # Find matching terminal across ALL windows
        target = None
        for window in sublime.windows():
            for view in window.views():
                if view.settings().get("terminus_view"):
                    if view.settings().get("terminus_view.tag") == tag:
                        target = view
                        break
            if target:
                break

        if not target:
            return {"error": f"No terminal found with tag '{tag}'"}

        view_id = target.id()
        tag_val = target.settings().get("terminus_view.tag", "")

        # Close the terminal
        target.close()

        return {
            "closed": True,
            "view_id": view_id,
            "tag": tag_val,
        }

    # ─── LSP Tools ────────────────────────────────────────────────────────

    def _resolve_file_view(self, file_path):
        """Resolve a file path to a Sublime view, opening if needed."""
        window = self._get_window()
        if not window:
            return None, "No window"

        # Resolve relative paths
        if not os.path.isabs(file_path):
            for folder in window.folders():
                full = os.path.join(folder, file_path)
                if os.path.exists(full):
                    file_path = full
                    break

        file_path = os.path.normpath(file_path)

        # Find existing view
        for v in window.views():
            if v.file_name() and os.path.normpath(v.file_name()) == file_path:
                return v, None

        # Open transiently
        if os.path.exists(file_path):
            view = window.open_file(file_path, sublime.TRANSIENT)
            max_wait = 2.0
            start = time.time()
            while view.is_loading() and time.time() - start < max_wait:
                time.sleep(0.05)
            return view, None

        return None, f"File not found: {file_path}"

    def _get_lsp_session(self, view, capability=None):
        """Get best LSP session for a view."""
        try:
            from LSP.plugin.core.registry import windows as lsp_windows
        except ImportError:
            return None, "LSP package not installed"

        listener = lsp_windows.listener_for_view(view)
        if not listener:
            return None, "No LSP listener for this view"

        if capability:
            session = listener.session_async(capability)
            if not session:
                return None, f"No LSP server with {capability} capability"
            return session, None
        else:
            sessions = listener.sessions_async()
            if sessions:
                return sessions[0], None
            return None, "No LSP server for this view"

    def _lsp_request_sync(self, session, method, params, view=None, timeout=5):
        """Blocking LSP request via threading.Event."""
        try:
            from LSP.plugin.core.protocol import Request
        except ImportError:
            return None, "LSP package not installed"

        event = threading.Event()
        result = [None]
        error = [None]

        def on_result(r):
            result[0] = r
            event.set()

        def on_error(e):
            error[0] = str(e) if e else "Unknown error"
            event.set()

        request = Request(method, params, view=view)
        session.send_request(request, on_result, on_error)
        event.wait(timeout)

        if error[0]:
            return None, error[0]
        return result[0], None

    def _lsp_hover(self, file_path, line, col):
        """Get hover info (type, docs) at position."""
        try:
            from LSP.plugin.core.views import text_document_position_params
        except ImportError:
            return {"error": "LSP package not installed"}

        view, err = self._resolve_file_view(file_path)
        if err:
            return {"error": err}

        session, err = self._get_lsp_session(view, "hoverProvider")
        if err:
            return {"error": err}

        point = view.text_point(line, col)
        params = text_document_position_params(view, point)
        result, err = self._lsp_request_sync(session, "textDocument/hover", params, view)
        if err:
            return {"error": err}
        if not result:
            return {"result": None, "message": "No hover info at this position"}

        # Extract content from hover result
        contents = result.get("contents", "")
        if isinstance(contents, dict):
            # MarkupContent: {kind, value}
            text = contents.get("value", "")
        elif isinstance(contents, list):
            # MarkedString[]
            parts = []
            for item in contents:
                if isinstance(item, dict):
                    parts.append(item.get("value", ""))
                else:
                    parts.append(str(item))
            text = "\n\n".join(parts)
        else:
            text = str(contents)

        return {"content": text}

    def _lsp_definition(self, file_path, line, col):
        """Get definition location(s) for symbol at position."""
        try:
            from LSP.plugin.core.views import text_document_position_params
        except ImportError:
            return {"error": "LSP package not installed"}

        view, err = self._resolve_file_view(file_path)
        if err:
            return {"error": err}

        session, err = self._get_lsp_session(view, "definitionProvider")
        if err:
            return {"error": err}

        point = view.text_point(line, col)
        params = text_document_position_params(view, point)
        result, err = self._lsp_request_sync(session, "textDocument/definition", params, view)
        if err:
            return {"error": err}
        if not result:
            return {"locations": [], "message": "No definition found"}

        return {"locations": self._parse_locations(result)}

    def _lsp_references(self, file_path, line, col):
        """Find all references to symbol at position."""
        try:
            from LSP.plugin.core.views import text_document_position_params
        except ImportError:
            return {"error": "LSP package not installed"}

        view, err = self._resolve_file_view(file_path)
        if err:
            return {"error": err}

        session, err = self._get_lsp_session(view, "referencesProvider")
        if err:
            return {"error": err}

        point = view.text_point(line, col)
        params = text_document_position_params(view, point)
        params["context"] = {"includeDeclaration": True}
        result, err = self._lsp_request_sync(session, "textDocument/references", params, view)
        if err:
            return {"error": err}
        if not result:
            return {"locations": [], "message": "No references found"}

        return {"locations": self._parse_locations(result), "count": len(result)}

    def _lsp_symbols(self, file_path):
        """List all symbols in a file."""
        try:
            from LSP.plugin.core.views import text_document_identifier
        except ImportError:
            return {"error": "LSP package not installed"}

        view, err = self._resolve_file_view(file_path)
        if err:
            return {"error": err}

        session, err = self._get_lsp_session(view, "documentSymbolProvider")
        if err:
            return {"error": err}

        params = {"textDocument": text_document_identifier(view)}
        result, err = self._lsp_request_sync(session, "textDocument/documentSymbol", params, view)
        if err:
            return {"error": err}
        if not result:
            return {"symbols": []}

        symbols = self._flatten_document_symbols(result)
        return {"symbols": symbols, "count": len(symbols)}

    def _lsp_workspace_symbols(self, query):
        """Search symbols across the workspace."""
        try:
            from LSP.plugin.core.registry import windows as lsp_windows
        except ImportError:
            return {"error": "LSP package not installed"}

        window = self._get_window()
        if not window:
            return {"error": "No window"}

        # Find any session with workspace symbol capability
        session = None
        for w_manager_window in sublime.windows():
            listener = None
            active = w_manager_window.active_view()
            if active:
                listener = lsp_windows.listener_for_view(active)
            if listener:
                s = listener.session_async("workspaceSymbolProvider")
                if s:
                    session = s
                    break

        if not session:
            return {"error": "No LSP server with workspaceSymbolProvider capability"}

        params = {"query": query}
        result, err = self._lsp_request_sync(session, "workspace/symbol", params)
        if err:
            return {"error": err}
        if not result:
            return {"symbols": []}

        symbols = []
        for sym in result:
            location = sym.get("location", {})
            uri = location.get("uri", "")
            range_info = location.get("range", {}).get("start", {})
            file_path = self._uri_to_path(uri)
            symbols.append({
                "name": sym.get("name", ""),
                "kind": self._symbol_kind_name(sym.get("kind", 0)),
                "file": file_path,
                "line": range_info.get("line", 0),
                "col": range_info.get("character", 0),
                "container": sym.get("containerName", ""),
            })

        return {"symbols": symbols, "count": len(symbols)}

    def _lsp_diagnostics(self, file_path=None):
        """Get diagnostics (errors/warnings) for a file or active file."""
        try:
            from LSP.plugin.core.registry import windows as lsp_windows
        except ImportError:
            return {"error": "LSP package not installed"}

        if file_path:
            view, err = self._resolve_file_view(file_path)
            if err:
                return {"error": err}
        else:
            window = self._get_window()
            view = window.active_view() if window else None
            if not view:
                return {"error": "No active view"}

        listener = lsp_windows.listener_for_view(view)
        if not listener:
            return {"error": "No LSP listener for this view"}

        sessions = listener.sessions_async()
        if not sessions:
            return {"error": "No LSP server for this view"}

        all_diagnostics = []
        for session in sessions:
            try:
                from LSP.plugin.core.views import uri_from_view
                uri = uri_from_view(view)
                diags = session.diagnostics.get_diagnostics_for_uri(uri)
                severity_names = {1: "error", 2: "warning", 3: "info", 4: "hint"}
                for d in diags:
                    range_info = d.get("range", {}).get("start", {})
                    all_diagnostics.append({
                        "severity": severity_names.get(d.get("severity", 4), "unknown"),
                        "message": d.get("message", ""),
                        "line": range_info.get("line", 0),
                        "col": range_info.get("character", 0),
                        "source": d.get("source", ""),
                    })
            except Exception as e:
                all_diagnostics.append({"error": f"Failed to get diagnostics from {session.config.name}: {e}"})

        file_name = view.file_name() or "(untitled)"
        return {
            "file": file_name,
            "diagnostics": all_diagnostics,
            "count": len(all_diagnostics),
        }

    def _uri_to_path(self, uri):
        """Convert file:// URI to filesystem path."""
        if uri.startswith("file://"):
            from urllib.parse import unquote, urlparse
            parsed = urlparse(uri)
            return unquote(parsed.path)
        return uri

    def _parse_locations(self, result):
        """Parse Location or Location[] from LSP response."""
        if isinstance(result, dict):
            result = [result]
        locations = []
        for loc in result:
            uri = loc.get("uri", loc.get("targetUri", ""))
            range_info = loc.get("range", loc.get("targetRange", loc.get("targetSelectionRange", {}))).get("start", {})
            file_path = self._uri_to_path(uri)
            locations.append({
                "file": file_path,
                "line": range_info.get("line", 0),
                "col": range_info.get("character", 0),
            })
        return locations

    def _flatten_document_symbols(self, symbols, parent_name=""):
        """Flatten DocumentSymbol tree into flat list."""
        result = []
        for sym in symbols:
            name = sym.get("name", "")
            full_name = f"{parent_name}.{name}" if parent_name else name

            # DocumentSymbol has 'range', SymbolInformation has 'location'
            if "range" in sym:
                range_info = sym["range"]["start"]
                result.append({
                    "name": full_name,
                    "kind": self._symbol_kind_name(sym.get("kind", 0)),
                    "line": range_info.get("line", 0),
                    "col": range_info.get("character", 0),
                })
            elif "location" in sym:
                range_info = sym["location"].get("range", {}).get("start", {})
                result.append({
                    "name": full_name,
                    "kind": self._symbol_kind_name(sym.get("kind", 0)),
                    "line": range_info.get("line", 0),
                    "col": range_info.get("character", 0),
                })

            # Recurse into children
            children = sym.get("children", [])
            if children:
                result.extend(self._flatten_document_symbols(children, full_name))

        return result

    _SYMBOL_KIND_NAMES = {
        1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
        6: "Method", 7: "Property", 8: "Field", 9: "Constructor", 10: "Enum",
        11: "Interface", 12: "Function", 13: "Variable", 14: "Constant",
        15: "String", 16: "Number", 17: "Boolean", 18: "Array", 19: "Object",
        20: "Key", 21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
        25: "Operator", 26: "TypeParameter",
    }

    def _symbol_kind_name(self, kind):
        return self._SYMBOL_KIND_NAMES.get(kind, f"Unknown({kind})")

    # ─── Session Helpers ──────────────────────────────────────────────────

    def _get_session_for_tool(self, session_id: int = None):
        """Get the Claude session for tool execution.

        Args:
            session_id: Optional specific session to use. If not provided,
                        uses claude_executing_view (set during internal tool execution).

        Returns:
            Tuple of (session, error_dict)
        """
        # If session_id provided, use it directly
        if session_id is not None:
            if session_id not in sublime._claude_sessions:
                available = list(sublime._claude_sessions.keys())
                return None, {
                    "error": f"Session not found: {session_id}",
                    "available_sessions": available
                }
            return sublime._claude_sessions[session_id], None

        # Try to find session from execution context
        window = self._get_window()
        if not window:
            return None, {"error": "No active window"}

        # First try caller_view_id from MCP request (most reliable for subsessions)
        view_id = getattr(self, '_caller_view_id', None)

        # Fall back to claude_executing_view (set during active query)
        if not view_id or view_id not in sublime._claude_sessions:
            view_id = window.settings().get("claude_executing_view")

        # Fall back to claude_active_view (last active session)
        if not view_id or view_id not in sublime._claude_sessions:
            view_id = window.settings().get("claude_active_view")

        # Last resort: if only one session exists, use it
        if not view_id or view_id not in sublime._claude_sessions:
            available = list(sublime._claude_sessions.keys())
            if len(available) == 1:
                view_id = available[0]
            else:
                return None, {
                    "error": "No session context available",
                    "hint": "Multiple sessions active. Focus the target session window.",
                    "available_sessions": available
                }

        return sublime._claude_sessions[view_id], None

    # ─── Notification Tools (notalone2) ────────────────────────────────
    # Uses notalone2 daemon for timer and subsession notifications

    def _register_notification(self, notification_type: str, params: dict, wake_prompt: str) -> dict:
        """Register a notification via notalone2 daemon (direct sync socket).

        Args:
            notification_type: 'timer', 'subsession_complete', or service type
            params: Type-specific parameters (e.g., {'seconds': 30} for timer)
            wake_prompt: Prompt to inject when notification fires

        Returns:
            {notification_id: str, status: "registered"}
        """
        session, error = self._get_session_for_tool()
        if error:
            return error

        # Get view_id for session_id
        view_id = session.output.view.id() if session.output and session.output.view else None
        if not view_id:
            return {"error": "Session has no view"}

        # Direct sync socket call to daemon (like hive does)
        import socket
        from pathlib import Path

        socket_path = str(Path.home() / ".notalone" / "notalone.sock")
        session_id = f"sublime.{view_id}"

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(socket_path)
            sock.sendall((json.dumps({
                "method": "register",
                "session_id": session_id,
                "type": notification_type,
                "params": params,
                "wake_prompt": wake_prompt
            }) + "\n").encode())

            data = b""
            while b"\n" not in data:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                data += chunk

            sock.close()
            resp = json.loads(data.decode().strip())

            if resp.get("notification_id"):
                return {
                    "notification_id": resp["notification_id"],
                    "status": "registered",
                    "session_id": session_id
                }
            else:
                error_msg = resp.get("error", "Registration failed")
                # Fetch available services to help agent
                try:
                    services_resp = self._discover_services()
                    available = services_resp.get("services", {})
                    service_types = []
                    for svc, types in available.items():
                        for t in types:
                            service_types.append(f"{svc}.{t}")
                except:
                    service_types = []

                return {
                    "error": error_msg,
                    "hint": f"Invalid type '{notification_type}'. Use discover_services() or try one of these.",
                    "builtin_types": ["timer", "subsession"],
                    "service_types": service_types
                }

        except FileNotFoundError:
            return {"error": "notalone2 daemon not running"}
        except Exception as e:
            return {"error": str(e)}

    def _subscribe_to_service(self, notification_type: str, params: dict, wake_prompt: str) -> dict:
        """Subscribe to a service - handles HTTP endpoints for channel services.

        For channel-type services with HTTP endpoints, POSTs to the endpoint first,
        then registers with notalone daemon.
        """
        import socket
        import urllib.request
        from pathlib import Path

        session, error = self._get_session_for_tool()
        if error:
            return error

        view_id = session.output.view.id() if session.output and session.output.view else None
        if not view_id:
            return {"error": "Session has no view"}

        socket_path = str(Path.home() / ".notalone" / "notalone.sock")
        session_id = f"sublime.{view_id}"

        # Get services list to check if this is a channel service
        services = []
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(socket_path)
            sock.sendall((json.dumps({"method": "services"}) + "\n").encode())
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            sock.close()
            services_data = json.loads(data.decode().strip())
            services = services_data.get("services", [])
        except Exception as e:
            print(f"[Claude MCP] Failed to get services: {e}")

        # Check if this is a channel service with an endpoint
        endpoint = None
        mode = None
        for svc in services:
            if svc.get("type") == notification_type:
                endpoint = svc.get("endpoint")
                mode = svc.get("mode")
                break

        # Only POST to endpoint for channel-mode services (not notify-mode)
        if endpoint and mode == "channel":
            try:
                # Include session_id in params for the endpoint
                post_data = dict(params) if params else {}
                post_data["session_id"] = session_id
                req = urllib.request.Request(
                    endpoint,
                    data=json.dumps(post_data).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    result = resp.read().decode()
                    print(f"[Claude MCP] Subscribed to {notification_type}: {result}")
            except Exception as e:
                print(f"[Claude MCP] Failed to POST to {notification_type}: {e}")
                return {"error": f"Failed to subscribe to service endpoint: {e}"}

        # Now register with notalone daemon
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(socket_path)
            sock.sendall((json.dumps({
                "method": "register",
                "session_id": session_id,
                "type": notification_type,
                "params": params,
                "wake_prompt": wake_prompt
            }) + "\n").encode())

            data = b""
            while b"\n" not in data:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                data += chunk

            sock.close()
            resp = json.loads(data.decode().strip())

            if resp.get("notification_id"):
                return {
                    "notification_id": resp["notification_id"],
                    "status": "registered",
                    "session_id": session_id
                }
            else:
                return {"error": resp.get("error", "Registration failed")}

        except FileNotFoundError:
            return {"error": "notalone2 daemon not running"}
        except Exception as e:
            return {"error": str(e)}

    def _signal_subsession_complete(self, session_id: int = None, result_summary: str = None) -> dict:
        """Signal that this subsession has completed.

        Directly injects result_summary into parent session's prompt queue.

        Args:
            session_id: The subsession's view_id (required to identify caller)
            result_summary: Optional summary of what was accomplished

        Returns:
            {status: "signaled", subsession_id: str}
        """
        # Look up the calling session by session_id
        if session_id is None:
            return {"error": "session_id is required - pass your view_id from spawn result"}

        session = sublime._claude_sessions.get(session_id)
        if not session:
            available = list(sublime._claude_sessions.keys())
            return {"error": f"Session {session_id} not found", "available": available}

        # Get parent_view_id from this subsession
        parent_view_id = getattr(session, 'parent_view_id', None)
        subsession_id = getattr(session, 'subsession_id', None)

        if not parent_view_id:
            return {"error": f"Session {session_id} is not a subsession - no parent_view_id"}

        # Look up parent session directly in Sublime
        parent_session = sublime._claude_sessions.get(parent_view_id)
        if not parent_session:
            available = list(sublime._claude_sessions.keys())
            return {"error": f"Parent session not found: {parent_view_id}", "available": available}

        # Check if parent session is ready to receive
        if not parent_session.client:
            return {"error": f"Parent session {parent_view_id} has no client connection"}
        if not parent_session.initialized:
            return {"error": f"Parent session {parent_view_id} not initialized"}

        # Build wake prompt
        wake_prompt = f"✅ Subsession {subsession_id} completed"
        if result_summary:
            wake_prompt += f":\n{result_summary}"

        print(f"[Claude] signal_complete: queuing for parent {parent_view_id}: {wake_prompt[:50]}...")

        # Queue injection with retry if parent is busy
        def try_inject():
            if not parent_session.working:
                print(f"[Claude] signal_complete: injecting now to {parent_view_id}")
                parent_session.query(wake_prompt, display_prompt=f"📬 Subsession complete")
            else:
                # Parent still busy, retry in 500ms
                print(f"[Claude] signal_complete: parent {parent_view_id} busy, retrying...")
                sublime.set_timeout(try_inject, 500)

        sublime.set_timeout(try_inject, 0)

        return {"status": "signaled", "subsession_id": subsession_id, "parent_view_id": parent_view_id, "result_summary": result_summary}

    def _list_notifications(self) -> dict:
        """List active notifications for this session (direct sync socket)."""
        session, error = self._get_session_for_tool()
        if error:
            return {"notifications": [], "error": str(error)}

        # Get view_id for session_id
        view_id = session.output.view.id() if session.output and session.output.view else None
        if not view_id:
            return {"notifications": [], "error": "Session has no view"}

        # Direct sync socket call to daemon
        import socket
        from pathlib import Path

        socket_path = str(Path.home() / ".notalone" / "notalone.sock")
        session_id = f"sublime.{view_id}"

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(socket_path)
            sock.sendall((json.dumps({
                "method": "list",
                "session_id": session_id
            }) + "\n").encode())

            data = b""
            while b"\n" not in data:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                data += chunk

            sock.close()
            resp = json.loads(data.decode().strip())
            return {"notifications": resp.get("notifications", [])}

        except FileNotFoundError:
            return {"notifications": [], "error": "notalone2 daemon not running"}
        except Exception as e:
            return {"notifications": [], "error": str(e)}

    def _discover_services(self) -> dict:
        """Discover available notification services from notalone2 daemon."""
        import socket
        import json
        from pathlib import Path

        # Builtin types with descriptions
        services = [
            {
                "type": "timer",
                "mode": "builtin",
                "description": "Wake after N seconds",
                "params": {"seconds": "int"}
            },
            {
                "type": "subsession",
                "mode": "builtin",
                "description": "Wake when subsession completes",
                "params": {"subsession_id": "string"}
            }
        ]

        # Query daemon for external services
        socket_path = str(Path.home() / ".notalone" / "notalone.sock")
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(socket_path)
            sock.sendall((json.dumps({"method": "services"}) + "\n").encode())

            data = b""
            while b"\n" not in data:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                data += chunk

            sock.close()
            resp = json.loads(data.decode().strip())

            # Transform daemon response to flat list
            daemon_services = resp.get("services", {})

            # Handle both dict format and list format from daemon
            if isinstance(daemon_services, dict):
                # Format: {"service-name": {"type.name": {"mode": "...", "description": "..."}}}
                for service_name, types in daemon_services.items():
                    if not isinstance(types, dict):
                        continue
                    endpoint = types.get("_endpoint")
                    for type_name, type_info in types.items():
                        if type_name.startswith("_") or not isinstance(type_info, dict):
                            continue
                        entry = {
                            "type": type_name,
                            "mode": type_info.get("mode", "notify"),
                            "description": type_info.get("description", ""),
                            "service": service_name
                        }
                        if endpoint:
                            entry["endpoint"] = endpoint
                        services.append(entry)
            elif isinstance(daemon_services, list):
                # Already flat list format from daemon
                for svc in daemon_services:
                    if isinstance(svc, dict) and "type" in svc:
                        services.append(svc)

            return {"services": services}

        except FileNotFoundError:
            return {"services": services, "error": "notalone2 daemon not running"}
        except Exception as e:
            return {"services": services, "error": str(e)}

    # ─── Order Table ───────────────────────────────────────────────────────

    def _order_table_cmd(self, action: str, **kwargs) -> str:
        """Dispatch order table commands."""
        from .order_table import get_table_for_cwd, refresh_order_table

        window = self._get_window()
        cwd = window.folders()[0] if window and window.folders() else None
        if not cwd:
            return "error: No project folder"

        table = get_table_for_cwd(cwd)
        agent_id = str(self._caller_view_id) if self._caller_view_id else None

        if action == "list":
            state = kwargs.get("state")
            orders = table.list(state)
            if not orders:
                return "No orders" + (f" ({state})" if state else "")
            lines = []
            for o in orders:
                loc = ""
                if o.get("file_path"):
                    loc = f" @ {o['file_path']}:{o.get('row', 0)+1}"
                if o["state"] == "done":
                    status = "✓"
                elif o.get("claimed_by"):
                    status = f"⏳({o['claimed_by']})"
                else:
                    status = "○"
                lines.append(f"{status} [{o['id']}]{loc} {o['prompt']}")
            return "\n".join(lines)

        elif action == "complete":
            order_id = kwargs.get("order_id")
            if not order_id:
                return "error: Missing order_id"
            ok, msg = table.complete(order_id, agent_id)
            if ok:
                sublime.set_timeout(lambda: refresh_order_table(window), 100)
            return f"✓ {order_id} done" if ok else f"error: {msg}"

        elif action == "subscribe":
            # Local subscription (no daemon needed)
            from .order_table import subscribe_to_orders
            wake_prompt = kwargs.get("wake_prompt") or "New order [{context[order_id]}]{context[location]}: {context[prompt]}"
            view_id = self._caller_view_id
            if not view_id:
                return "error: No session view"
            sub_id = subscribe_to_orders(cwd, view_id, wake_prompt)
            return f"Subscribed to orders (id: {sub_id})"

        elif action == "claim":
            order_id = kwargs.get("order_id")
            if not order_id:
                return "error: Missing order_id"
            if not agent_id:
                return "error: No agent context"
            ok, msg = table.claim(order_id, agent_id)
            if ok:
                sublime.set_timeout(lambda: refresh_order_table(window), 100)
            return f"✓ Claimed {order_id}" if ok else f"error: {msg}"

        elif action == "release":
            order_id = kwargs.get("order_id")
            if not order_id:
                return "error: Missing order_id"
            ok, msg = table.release(order_id, agent_id)
            if ok:
                sublime.set_timeout(lambda: refresh_order_table(window), 100)
            return f"✓ Released {order_id}" if ok else f"error: {msg}"

        else:
            return f"error: Unknown action: {action}"


# ============================================================================
# Chatroom functions
# ============================================================================

def _chatroom_command(req: dict) -> dict:
    """Send chatroom command to daemon."""
    socket_path = str(Path.home() / ".notalone" / "notalone.sock")
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(socket_path)
        sock.sendall((json.dumps(req) + "\n").encode())

        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk

        sock.close()
        return json.loads(data.decode().strip())
    except Exception as e:
        return {"error": str(e)}


def chatroom_list() -> dict:
    """List all chatrooms."""
    return _chatroom_command({"method": "chatroom_list"})


def chatroom_rooms_for_session(view_id: int) -> dict:
    """List rooms a session has joined."""
    return _chatroom_command({
        "method": "chatroom_rooms_for_session",
        "session_id": f"sublime.{view_id}"
    })


def chatroom_create(room_id: str = None, name: str = None, max_chars: int = 1000, prompt_hint: int = 500) -> dict:
    """Create a new chatroom."""
    req = {"method": "chatroom_create", "max_chars": max_chars, "prompt_hint": prompt_hint}
    if room_id:
        req["room_id"] = room_id
    if name:
        req["name"] = name
    return _chatroom_command(req)


def chatroom_join(view_id: int, room_id: str, role: str = "agent") -> dict:
    """Join a chatroom."""
    return _chatroom_command({
        "method": "chatroom_join",
        "room_id": room_id,
        "session_id": f"sublime.{view_id}",
        "role": role
    })


def chatroom_leave(view_id: int, room_id: str) -> dict:
    """Leave a chatroom."""
    return _chatroom_command({
        "method": "chatroom_leave",
        "room_id": room_id,
        "session_id": f"sublime.{view_id}"
    })


def chatroom_post(view_id: int, room_id: str, content: str) -> dict:
    """Post a message to a chatroom."""
    return _chatroom_command({
        "method": "chatroom_post",
        "room_id": room_id,
        "session_id": f"sublime.{view_id}",
        "content": content
    })


def chatroom_history(room_id: str, limit: int = 50, before_id: int = 0) -> dict:
    """Get chat history."""
    req = {"method": "chatroom_history", "room_id": room_id, "limit": limit}
    if before_id > 0:
        req["before_id"] = before_id
    return _chatroom_command(req)


def garage_search(query: str, k: int = 5) -> list:
    """Search indexed sessions with garage CLI."""
    import subprocess
    import re

    try:
        result = subprocess.run(
            ["garage", "search", query, "--k", str(k)],
            capture_output=True,
            text=True,
            timeout=10
        )
    except FileNotFoundError:
        return {"error": "garage CLI not found"}
    except subprocess.TimeoutExpired:
        return {"error": "garage search timed out"}

    # Parse output - handles both old and new format
    results = []
    lines = result.stdout.strip().split("\n")
    i = 0
    while i < len(lines):
        # New format: 1. [0.696] 2ccb865b  [pil]  Turns: 268
        new_match = re.match(r'\d+\.\s+\[([0-9.]+)\]\s+([a-f0-9]+)\s+\[([^\]]+)\]\s+Turns:\s*(\d+)', lines[i])
        if new_match:
            score = float(new_match.group(1))
            short_id = new_match.group(2)
            project = new_match.group(3)
            turns = int(new_match.group(4))
            summary = ""
            full_id = short_id
            while i + 1 < len(lines) and not re.match(r'\d+\.', lines[i + 1]):
                i += 1
                line = lines[i].strip()
                if line.startswith("- "):
                    summary = line[2:]
                elif line.startswith("ID: "):
                    full_id = line[4:]
            results.append({
                "session_id": full_id,
                "short_id": short_id,
                "score": score,
                "project": project,
                "turns": turns,
                "summary": summary,
            })
            i += 1
            continue

        # Old format: 1. [0.610] f400b570
        old_match = re.match(r'\d+\.\s+\[([0-9.]+)\]\s+([a-f0-9]+)', lines[i])
        if old_match:
            score = float(old_match.group(1))
            session_id = old_match.group(2)
            project = ""
            turns = 0
            while i + 1 < len(lines) and not re.match(r'\d+\.', lines[i + 1]):
                i += 1
                line = lines[i].strip()
                if line.startswith("Project:"):
                    project = line.replace("Project:", "").strip()
                elif "Turns:" in line:
                    try:
                        turns = int(line.split("Turns:")[-1].strip())
                    except:
                        pass
            results.append({
                "session_id": session_id,
                "short_id": session_id[:8],
                "score": score,
                "project": project,
                "turns": turns,
                "summary": "",
            })
        i += 1

    return results
