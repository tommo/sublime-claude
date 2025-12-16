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
    def __init__(self, window: sublime.Window, resume_id: Optional[str] = None, fork: bool = False, profile: Optional[Dict] = None):
        self.window = window
        self.client: Optional[JsonRpcClient] = None
        self.output = OutputView(window)
        self.initialized = False
        self.working = False
        self.current_tool: Optional[str] = None
        self.spinner_frame = 0
        # Session identity
        # When resuming (not forking), use resume_id as session_id immediately
        # so renames/saves work before first query completes
        self.session_id: Optional[str] = resume_id if resume_id and not fork else None
        self.resume_id: Optional[str] = resume_id  # ID to resume from
        self.fork: bool = fork  # Fork from resume_id instead of continuing it
        self.profile: Optional[Dict] = profile  # Profile config (model, betas, system_prompt, preload_docs)
        self.name: Optional[str] = None
        self.total_cost: float = 0.0
        self.query_count: int = 0
        # Pending context for next query
        self.pending_context: List[ContextItem] = []
        # Profile docs available for reading (paths only, not content)
        self.profile_docs: List[str] = []
        # Draft prompt (persists across input panel open/close)
        self.draft_prompt: str = ""
        # Track if we've entered input mode after last query
        self._input_mode_entered: bool = False

    def start(self) -> None:
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        python_path = settings.get("python_path", "python3")

        # Build profile docs list early (before init) so we can add to system prompt
        self._build_profile_docs_list()

        self.client = JsonRpcClient(self._on_notification)
        self.client.start([python_path, BRIDGE_SCRIPT])
        self._status("connecting...")

        permission_mode = settings.get("permission_mode", "acceptEdits")
        # In default mode, don't auto-allow any tools - prompt for all
        if permission_mode == "default":
            allowed_tools = []
        else:
            allowed_tools = settings.get("allowed_tools", [])

        print(f"[Claude] initialize: permission_mode={permission_mode}, allowed_tools={allowed_tools}, resume={self.resume_id}, fork={self.fork}, profile={self.profile}")
        init_params = {
            "cwd": self._cwd(),
            "allowed_tools": allowed_tools,
            "permission_mode": permission_mode,
        }
        if self.resume_id:
            init_params["resume"] = self.resume_id
            if self.fork:
                init_params["fork_session"] = True
        # Apply profile config
        if self.profile:
            if self.profile.get("model"):
                init_params["model"] = self.profile["model"]
            if self.profile.get("betas"):
                init_params["betas"] = self.profile["betas"]
            # Build system prompt with profile docs info
            system_prompt = self.profile.get("system_prompt", "")
            if self.profile_docs:
                docs_info = f"\n\nProfile Documentation: {len(self.profile_docs)} files available. Use list_profile_docs to see them and read_profile_doc(path) to read their contents."
                system_prompt = system_prompt + docs_info if system_prompt else docs_info.strip()
            if system_prompt:
                init_params["system_prompt"] = system_prompt
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
            error_msg = result['error'].get('message', str(result['error']))
            print(f"[Claude] init error: {error_msg}")
            self._status("error")

            # Show user-friendly message in view
            is_session_error = (
                "No conversation found" in error_msg or
                "Command failed" in error_msg
            )
            if is_session_error:
                self.output.text("\n*Session expired or not found.*\n\nUse `Claude: Restart Session` (Cmd+Shift+R) to start fresh.\n")
            else:
                self.output.text(f"\n*Failed to connect: {error_msg}*\n\nTry `Claude: Restart Session` (Cmd+Shift+R).\n")
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

    def _build_profile_docs_list(self) -> None:
        """Build list of available docs from profile preload_docs patterns (no reading yet)."""
        if not self.profile or not self.profile.get("preload_docs"):
            return

        import glob as glob_module

        patterns = self.profile["preload_docs"]
        if isinstance(patterns, str):
            patterns = [patterns]

        cwd = self._cwd()

        try:
            for pattern in patterns:
                # Make pattern relative to cwd
                full_pattern = os.path.join(cwd, pattern)
                for filepath in glob_module.glob(full_pattern, recursive=True):
                    if os.path.isfile(filepath):
                        rel_path = os.path.relpath(filepath, cwd)
                        self.profile_docs.append(rel_path)

            if self.profile_docs:
                print(f"[Claude] Profile docs available: {len(self.profile_docs)} files")
        except Exception as e:
            print(f"[Claude] preload_docs error: {e}")

    def add_context_file(self, path: str, content: str) -> None:
        """Add a file to pending context."""
        name = os.path.basename(path)
        self.pending_context.append(ContextItem("file", name, f"File: {path}\n```\n{content}\n```"))
        # print(f"[Claude] add_context_file: added {name}, pending_context={[c.name for c in self.pending_context]}")
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

        # Mark this session as the currently executing session for MCP tools
        # MCP tools should operate on the executing session, not the UI-active session
        # Only set if not already set (don't overwrite parent session when spawning subsessions)
        self._is_executing_session = False  # Track if we set the marker
        if self.output.view and not self.window.settings().has("claude_executing_view"):
            self.window.settings().set("claude_executing_view", self.output.view.id())
            self._is_executing_session = True
        # Build prompt with context
        full_prompt = self._build_prompt_with_context(prompt)
        context_names = [item.name for item in self.pending_context]
        self.pending_context = []  # Clear after use
        self._update_context_display()

        print(f"[Claude] >>> {prompt[:60]}...")
        self.output.show()

        # Check if bridge is alive before sending
        if not self.client.is_alive():
            self._status("error: bridge died")
            self.output.text("\n\n*Bridge process died. Please restart the session.*\n")
            return

        # Auto-name session from first prompt if not already named
        if not self.name:
            self._set_name(prompt[:30].strip() + ("..." if len(prompt) > 30 else ""))
        print(f"[Claude] query() - calling output.prompt()")
        self.output.prompt(prompt, context_names)
        print(f"[Claude] query() - calling _animate()")
        self._animate()
        print(f"[Claude] query() - sending RPC query request")
        if not self.client.send("query", {"prompt": full_prompt}, self._on_done):
            self._status("error: bridge died")
            self.working = False
            self.output.text("\n\n*Failed to send query. Bridge process died.*\n")
        else:
            print(f"[Claude] query() - RPC query sent successfully")

    def _on_done(self, result: dict) -> None:
        self.working = False
        self.current_tool = None

        # Clear executing session marker - MCP tools should no longer target this session
        # Only clear if this session was the one that set it (don't clear parent session's marker)
        if self.output.view and getattr(self, '_is_executing_session', False):
            self.window.settings().erase("claude_executing_view")
            self._is_executing_session = False

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

        # Clear any stale permission UI (query finished, no more permissions expected)
        self.output.clear_all_permissions()

        # Notify ALL bridges that this subsession completed (for alarm system)
        # Other sessions may be waiting on this subsession via alarms
        # Each session has its own bridge, so we need to broadcast to all
        if self.output.view:
            view_id = str(self.output.view.id())
            # Broadcast to all active sessions' bridges
            for session in sublime._claude_sessions.values():
                if session.client:
                    session.client.send("subsession_complete", {"subsession_id": view_id})

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

    def queue_prompt(self, prompt: str) -> None:
        """Inject a prompt into the current query stream."""
        print(f"[Claude] Injecting prompt: {prompt[:60]}...")
        self._status(f"injected: {prompt[:30]}...")

        # Show prompt in output view
        self.output.text(f"\n**[injected]** {prompt}\n\n")

        # Send to bridge to inject into active query
        if self.client:
            self.client.send("inject_message", {"message": prompt})

    def show_queue_input(self) -> None:
        """Show input panel to queue a prompt while session is working."""
        if not self.working:
            # Not working, just enter normal input mode
            self._enter_input_with_draft()
            return

        def on_done(text: str) -> None:
            text = text.strip()
            if text:
                self.queue_prompt(text)

        self.window.show_input_panel(
            "Queue prompt:",
            self.draft_prompt,
            on_done,
            None,  # on_change
            None   # on_cancel
        )

    def interrupt(self) -> None:
        if self.client:
            self.client.send("interrupt", {})
            self._status("interrupting...")
            self.working = False

    def stop(self) -> None:
        if self.client:
            self.client.send("shutdown", {}, lambda _: self.client.stop())
        self._clear_status()

    def set_alarm(self, event_type: str, event_params: dict, wake_prompt: str, alarm_id: Optional[str] = None, callback=None) -> None:
        """Set an alarm to wake this session when an event occurs.

        Instead of polling for events, the session sleeps and wakes when the event fires.
        The alarm "wakes" the session by injecting the wake_prompt.

        Args:
            event_type: "agent_complete", "time_elapsed", "subsession_complete"
            event_params: Event-specific parameters
                - agent_complete: {agent_id: str}
                - time_elapsed: {seconds: int}
                - subsession_complete: {subsession_id: str}
            wake_prompt: Prompt to inject when alarm fires
            alarm_id: Optional alarm identifier (generated if not provided)
            callback: Optional callback for result

        Returns (via callback):
            {alarm_id: str, status: "set", event_type: str}
        """
        if not self.client:
            return

        params = {
            "event_type": event_type,
            "event_params": event_params,
            "wake_prompt": wake_prompt,
        }
        if alarm_id:
            params["alarm_id"] = alarm_id

        self.client.send("set_alarm", params, callback)

    def cancel_alarm(self, alarm_id: str, callback=None) -> None:
        """Cancel a pending alarm.

        Args:
            alarm_id: Alarm identifier returned by set_alarm
            callback: Optional callback for result

        Returns (via callback):
            {alarm_id: str, status: "cancelled"}
        """
        if not self.client:
            return

        self.client.send("cancel_alarm", {"alarm_id": alarm_id}, callback)

    # ─── Notification Tools (notalone API) ────────────────────────────────

    def list_notifications(self, callback=None) -> None:
        """List active notifications for this session."""
        if not self.client:
            return
        self.client.send("list_notifications", {}, callback)

    def watch_ticket(self, ticket_id: int, states: list, wake_prompt: str, callback=None) -> None:
        """Watch a ticket for state changes in kanban (always remote)."""
        if not self.client:
            return

        params = {
            "ticket_id": ticket_id,
            "states": states,
            "wake_prompt": wake_prompt,
        }
        self.client.send("watch_ticket", params, callback)

    def subscribe_channel(self, channel: str, wake_prompt: str, callback=None) -> None:
        """Subscribe to a notification channel."""
        if not self.client:
            return

        params = {
            "channel": channel,
            "wake_prompt": wake_prompt,
        }
        self.client.send("subscribe_channel", params, callback)

    def broadcast_message(self, message: str, channel: str = None, data: dict = None, callback=None) -> None:
        """Broadcast a message to channel subscribers."""
        if not self.client:
            return

        params = {
            "message": message,
        }
        if channel:
            params["channel"] = channel
        if data:
            params["data"] = data

        self.client.send("broadcast_message", params, callback)

    def _on_notification(self, method: str, params: dict) -> None:
        if method == "permission_request":
            self._handle_permission_request(params)
            return

        if method == "queued_inject":
            # Injected prompt was queued because query completed too fast
            # Auto-submit it now
            message = params.get("message", "")
            if message:
                # Set working=True immediately to prevent input mode race condition
                # (notification handler runs on background thread, but _on_done schedules
                # input mode entry on main thread with 100ms delay)
                self.working = True
                print(f"[Claude] Auto-submitting queued inject: {message[:60]}...")
                self.query(message)
            return

        if method == "alarm_wake":
            # Alarm fired - start a new query with the wake prompt
            wake_prompt = params.get("wake_prompt", "")
            alarm_id = params.get("alarm_id", "")
            view_id = self.output.view.id() if self.output.view else "no-view"
            print(f"[Claude] ⏰ ALARM WAKE received by session: name='{self.name}', view_id={view_id}")
            print(f"[Claude] Alarm ID: {alarm_id}")
            print(f"[Claude] Wake prompt: {wake_prompt}")
            print(f"[Claude] Session state: initialized={self.initialized}, working={self.working}, client={self.client is not None}")

            # If session is still working, queue the wake query for when it becomes idle
            if self.working:
                print(f"[Claude] ⚠ Session is busy, deferring alarm wake until idle")

                def start_wake_query():
                    if not self.working:
                        print(f"[Claude] ✓ Session now idle, starting deferred alarm wake query")
                        try:
                            self.query(wake_prompt)
                        except Exception as e:
                            print(f"[Claude] ✗ ERROR starting deferred wake query: {e}")
                    else:
                        # Still working, try again later
                        sublime.set_timeout(start_wake_query, 500)

                sublime.set_timeout(start_wake_query, 500)
                return

            # Session is idle, start wake query immediately
            try:
                self.query(wake_prompt)
                print(f"[Claude] ✓ Alarm wake query started successfully")
            except Exception as e:
                print(f"[Claude] ✗ ERROR starting alarm wake query: {e}")
                import traceback
                traceback.print_exc()
            return

        if method != "message":
            return

        t = params.get("type")
        print(f"[Claude] notification: type={t}")
        if t == "tool_use":
            # Mark previous tool as done if any (skip if empty/anonymous)
            if self.current_tool and self.current_tool.strip():
                self.output.tool_done(self.current_tool)
            self.current_tool = params.get("name", "")

            # Skip anonymous/empty tool_use notifications
            if not self.current_tool or not self.current_tool.strip():
                self.current_tool = None
                return

            tool_input = params.get("input", {})
            print(f"[Claude] tool_use input: {tool_input}")
            self.output.tool(self.current_tool, tool_input)
        elif t == "tool_result":
            # Skip anonymous/empty tool results
            if not self.current_tool or not self.current_tool.strip():
                self.current_tool = None
                return

            content = params.get("content", "")
            # Convert content to string if it's a list
            if isinstance(content, list):
                content = "\n".join(str(c) for c in content)
            if params.get("is_error"):
                self.output.tool_error(self.current_tool, content)
            else:
                self.output.tool_done(self.current_tool, content)
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
        status = self.current_tool or "thinking..."
        self._status(f"{s} {status}")
        # Animate spinner in output view
        self.output.advance_spinner()
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
