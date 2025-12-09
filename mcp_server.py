"""MCP socket server for Sublime Text integration."""
import json
import os
import socket
import threading
import time

import sublime
import sublime_plugin

SOCKET_PATH = "/tmp/sublime_claude_mcp.sock"

_server = None
_blackboard: dict = {}  # Shared blackboard across sessions


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
            "get_symbols": self._get_symbols,
            "goto_symbol": self._goto_symbol,
            "list_tools": self._list_tools,
            # Blackboard
            "bb_write": self._bb_write,
            "bb_read": self._bb_read,
            "bb_delete": self._bb_delete,
            "bb_list": self._bb_list,
            "bb_clear": self._bb_clear,
            # Session tools
            "spawn_session": self._spawn_session,
            "send_to_session": self._send_to_session,
            "list_sessions": self._list_sessions,
            # Terminus tools
            "terminus_list": self._terminus_list,
            "terminus_send": self._terminus_send,
            "terminus_read": self._terminus_read,
            "terminus_close": self._terminus_close,
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

    def _get_symbols(self, query: str = "", file_path: str = None) -> list:
        """Get symbols from index."""
        window = sublime.active_window()
        if not window:
            return []

        locations = window.lookup_symbol_in_index(query)
        results = []
        for loc in locations[:500]:
            if file_path and loc[0] != file_path:
                continue
            results.append({
                "name": loc[1],
                "file": loc[0],
                "row": loc[2][0],
                "col": loc[2][1]
            })
        return results

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

    # ─── Blackboard ───────────────────────────────────────────────────────

    def _bb_write(self, key: str, value) -> dict:
        """Write a value to the shared blackboard."""
        global _blackboard
        _blackboard[key] = {
            "value": value,
            "timestamp": time.time(),
        }
        return {"key": key, "written": True}

    def _bb_read(self, key: str) -> dict:
        """Read a value from the blackboard."""
        if key in _blackboard:
            entry = _blackboard[key]
            return {"key": key, "value": entry["value"], "timestamp": entry["timestamp"]}
        return {"key": key, "value": None, "error": "Key not found"}

    def _bb_delete(self, key: str) -> dict:
        """Delete a key from the blackboard."""
        global _blackboard
        if key in _blackboard:
            del _blackboard[key]
            return {"key": key, "deleted": True}
        return {"key": key, "deleted": False, "error": "Key not found"}

    def _bb_list(self) -> list:
        """List all keys in the blackboard with previews."""
        result = []
        for key, entry in _blackboard.items():
            value = entry["value"]
            # Preview: truncate long values
            if isinstance(value, str):
                preview = value[:100] + "..." if len(value) > 100 else value
            else:
                preview = str(value)[:100]
            result.append({
                "key": key,
                "preview": preview,
                "timestamp": entry["timestamp"],
            })
        return result

    def _bb_clear(self) -> dict:
        """Clear all blackboard entries."""
        global _blackboard
        count = len(_blackboard)
        _blackboard = {}
        return {"cleared": count}

    # ─── Session Spawn ────────────────────────────────────────────────────

    def _spawn_session(self, prompt: str, name: str = None) -> dict:
        """Spawn a new Claude session with the given prompt."""
        window = sublime.active_window()
        if not window:
            return {"error": "No window"}

        # Import here to avoid circular import
        from . import claude_code

        # Create new session
        session = claude_code.create_session(window)
        if name:
            session.name = name
            session.output.set_name(name)

        # Queue the prompt to run after initialization
        def send_prompt():
            if session.initialized:
                session.query(prompt)
            else:
                sublime.set_timeout(send_prompt, 200)

        sublime.set_timeout(send_prompt, 300)

        view_id = session.output.view.id() if session.output.view else None
        return {
            "spawned": True,
            "name": name or "(unnamed)",
            "view_id": view_id,
        }

    def _send_to_session(self, view_id: int, prompt: str) -> dict:
        """Send a message to an existing session."""
        from . import claude_code

        session = claude_code._sessions.get(view_id)
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
        """List all active sessions in the current window."""
        window = sublime.active_window()
        if not window:
            return []

        from . import claude_code

        result = []
        for view_id, session in claude_code._sessions.items():
            if session.window == window:
                result.append({
                    "view_id": view_id,
                    "name": session.name or "(unnamed)",
                    "working": session.working,
                    "query_count": session.query_count,
                    "total_cost": session.total_cost,
                })
        return result

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

    def _terminus_send(self, text: str, tag: str = None) -> dict:
        """Send text/command to a Terminus terminal."""
        window = sublime.active_window()
        if not window:
            return {"error": "No window"}

        # Find matching terminal to get its info
        target = None
        for view in window.views():
            if view.settings().get("terminus_view"):
                if tag:
                    if view.settings().get("terminus_view.tag") == tag:
                        target = view
                        break
                else:
                    # Use first terminal if no tag specified
                    target = view
                    break

        # Open a new terminal if none found
        if not target:
            cwd = window.folders()[0] if window.folders() else None
            open_args = {"panel_name": None}  # None = open as view/tab, not panel
            if cwd:
                open_args["cwd"] = cwd
            if tag:
                open_args["tag"] = tag
            window.run_command("terminus_open", open_args)
            # Find the newly created terminal
            for view in window.views():
                if view.settings().get("terminus_view"):
                    target = view
                    break
            if not target:
                return {"error": "Failed to open terminal"}

        # Send using Terminus command on WINDOW (not view)
        args = {"string": text}
        target_tag = target.settings().get("terminus_view.tag", "")
        if target_tag:
            args["tag"] = target_tag
        window.run_command("terminus_send_string", args)
        return {
            "sent": True,
            "view_id": target.id(),
            "tag": target_tag,
        }

    def _terminus_read(self, tag: str = None, lines: int = 100) -> dict:
        """Read output from a Terminus terminal."""
        window = sublime.active_window()
        if not window:
            return {"error": "No window"}

        # Find matching terminal
        target = None
        for view in window.views():
            if view.settings().get("terminus_view"):
                if tag:
                    if view.settings().get("terminus_view.tag") == tag:
                        target = view
                        break
                else:
                    target = view
                    break

        if not target:
            return {"error": f"No terminal found" + (f" with tag '{tag}'" if tag else "")}

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

    def _terminus_close(self, tag: str = None) -> dict:
        """Close a Terminus terminal."""
        window = sublime.active_window()
        if not window:
            return {"error": "No window"}

        # Find matching terminal
        target = None
        for view in window.views():
            if view.settings().get("terminus_view"):
                if tag:
                    if view.settings().get("terminus_view.tag") == tag:
                        target = view
                        break
                else:
                    target = view
                    break

        if not target:
            return {"error": f"No terminal found" + (f" with tag '{tag}'" if tag else "")}

        view_id = target.id()
        tag_val = target.settings().get("terminus_view.tag", "")

        # Close the terminal
        target.close()

        return {
            "closed": True,
            "view_id": view_id,
            "tag": tag_val,
        }
