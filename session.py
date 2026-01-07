"""Claude Code session management."""
import json
import os
from typing import Optional, List, Dict, Callable

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
    def __init__(self, window: sublime.Window, resume_id: Optional[str] = None, fork: bool = False, profile: Optional[Dict] = None, initial_context: Optional[Dict] = None):
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
        self.initial_context: Optional[Dict] = initial_context  # Initial context (subsession_id, parent_view_id, etc.)
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
        # Callback for channel mode responses
        self._response_callback: Optional[Callable[[str], None]] = None

        # Extract subsession_id and parent_view_id if provided
        if initial_context:
            self.subsession_id = initial_context.get("subsession_id")
            self.parent_view_id = initial_context.get("parent_view_id")
        else:
            self.subsession_id = None
            self.parent_view_id = None

        # Persona info (for release on close)
        if profile:
            self.persona_id = profile.get("persona_id")
            self.persona_session_id = profile.get("persona_session_id")
            self.persona_url = profile.get("persona_url")
        else:
            self.persona_id = None
            self.persona_session_id = None
            self.persona_url = None

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

        default_model = settings.get("default_model")
        print(f"[Claude] initialize: permission_mode={permission_mode}, allowed_tools={allowed_tools}, resume={self.resume_id}, fork={self.fork}, profile={self.profile}, default_model={default_model}, subsession_id={getattr(self, 'subsession_id', None)}")
        init_params = {
            "cwd": self._cwd(),
            "allowed_tools": allowed_tools,
            "permission_mode": permission_mode,
            "view_id": str(self.output.view.id()) if self.output and self.output.view else None,
        }
        if self.resume_id:
            init_params["resume"] = self.resume_id
            if self.fork:
                init_params["fork_session"] = True
        # Pass subsession_id if this is a subsession
        if hasattr(self, 'subsession_id') and self.subsession_id:
            init_params["subsession_id"] = self.subsession_id
        # Apply profile config or default model
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
        else:
            # No profile - use default_model setting if available
            if default_model:
                init_params["model"] = default_model
        self.client.send("initialize", init_params, self._on_init)

    def _cwd(self) -> str:
        if self.window.folders():
            return self.window.folders()[0]
        view = self.window.active_view()
        if view and view.file_name():
            return os.path.dirname(view.file_name())
        # Fallback: use ~/.claude/scratch for sessions without a project
        # This ensures consistent cwd for session resume
        scratch_dir = os.path.expanduser("~/.claude/scratch")
        os.makedirs(scratch_dir, exist_ok=True)
        return scratch_dir

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
        # Capture session_id from initialize response (set via --session-id CLI arg)
        if result.get("session_id"):
            self.session_id = result["session_id"]
            print(f"[Claude] session_id={self.session_id}")
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

    def query(self, prompt: str, display_prompt: str = None, silent: bool = False) -> None:
        """
        Start a new query.

        Args:
            prompt: The full prompt to send to the agent
            display_prompt: Optional shorter prompt to display in the UI (defaults to prompt)
            silent: If True, skip UI updates (for channel mode)
        """
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

        # Use display_prompt for UI if provided, otherwise use full prompt
        ui_prompt = display_prompt if display_prompt else prompt

        print(f"[Claude] >>> {ui_prompt[:60]}...")

        # Check if bridge is alive before sending
        if not self.client.is_alive():
            self._status("error: bridge died")
            if not silent:
                self.output.text("\n\n*Bridge process died. Please restart the session.*\n")
            return

        if not silent:
            self.output.show()
            # Auto-name session from first prompt if not already named
            if not self.name:
                self._set_name(ui_prompt[:30].strip() + ("..." if len(ui_prompt) > 30 else ""))
            print(f"[Claude] query() - calling output.prompt()")
            self.output.prompt(ui_prompt, context_names)
            print(f"[Claude] query() - calling _animate()")
            self._animate()
        print(f"[Claude] query() - sending RPC query request")
        if not self.client.send("query", {"prompt": full_prompt}, self._on_done):
            self._status("error: bridge died")
            self.working = False
            self.output.text("\n\n*Failed to send query. Bridge process died.*\n")
        else:
            print(f"[Claude] query() - RPC query sent successfully")

    def send_message_with_callback(self, message: str, callback: Callable[[str], None], silent: bool = False, display_prompt: str = None) -> None:
        """Send message and call callback with Claude's response.

        Used by channel mode for sync request-response communication.

        Args:
            message: The message to send to Claude
            callback: Function to call with the response text when complete
            silent: If True, skip UI updates
            display_prompt: Optional display text for UI (ignored if silent=True)
        """
        # Validate session state before setting callback
        if not self.client or not self.initialized:
            print(f"[Claude] send_message_with_callback: session not initialized")
            callback("Error: session not initialized")
            return
        if not self.client.is_alive():
            print(f"[Claude] send_message_with_callback: bridge not running")
            callback("Error: bridge not running")
            return

        print(f"[Claude] send_message_with_callback: sending message")
        self._response_callback = callback
        ui_prompt = display_prompt if display_prompt else (message[:50] + "..." if len(message) > 50 else message)
        self.query(message, display_prompt=ui_prompt, silent=silent)

        # Check if query() failed (working is False if send failed)
        if not self.working and self._response_callback:
            print(f"[Claude] send_message_with_callback: query failed, calling callback with error")
            cb = self._response_callback
            self._response_callback = None
            cb("Error: failed to send query")

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
            error_msg = result['error'].get('message', str(result['error'])) if isinstance(result['error'], dict) else str(result['error'])
            self._status("error")
            print(f"[Claude] query error: {error_msg}")
            # Show error to user
            self.output.text(f"\n\n*Error: {error_msg}*\n")
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

        # Call response callback if set (channel mode)
        if self._response_callback:
            callback = self._response_callback
            self._response_callback = None  # Clear before calling
            # Extract response text from current conversation
            response_text = ""
            if self.output.current:
                response_text = "".join(self.output.current.text_chunks)
            print(f"[Claude] _on_done: calling response callback with {len(response_text)} chars")
            try:
                callback(response_text)
            except Exception as e:
                print(f"[Claude] response callback error: {e}")

        # Notify ALL bridges that this subsession completed (for notalone2)
        # Other sessions may be waiting on this subsession via notifications
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

    def interrupt(self, break_channel: bool = True) -> None:
        """Interrupt current query.

        Args:
            break_channel: If True, also breaks any active channel connection.
                          Set to False when interrupt is from channel message.
        """
        if self.client:
            self.client.send("interrupt", {})
            self._status("interrupting...")
            self.working = False

        # Break any active channel connection (only for user-initiated interrupts)
        if break_channel:
            from . import notalone
            notalone.interrupt_channel(self.view.id())

    def stop(self) -> None:
        # Release persona if acquired
        if self.persona_session_id and self.persona_url:
            self._release_persona()

        if self.client:
            self.client.send("shutdown", {}, lambda _: self.client.stop())
        self._clear_status()

    def _release_persona(self) -> None:
        """Release acquired persona."""
        import threading
        from . import persona_client

        session_id = self.persona_session_id
        persona_url = self.persona_url

        def release():
            result = persona_client.release_persona(session_id, base_url=persona_url)
            if "error" not in result:
                print(f"[Claude] Released persona for session {session_id}")

        threading.Thread(target=release, daemon=True).start()

    # ─── Notification Tools ───────────────────────────────────────────────
    # Notification tools are provided by dedicated MCP servers:
    # - notalone2 daemon: timers, session completion, list/unregister
    # - vibekanban MCP server: watch_kanban for ticket state changes

    def _on_notification(self, method: str, params: dict) -> None:
        if method == "permission_request":
            self._handle_permission_request(params)
            return

        if method == "question_request":
            self._handle_question_request(params)
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

        if method == "notification_wake":
            # Notification fired - start a new query with the wake prompt
            wake_prompt = params.get("wake_prompt", "")
            display_message = params.get("display_message", "")  # User-friendly message
            notification_id = params.get("notification_id", "")

            # Use display_message for user (concise), wake_prompt goes to agent (detailed)
            # If no display_message, extract first line of wake_prompt
            if display_message:
                user_message = display_message
            else:
                # Extract first meaningful line for display
                first_line = wake_prompt.split("\n")[0].strip() if wake_prompt else ""
                user_message = first_line if first_line else "🔔 Notification received"

            print(f"[Claude] {user_message}")

            # If session is still working, queue the wake query for when it becomes idle
            if self.working:
                print(f"[Claude] Session is busy, will start when idle...")

                def start_wake_query():
                    if not self.working:
                        try:
                            self.query(wake_prompt, display_prompt=user_message)
                        except Exception as e:
                            print(f"[Claude] ✗ ERROR starting deferred wake query: {e}")
                    else:
                        # Still working, try again later
                        sublime.set_timeout(start_wake_query, 500)

                sublime.set_timeout(start_wake_query, 500)
                return

            # Session is idle, start wake query immediately
            try:
                self.query(wake_prompt, display_prompt=user_message)
            except Exception as e:
                print(f"[Claude] ✗ ERROR starting wake query: {e}")
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

    def _handle_question_request(self, params: dict) -> None:
        """Handle AskUserQuestion from Claude - show quick panel for each question."""
        qid = params.get("id")
        questions = params.get("questions", [])
        print(f"[Claude] _handle_question_request: qid={qid}, questions={len(questions)}")

        if not questions:
            if self.client:
                self.client.send("question_response", {"id": qid, "answers": {}})
            return

        answers = {}
        current_q = [0]  # Use list to allow mutation in nested function

        def ask_next():
            if current_q[0] >= len(questions):
                # All questions answered
                if self.client:
                    self.client.send("question_response", {"id": qid, "answers": answers})
                return

            q = questions[current_q[0]]
            question_text = q.get("question", "")
            options = q.get("options", [])
            header = q.get("header", f"Q{current_q[0]+1}")

            # Build quick panel items
            items = []
            for opt in options:
                label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
                desc = opt.get("description", "") if isinstance(opt, dict) else ""
                items.append([label, desc])
            items.append(["Other...", "Type a custom response"])

            def on_select(idx):
                if idx == -1:
                    # Cancelled - send None to deny
                    if self.client:
                        self.client.send("question_response", {"id": qid, "answers": None})
                    return
                elif idx == len(options):
                    # "Other" - show input panel
                    def on_input(text):
                        answers[str(current_q[0])] = text
                        current_q[0] += 1
                        sublime.set_timeout(ask_next, 50)

                    def on_cancel():
                        if self.client:
                            self.client.send("question_response", {"id": qid, "answers": None})

                    self.window.show_input_panel(question_text, "", on_input, None, on_cancel)
                else:
                    # Selected an option
                    opt = options[idx]
                    label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
                    answers[str(current_q[0])] = label
                    current_q[0] += 1
                    sublime.set_timeout(ask_next, 50)

            self.window.show_quick_panel(items, on_select, placeholder=f"{header}: {question_text}")

        sublime.set_timeout(ask_next, 0)

    # ─── Notification API (notalone2) ──────────────────────────────────

    def subscribe_to_service(
        self,
        notification_type: str,
        params: dict,
        wake_prompt: str
    ) -> dict:
        """Subscribe to a service - handles HTTP endpoints for channel services.

        This is synchronous and returns a result dict.
        """
        import urllib.request
        import json as json_mod
        import socket as sock_mod

        # First, get services list synchronously from notalone
        try:
            sock = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(os.path.expanduser("~/.notalone/notalone.sock"))
            sock.sendall((json_mod.dumps({"method": "services"}) + "\n").encode())
            response = sock.recv(65536).decode().strip()
            sock.close()
            services_data = json_mod.loads(response)
            services = services_data.get("services", [])
        except Exception as e:
            print(f"[Claude] Failed to get services: {e}")
            services = []

        # Check if this is a channel service with an endpoint
        endpoint = None
        for svc in services:
            if svc.get("type") == notification_type and svc.get("endpoint"):
                endpoint = svc.get("endpoint")
                break

        # If it has an endpoint, POST to it first
        if endpoint:
            session_id = params.get("session_id", f"sublime.{self.view.id()}")
            try:
                req = urllib.request.Request(
                    endpoint,
                    data=json_mod.dumps({"session_id": session_id}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    result = resp.read().decode()
                    print(f"[Claude] Subscribed to {notification_type}: {result}")
            except Exception as e:
                print(f"[Claude] Failed to subscribe to {notification_type}: {e}")
                return {"error": str(e)}

        # Now register with notalone daemon (simple version, no callback)
        if self.client:
            self.client.send("register_notification", {
                "notification_type": notification_type,
                "params": params,
                "wake_prompt": wake_prompt
            })
        return {"ok": True, "notification_type": notification_type, "session_id": params.get("session_id", f"sublime.{self.view.id()}")}

    def register_notification(
        self,
        notification_type: str,
        params: dict,
        wake_prompt: str,
        notification_id: Optional[str] = None,
        callback: Optional[callable] = None
    ) -> None:
        """Register a notification via notalone2 daemon.

        Args:
            notification_type: 'timer', 'subsession_complete', 'ticket_update', 'channel'
            params: Type-specific parameters
            wake_prompt: Prompt to inject when notification fires
            notification_id: Optional custom notification ID
            callback: Optional callback for result
        """
        if not self.client:
            return

        self.client.send("register_notification", {
            "notification_type": notification_type,
            "params": params,
            "wake_prompt": wake_prompt,
            "notification_id": notification_id
        }, callback)

    def signal_subsession_complete(
        self,
        result_summary: Optional[str] = None,
        callback: Optional[callable] = None
    ) -> None:
        """Signal that this subsession has completed.

        Args:
            result_summary: Optional summary of what was accomplished
            callback: Optional callback for result
        """
        if not self.client:
            return

        self.client.send("signal_subsession_complete", {
            "result_summary": result_summary
        }, callback)
