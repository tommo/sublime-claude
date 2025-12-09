"""Claude Code session management."""
import json
import os
from typing import Optional, List, Dict

import sublime

from .rpc import JsonRpcClient
from .output import OutputView


BRIDGE_SCRIPT = os.path.join(os.path.dirname(__file__), "bridge", "main.py")
SESSIONS_FILE = os.path.join(os.path.dirname(__file__), ".sessions.json")


def load_saved_sessions() -> List[Dict]:
    """Load saved sessions from disk."""
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return []


def save_sessions(sessions: List[Dict]) -> None:
    """Save sessions to disk."""
    try:
        with open(SESSIONS_FILE, "w") as f:
            json.dump(sessions, f, indent=2)
    except Exception as e:
        print(f"[Claude] Failed to save sessions: {e}")


class ContextItem:
    """A pending context item to attach to next query."""
    def __init__(self, kind: str, name: str, content: str):
        self.kind = kind  # "file", "selection"
        self.name = name  # Display name
        self.content = content  # Actual content


class Session:
    def __init__(self, window: sublime.Window, resume_id: Optional[str] = None, fork: bool = False):
        self.window = window
        self.client: Optional[JsonRpcClient] = None
        self.output = OutputView(window)
        self.initialized = False
        self.working = False
        self.current_tool: Optional[str] = None
        self.spinner_frame = 0
        # Session identity
        self.session_id: Optional[str] = None
        self.resume_id: Optional[str] = resume_id  # ID to resume from
        self.fork: bool = fork  # Fork from resume_id instead of continuing it
        self.name: Optional[str] = None
        self.total_cost: float = 0.0
        self.query_count: int = 0
        # Pending context for next query
        self.pending_context: List[ContextItem] = []
        # Draft prompt (persists across input panel open/close)
        self.draft_prompt: str = ""
        # Track if we've entered input mode after last query
        self._input_mode_entered: bool = False

    def start(self) -> None:
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        python_path = settings.get("python_path", "python3")

        self.client = JsonRpcClient(self._on_notification)
        self.client.start([python_path, BRIDGE_SCRIPT])
        self._status("connecting...")

        permission_mode = settings.get("permission_mode", "acceptEdits")
        # In default mode, don't auto-allow any tools - prompt for all
        if permission_mode == "default":
            allowed_tools = []
        else:
            allowed_tools = settings.get("allowed_tools", [])

        print(f"[Claude] initialize: permission_mode={permission_mode}, allowed_tools={allowed_tools}, resume={self.resume_id}, fork={self.fork}")
        init_params = {
            "cwd": self._cwd(),
            "allowed_tools": allowed_tools,
            "permission_mode": permission_mode,
        }
        if self.resume_id:
            init_params["resume"] = self.resume_id
            if self.fork:
                init_params["fork_session"] = True
        self.client.send("initialize", init_params, self._on_init)

    def _cwd(self) -> str:
        if self.window.folders():
            return self.window.folders()[0]
        view = self.window.active_view()
        if view and view.file_name():
            return os.path.dirname(view.file_name())
        return os.getcwd()

    def _on_init(self, result: dict) -> None:
        if "error" in result:
            self._status(f"error: {result['error']['message']}")
            return
        self.initialized = True
        self._input_mode_entered = False  # Reset for fresh start after init
        # Show loaded MCP servers and agents
        mcp_servers = result.get("mcp_servers", [])
        agents = result.get("agents", [])
        parts = []
        if mcp_servers:
            print(f"[Claude] MCP servers: {mcp_servers}")
            parts.append(f"MCP: {', '.join(mcp_servers)}")
        if agents:
            print(f"[Claude] Agents: {agents}")
            parts.append(f"agents: {', '.join(agents)}")
        if parts:
            self._status(f"ready ({'; '.join(parts)})")
        else:
            self._status("ready")
        # Auto-enter input mode when ready
        self._enter_input_with_draft()

    def add_context_file(self, path: str, content: str) -> None:
        """Add a file to pending context."""
        name = os.path.basename(path)
        self.pending_context.append(ContextItem("file", name, f"File: {path}\n```\n{content}\n```"))
        self._update_context_display()

    def add_context_selection(self, path: str, content: str) -> None:
        """Add a selection to pending context."""
        name = os.path.basename(path) if path else "selection"
        self.pending_context.append(ContextItem("selection", name, f"Selection from {path}:\n```\n{content}\n```"))
        self._update_context_display()

    def add_context_folder(self, path: str) -> None:
        """Add a folder path to pending context."""
        name = os.path.basename(path) + "/"
        self.pending_context.append(ContextItem("folder", name, f"Folder: {path}"))
        self._update_context_display()

    def clear_context(self) -> None:
        """Clear pending context."""
        self.pending_context = []
        self._update_context_display()

    def _update_context_display(self) -> None:
        """Update output view with pending context."""
        self.output.set_pending_context(self.pending_context)

    def _build_prompt_with_context(self, prompt: str) -> str:
        """Build full prompt with pending context."""
        if not self.pending_context:
            return prompt
        parts = [item.content for item in self.pending_context]
        parts.append(prompt)
        return "\n\n".join(parts)

    def query(self, prompt: str) -> None:
        if not self.client or not self.initialized:
            sublime.error_message("Claude not initialized")
            return

        self.working = True
        self.query_count += 1
        self._input_mode_entered = False  # Reset so input mode can be entered when query completes
        # Build prompt with context
        full_prompt = self._build_prompt_with_context(prompt)
        context_names = [item.name for item in self.pending_context]
        self.pending_context = []  # Clear after use
        self._update_context_display()

        print(f"[Claude] >>> {prompt[:60]}...")
        self.output.show()
        # Auto-name session from first prompt if not already named
        if not self.name:
            self._set_name(prompt[:30].strip() + ("..." if len(prompt) > 30 else ""))
        self.output.prompt(prompt, context_names)
        self._animate()
        self.client.send("query", {"prompt": full_prompt}, self._on_done)

    def _on_done(self, result: dict) -> None:
        self.working = False
        self.current_tool = None
        status = result.get("status", "")
        if "error" in result:
            self._status("error")
            # Mark conversation as done on error
            if self.output.current:
                self.output.current.working = False
                self.output._render_current()
        elif status == "interrupted":
            self._status("interrupted")
            self.output.interrupted()
        else:
            self._status("done")
            sublime.set_timeout(lambda: self._status("ready") if not self.working else None, 2000)
        # Update view title to reflect idle state
        self.output.set_name(self.name or "Claude")
        # Auto-enter input mode when idle
        sublime.set_timeout(lambda: self._enter_input_with_draft() if not self.working else None, 100)

    def _enter_input_with_draft(self) -> None:
        """Enter input mode and restore draft with cursor at end."""
        # Skip if already in input mode or session is working
        if self.output.is_input_mode() or self.working:
            return

        # Skip if we've already entered input mode after the last query
        # This prevents duplicate entries from multiple callers (on_activated, _on_done, etc.)
        if self._input_mode_entered:
            return

        self.output.enter_input_mode()

        # Check if enter_input_mode actually succeeded (might have deferred)
        if not self.output.is_input_mode():
            return

        self._input_mode_entered = True

        if self.draft_prompt and self.output.view:
            self.output.view.run_command("append", {"characters": self.draft_prompt})
            end = self.output.view.size()
            self.output.view.sel().clear()
            self.output.view.sel().add(sublime.Region(end, end))

    def interrupt(self) -> None:
        if self.client:
            self.client.send("interrupt", {})
            self._status("interrupting...")
            self.working = False

    def stop(self) -> None:
        if self.client:
            self.client.send("shutdown", {}, lambda _: self.client.stop())
        self._clear_status()

    def _on_notification(self, method: str, params: dict) -> None:
        if method == "permission_request":
            self._handle_permission_request(params)
            return

        if method != "message":
            return

        t = params.get("type")
        print(f"[Claude] notification: type={t}")
        if t == "tool_use":
            # Mark previous tool as done if any
            if self.current_tool:
                self.output.tool_done(self.current_tool)
            self.current_tool = params.get("name", "")
            tool_input = params.get("input", {})
            print(f"[Claude] tool_use input: {tool_input}")
            self.output.tool(self.current_tool, tool_input)
        elif t == "tool_result":
            content = params.get("content", "")
            # Convert content to string if it's a list
            if isinstance(content, list):
                content = "\n".join(str(c) for c in content)
            if params.get("is_error"):
                self.output.tool_error(self.current_tool or "tool", content)
            else:
                self.output.tool_done(self.current_tool or "tool", content)
            self.current_tool = None
        elif t == "text":
            self.output.text(params.get("text", ""))
        elif t == "result":
            # Capture session ID for resume
            if params.get("session_id"):
                self.session_id = params["session_id"]
                self._save_session()
            cost = params.get("total_cost_usd") or 0
            self.total_cost += cost
            dur = params.get("duration_ms", 0) / 1000
            print(f"[Claude] [{dur:.1f}s, ${cost:.4f}]" if cost else f"[Claude] [{dur:.1f}s]")
            self.output.meta(dur, cost)
            self._update_status_bar()

    def _set_name(self, name: str) -> None:
        """Set session name and update UI."""
        self.name = name
        self.output.set_name(name)
        self._update_status_bar()
        self._save_session()

    def _save_session(self) -> None:
        """Save session info to disk for later resume."""
        if not self.session_id:
            return
        sessions = load_saved_sessions()
        # Update or add this session
        found = False
        for s in sessions:
            if s.get("session_id") == self.session_id:
                s["name"] = self.name
                s["project"] = self._cwd()
                s["total_cost"] = self.total_cost
                s["query_count"] = self.query_count
                found = True
                break
        if not found:
            sessions.insert(0, {
                "session_id": self.session_id,
                "name": self.name,
                "project": self._cwd(),
                "total_cost": self.total_cost,
                "query_count": self.query_count,
            })
        # Keep only last 20 sessions
        sessions = sessions[:20]
        save_sessions(sessions)

    def _status(self, text: str) -> None:
        """Update status on output view only."""
        if self.output.view and self.output.view.is_valid():
            self.output.view.set_status("claude", f"Claude: {text}")

    def _update_status_bar(self) -> None:
        """Update status bar with session info on output view."""
        if not self.output.view or not self.output.view.is_valid():
            return
        parts = []
        if self.name:
            parts.append(self.name)
        if self.total_cost > 0:
            parts.append(f"${self.total_cost:.4f}")
        if self.query_count > 0:
            parts.append(f"{self.query_count}q")
        status = " | ".join(parts) if parts else "Claude"
        self.output.view.set_status("claude_session", f"Claude: {status}")

    def _clear_status(self) -> None:
        if self.output.view and self.output.view.is_valid():
            self.output.view.erase_status("claude")
            self.output.view.erase_status("claude_session")

    def _animate(self) -> None:
        if not self.working:
            # Restore normal title when done
            self.output.set_name(self.name or "Claude")
            return
        chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        s = chars[self.spinner_frame % len(chars)]
        self.spinner_frame += 1
        # Show spinner in status bar only (not title - causes cursor flicker)
        self._status(f"{s} {self.current_tool or 'thinking...'}")
        sublime.set_timeout(self._animate, 100)

    def _handle_permission_request(self, params: dict) -> None:
        """Handle permission request from bridge - show in output view."""
        from .output import PERM_ALLOW

        pid = params.get("id")
        tool = params.get("tool", "Unknown")
        tool_input = params.get("input", {})
        print(f"[Claude] _handle_permission_request: pid={pid}, tool={tool}")
        print(f"[Claude] output.pending_permission={self.output.pending_permission}")

        def on_response(response: str) -> None:
            if self.client:
                allow = (response == PERM_ALLOW)
                if not allow:
                    # Mark tool as error immediately - SDK won't send tool_result for denied
                    self.output.tool_error(tool)
                    self.current_tool = None
                self.client.send("permission_response", {
                    "id": pid,
                    "allow": allow,
                    "input": tool_input if allow else None,
                    "message": None if allow else "User denied permission",
                })

        # Show permission UI in output view
        self.output.permission_request(pid, tool, tool_input, on_response)
