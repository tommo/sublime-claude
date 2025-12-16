"""MCP socket server for Sublime Text integration."""
import json
import os
import socket
import threading
import time

import sublime
import sublime_plugin

from .settings import load_profiles_and_checkpoints

SOCKET_PATH = "/tmp/sublime_claude_mcp.sock"

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

            result = {"result": None, "error": None}
            done = threading.Event()

            # Check if this is an ask_user call - needs special async handling
            is_ask_user = "ask_user(" in code

            def do_eval():
                try:
                    if is_ask_user:
                        # Special handling: ask_user needs to NOT block main thread
                        # We'll call the method which will show UI and set done when complete
                        result["result"] = self._eval_ask_user(code, done)
                    else:
                        result["result"] = self._eval(code, tool)
                        done.set()
                except Exception as e:
                    result["error"] = str(e)
                    done.set()

            sublime.set_timeout(do_eval, 0)
            # ask_user needs longer timeout (5 min), others use 30s
            timeout = 300 if is_ask_user else 30
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

    def _eval_ask_user(self, code: str, done_event: threading.Event):
        """Special eval for ask_user - handles async UI without blocking main thread."""
        # Parse the ask_user call to extract args
        # Expected format: return ask_user("question", ["opt1", "opt2"])
        import re
        match = re.search(r'ask_user\((.*)\)', code, re.DOTALL)
        if not match:
            done_event.set()
            return {"error": "Invalid ask_user call"}

        # Safely evaluate the arguments
        args_str = match.group(1)
        try:
            # Use ast.literal_eval for safety
            import ast
            # Wrap in tuple to parse multiple args
            args = ast.literal_eval(f"({args_str})")
            question = args[0] if len(args) > 0 else ""
            options = args[1] if len(args) > 1 else []
        except:
            done_event.set()
            return {"error": "Failed to parse ask_user arguments"}

        window = sublime.active_window()
        if not window:
            done_event.set()
            return {"error": "No window"}

        # Build items for quick panel
        items = []
        for opt in options:
            items.append([str(opt), ""])
        items.append(["Other...", "Type a custom response"])

        result = {"answer": None, "cancelled": False}

        def on_select(idx):
            if idx == -1:
                result["cancelled"] = True
                done_event.set()
            elif idx == len(options):
                # "Other" selected - show input panel after a delay
                # (immediate show can be dismissed by quick panel closing)
                def show_input():
                    def on_input(text):
                        result["answer"] = text
                        done_event.set()

                    def on_cancel():
                        result["cancelled"] = True
                        done_event.set()

                    window.show_input_panel(
                        question,
                        "",
                        on_input,
                        None,
                        on_cancel
                    )
                sublime.set_timeout(show_input, 50)
            else:
                result["answer"] = options[idx]
                done_event.set()

        window.show_quick_panel(
            items,
            on_select,
            placeholder=question
        )

        # Don't wait here - return the result dict that will be filled async
        # The socket handler waits on done_event
        return result

    def _eval(self, code: str, tool: str = None):
        """Execute code in Sublime's context."""
        # Load saved tool if specified
        if tool:
            window = sublime.active_window()
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
            # Alarm tools
            "set_alarm": self._set_alarm,
            "cancel_alarm": self._cancel_alarm,
            # Notification tools (notalone API)
            "list_notifications": self._list_notifications,
            "watch_ticket": self._watch_ticket,
            "subscribe_channel": self._subscribe_channel,
            "broadcast_message": self._broadcast_message,
        }

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
        window = sublime.active_window()
        if not window:
            return []
        return [v.file_name() for v in window.views() if v.file_name()]

    def _get_window_summary(self) -> dict:
        """Get summary of current window state."""
        window = sublime.active_window()
        if not window:
            return {"success": False, "error": "No window"}

        # Get all open files
        open_files = []
        for view in window.views():
            file_path = view.file_name()
            if file_path:
                open_files.append({
                    "path": file_path,
                    "is_dirty": view.is_dirty(),
                    "size": view.size(),
                })

        # Get active file with selection info
        active_view = window.active_view()
        active_file = None
        if active_view and active_view.file_name():
            active_file = {
                "path": active_view.file_name(),
                "line_count": active_view.rowcol(active_view.size())[0] + 1,
                "selection": [
                    {
                        "start": active_view.rowcol(sel.begin()),
                        "end": active_view.rowcol(sel.end()),
                    }
                    for sel in active_view.sel()
                ],
            }

        # Get project folders
        folders = window.folders()

        # Get layout info
        layout = window.layout()
        num_groups = window.num_groups()

        return {
            "success": True,
            "open_files_count": len(open_files),
            "open_files": open_files,
            "active_file": active_file,
            "project_folders": folders,
            "layout": {
                "groups": num_groups,
                "cells": layout.get("cells", []),
            },
        }

    def _find_file(self, query: str, pattern: str = None, limit: int = 20) -> list:
        """Fuzzy find files by name, optionally filtered by glob pattern."""
        import fnmatch

        window = sublime.active_window()
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
        window = sublime.active_window()
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

        results = {}
        counts = {}

        for sym in symbols:
            locations = window.lookup_symbol_in_index(sym)

            # Filter by file_path if provided
            if file_path:
                locations = [loc for loc in locations if loc[0] == file_path]

            counts[sym] = len(locations)

            per_sym = []
            for loc in locations[:limit] if limit > 0 else locations:
                fp, display_name, (row, col) = loc[0], loc[1], loc[2]
                per_sym.append({
                    "name": display_name,
                    "file": fp,
                    "row": row,
                    "col": col,
                })
            results[sym] = per_sym

        return {
            "success": True,
            "results": results,
            "counts": counts,
            "limit": limit,
        }

    def _list_tools(self) -> list:
        """List available saved tools."""
        window = sublime.active_window()
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
        window = sublime.active_window()
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

    def _read_view(self, file_path: str = None, view_name: str = None, head: int = None, tail: int = None, grep: str = None, grep_i: str = None) -> dict:
        """Read content from any view by file path or view name with head/tail/grep filtering."""
        import os
        import re
        window = sublime.active_window()
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

        result = {
            "content": content,
            "size": len(content),
            "line_count": len(all_lines),
            "original_line_count": original_line_count,
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
        """List available profiles and checkpoints."""
        project_path = _get_project_profiles_path()
        profiles, checkpoints = load_profiles_and_checkpoints(project_path)

        result = {
            "profiles": [],
            "checkpoints": [],
            "paths": {
                "user": "~/.claude-sublime/profiles.json",
                "project": project_path or "(no project)",
            }
        }

        for name, config in profiles.items():
            result["profiles"].append({
                "name": name,
                "model": config.get("model", "default"),
                "description": config.get("description", ""),
                "has_preload_docs": bool(config.get("preload_docs")),
                "betas": config.get("betas", []),
            })

        for name, config in checkpoints.items():
            result["checkpoints"].append({
                "name": name,
                "session_id": config.get("session_id", "")[:8] + "...",
                "description": config.get("description", ""),
            })

        return result

    def _spawn_session(self, prompt: str, name: str = None, profile: str = None, checkpoint: str = None, fork_current: bool = False, wait_for_completion: bool = False) -> dict:
        """Spawn a new Claude session with the given prompt. Returns with _wait_for_init flag."""
        from .core import create_session, get_active_session

        window = sublime.active_window()
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

        # Fork from current session if requested
        if fork_current:
            current_session = get_active_session(window)
            if current_session and current_session.session_id:
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

        # Create new session
        session = create_session(window, resume_id=resume_id, fork=fork, profile=profile_config)
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

    def _list_sessions(self) -> list:
        """List all active sessions across all windows."""

        result = []
        for view_id, session in sublime._claude_sessions.items():
            result.append({
                "view_id": view_id,
                "name": session.name or "(unnamed)",
                "working": session.working,
                "query_count": session.query_count,
                "total_cost": session.total_cost,
                "window_id": session.window.id() if session.window else None,
            })
        return result

    def _read_session_output(self, view_id: int, lines: int = None) -> dict:
        """Read conversation output from a session's view."""

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

        return {
            "view_id": view_id,
            "name": session.name or "(unnamed)",
            "working": session.working,
            "output": content,
            "line_count": content.count('\n') + 1 if content else 0,
        }

    def _list_profile_docs(self) -> dict:
        """List documentation files available from current session's profile."""
        window = sublime.active_window()
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
        window = sublime.active_window()
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
        window = sublime.active_window()
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

    def _terminus_run(self, command: str, tag: str = None, wait: float = 0, target_id: str = None) -> dict:
        """Run command in terminal, optionally wait and return output.

        Args:
            command: Command to run (newline appended if missing)
            tag: Terminal tag (default: claude-agent)
            wait: Seconds to wait before reading output (0 = don't wait/read)
            target_id: Optional terminal ID for sharing across sessions

        Returns:
            If wait=0: just confirmation of send
            If wait>0: send confirmation + terminal output after waiting
        """
        # Priority: target_id > tag > session-specific default
        if target_id:
            tag = f"claude-agent-{target_id}"
        elif not tag:
            # Default tag uses active Claude session's view ID for isolation
            # Each session gets its own terminal to avoid state pollution
            from . import core
            window = sublime.active_window()
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
        window = sublime.active_window()
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
            window = sublime.active_window()
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
            window = sublime.active_window()
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
                return None, {"error": f"Session not found: {session_id}"}
            return sublime._claude_sessions[session_id], None

        # Try to find session from execution context
        window = sublime.active_window()
        if not window:
            return None, {"error": "No active window"}

        # Only trust claude_executing_view (set during internal tool execution)
        view_id = window.settings().get("claude_executing_view")

        if not view_id or view_id not in sublime._claude_sessions:
            # No reliable session context - require explicit session_id
            available = list(sublime._claude_sessions.keys())
            return None, {
                "error": "No session context available",
                "hint": "Specify session_id parameter explicitly. Get available sessions with list_sessions()",
                "available_sessions": available
            }

        return sublime._claude_sessions[view_id], None

    # ─── Alarm Tools ──────────────────────────────────────────────────────

    def _set_alarm(self, event_type: str, event_params: dict, wake_prompt: str, alarm_id: str = None, session_id: int = None) -> dict:
        """Set an alarm to wake current session when an event occurs.

        Instead of polling, the session sleeps and wakes when the event fires.

        Args:
            event_type: "subsession_complete", "time_elapsed", "agent_complete"
            event_params: Event-specific parameters
                - subsession_complete: {subsession_id: str}
                - time_elapsed: {seconds: int}
                - agent_complete: {agent_id: str}
            wake_prompt: Prompt to inject when alarm fires
            alarm_id: Optional alarm identifier (generated if not provided)

        Returns:
            {alarm_id: str, status: "set", event_type: str}
        """
        session, error = self._get_session_for_tool(session_id)
        if error:
            return error

        # Set alarm on the session
        result = {"pending": True}

        def on_result(alarm_result):
            result.update(alarm_result)

        session.set_alarm(
            event_type=event_type,
            event_params=event_params,
            wake_prompt=wake_prompt,
            alarm_id=alarm_id,
            callback=on_result
        )

        # Return immediately (alarm is async)
        return {
            "status": "alarm_set",
            "event_type": event_type,
            "note": "Alarm will fire asynchronously when event occurs"
        }

    def _cancel_alarm(self, alarm_id: str, session_id: int = None) -> dict:
        """Cancel a pending alarm.

        Args:
            alarm_id: Alarm identifier from set_alarm

        Returns:
            {alarm_id: str, status: "cancelled"}
        """
        session, error = self._get_session_for_tool(session_id)
        if error:
            return error

        # Cancel alarm
        result = {"pending": True}

        def on_result(cancel_result):
            result.update(cancel_result)

        session.cancel_alarm(alarm_id, callback=on_result)

        return {
            "status": "cancel_requested",
            "alarm_id": alarm_id
        }

    # ─── Notification Tools (notalone API) ────────────────────────────────

    def _list_notifications(self, session_id: int = None) -> dict:
        """List active notifications for current session."""
        session, error = self._get_session_for_tool(session_id)
        if error:
            return error

        result = {"pending": True, "notifications": []}

        def on_result(list_result):
            result.update(list_result)

        session.list_notifications(callback=on_result)

        return result

    def _watch_ticket(self, ticket_id: int, states: list, wake_prompt: str, remote_url: str = None, session_id: int = None) -> dict:
        """Watch a ticket for state changes (local or remote)."""
        session, error = self._get_session_for_tool(session_id)
        if error:
            return error

        result = {"pending": True}

        def on_result(watch_result):
            result.update(watch_result)

        session.watch_ticket(
            ticket_id=ticket_id,
            states=states,
            wake_prompt=wake_prompt,
            remote_url=remote_url,
            callback=on_result
        )

        return {
            "status": "watching_remote" if remote_url else "watching",
            "ticket_id": ticket_id,
            "states": states,
            "remote_url": remote_url
        }

    def _watch_ticket_remote(self, remote_url: str, ticket_id: int, states: list, wake_prompt: str, session_id: int = None) -> dict:
        """Watch a ticket on a remote system for state changes via notalone RPC."""
        session, error = self._get_session_for_tool(session_id)
        if error:
            return error

        result = {"pending": True}

        def on_result(watch_result):
            result.update(watch_result)

        session.watch_ticket_remote(
            remote_url=remote_url,
            ticket_id=ticket_id,
            states=states,
            wake_prompt=wake_prompt,
            callback=on_result
        )

        return {
            "status": "registering_remote",
            "remote_url": remote_url,
            "ticket_id": ticket_id,
            "states": states
        }

    def _subscribe_channel(self, channel: str, wake_prompt: str, session_id: int = None) -> dict:
        """Subscribe to a notification channel."""
        session, error = self._get_session_for_tool(session_id)
        if error:
            return error

        result = {"pending": True}

        def on_result(sub_result):
            result.update(sub_result)

        session.subscribe_channel(
            channel=channel,
            wake_prompt=wake_prompt,
            callback=on_result
        )

        return {
            "status": "subscribed",
            "channel": channel
        }

    def _broadcast_message(self, message: str, channel: str = None, data: dict = None, session_id: int = None) -> dict:
        """Broadcast a message to channel subscribers."""
        session, error = self._get_session_for_tool(session_id)
        if error:
            return error

        result = {"pending": True}

        def on_result(broadcast_result):
            result.update(broadcast_result)

        session.broadcast_message(
            message=message,
            channel=channel,
            data=data or {},
            callback=on_result
        )

        return {
            "status": "broadcast_sent",
            "channel": channel or "global",
            "message": message
        }
