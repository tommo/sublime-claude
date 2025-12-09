"""Claude Code plugin for Sublime Text."""
import sublime
import sublime_plugin
from typing import Dict, Optional

from .session import Session, load_saved_sessions
from . import mcp_server


# Sessions keyed by output view id - allows multiple sessions per window
_sessions: Dict[int, Session] = {}

def plugin_loaded() -> None:
    """Called when plugin is loaded. Reconnect orphaned output views and start MCP server."""
    sublime.set_timeout(_reconnect_orphaned_views, 1000)
    mcp_server.start()


def plugin_unloaded() -> None:
    """Called when plugin is unloaded. Stop MCP server."""
    mcp_server.stop()


def _reconnect_orphaned_views() -> None:
    """Find Claude output views without sessions and reconnect them."""
    print("[Claude] _reconnect_orphaned_views: starting")
    for window in sublime.windows():
        for view in window.views():
            if view.settings().get("claude_output") and view.id() not in _sessions:
                print(f"[Claude] _reconnect_orphaned_views: found orphaned view {view.id()}, name={view.name()}")
                # Found orphaned output view - try to get session info from view name
                name = view.name()
                session_name = None
                # Strip status prefix if present (◉ active+working, ◇ active+idle, • inactive+working)
                if name.startswith(("◉ ", "◇ ", "• ")):
                    name = name[2:]
                if name.startswith("Claude: "):
                    session_name = name[8:]
                    # Strip any " - tool" suffix from stale working state
                    if " - " in session_name:
                        session_name = session_name.split(" - ")[0]
                # Handle spinner prefix (e.g., "⠋ name - thinking")
                for c in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏":
                    if name.startswith(c + " "):
                        name = name[2:]
                        if " - " in name:
                            session_name = name.split(" - ")[0]
                        else:
                            session_name = name
                        break

                print(f"[Claude] _reconnect_orphaned_views: creating session, session_name={session_name}")
                # Don't resume - the view already has the content
                # Just create fresh session to avoid duplicate content
                session = Session(window)
                session.name = session_name
                session.output.view = view  # Reuse existing view
                session.draft_prompt = ""  # Clear any stale draft
                _sessions[view.id()] = session

                # Reset active states (pending tools, permissions, stale title)
                print("[Claude] _reconnect_orphaned_views: calling reset_active_states")
                session.output.reset_active_states()
                # Reset view title to clean state
                if session_name:
                    view.set_name(f"Claude: {session_name}")
                else:
                    view.set_name("Claude")

                print("[Claude] _reconnect_orphaned_views: calling session.start()")
                session.start()

                if session_name:
                    view.set_status("claude_reconnect", f"Reconnected: {session_name}")
                    sublime.set_timeout(lambda v=view: v.erase_status("claude_reconnect"), 3000)
    print("[Claude] _reconnect_orphaned_views: done")


def get_session_for_view(view: sublime.View) -> Optional[Session]:
    """Get session for a specific output view."""
    return _sessions.get(view.id())


def get_active_session(window: sublime.Window) -> Optional[Session]:
    """Get session for active view if it's a Claude output, or last active Claude session in window."""
    view = window.active_view()
    if view and view.settings().get("claude_output"):
        return _sessions.get(view.id())
    # Check for last active Claude view in this window
    active_view_id = window.settings().get("claude_active_view")
    if active_view_id and active_view_id in _sessions:
        session = _sessions[active_view_id]
        if session.window == window:
            return session
    # Fallback: return any session in this window
    for view_id, session in _sessions.items():
        if session.window == window:
            return session
    return None


def create_session(window: sublime.Window, resume_id: Optional[str] = None, fork: bool = False) -> Session:
    """Create a new session (always creates new, doesn't reuse)."""
    # Clear active marker from previous active session
    old_active = window.settings().get("claude_active_view")
    if old_active and old_active in _sessions:
        old_session = _sessions[old_active]
        old_session.output.set_name(old_session.name or "Claude")

    s = Session(window, resume_id=resume_id, fork=fork)
    s.output.show()  # Create view first
    s.start()
    # Register by view id and mark as active
    if s.output.view:
        _sessions[s.output.view.id()] = s
        window.settings().set("claude_active_view", s.output.view.id())
    return s


class ClaudeCodeStartCommand(sublime_plugin.WindowCommand):
    """Start a new session."""
    def run(self) -> None:
        create_session(self.window)
        # Input mode is auto-entered when session initializes


class ClaudeCodeQueryCommand(sublime_plugin.WindowCommand):
    """Open input for query (legacy - now just focuses output and enters input mode)."""
    def run(self) -> None:
        s = get_active_session(self.window) or create_session(self.window)
        s.output.show()
        s._enter_input_with_draft()


class ClaudeCodeAddMcpCommand(sublime_plugin.WindowCommand):
    """Add MCP tools config to project."""
    def run(self) -> None:
        import os
        import json

        folders = self.window.folders()
        if not folders:
            sublime.status_message("No project folder open")
            return

        project_root = folders[0]
        claude_dir = os.path.join(project_root, ".claude")
        settings_path = os.path.join(claude_dir, "settings.json")
        tools_dir = os.path.join(claude_dir, "sublime_tools")

        # Create directories
        os.makedirs(claude_dir, exist_ok=True)
        os.makedirs(tools_dir, exist_ok=True)

        # Get MCP server path
        plugin_dir = os.path.dirname(__file__)
        mcp_server = os.path.join(plugin_dir, "mcp", "server.py")

        # Load or create settings
        settings = {}
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r") as f:
                    settings = json.load(f)
            except:
                pass

        # Add MCP server config
        if "mcpServers" not in settings:
            settings["mcpServers"] = {}

        settings["mcpServers"]["sublime"] = {
            "command": "python3",
            "args": [mcp_server]
        }

        # Write settings
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)

        # Create example tool
        example_tool = os.path.join(tools_dir, "example.py")
        if not os.path.exists(example_tool):
            with open(example_tool, "w") as f:
                f.write('''# Example sublime tool
# Run with: sublime_eval(tool="example")

window = sublime.active_window()
view = window.active_view()

return {
    "file": view.file_name() if view else None,
    "selection": view.substr(view.sel()[0]) if view and view.sel() else None,
    "cursor": view.rowcol(view.sel()[0].begin()) if view and view.sel() else None,
}
''')

        sublime.status_message(f"MCP config added to {claude_dir}")
        self.window.open_file(settings_path)


class ClaudeCodeBlackboardCommand(sublime_plugin.WindowCommand):
    """View and edit the shared blackboard."""
    def run(self) -> None:
        from . import mcp_server

        bb = mcp_server._blackboard
        if not bb:
            sublime.status_message("Blackboard is empty")
            return

        # Build quick panel items
        items = []
        keys = list(bb.keys())
        for key in keys:
            entry = bb[key]
            value = entry["value"]
            if isinstance(value, str):
                preview = value[:80].replace("\n", "↵")
            else:
                preview = str(value)[:80]
            items.append([key, preview])

        def on_select(idx):
            if idx >= 0:
                key = keys[idx]
                self._show_entry(key)

        self.window.show_quick_panel(items, on_select)

    def _show_entry(self, key: str) -> None:
        from . import mcp_server

        entry = mcp_server._blackboard.get(key, {})
        value = entry.get("value", "")

        # Show in a new scratch view
        view = self.window.new_file()
        view.set_name(f"Blackboard: {key}")
        view.set_scratch(True)
        view.settings().set("blackboard_key", key)

        content = value if isinstance(value, str) else json.dumps(value, indent=2)
        view.run_command("insert", {"characters": content})

        # Add save hint
        sublime.status_message("Edit and save (Cmd+S) to update blackboard, or close to discard")


class ClaudeCodeBlackboardSaveCommand(sublime_plugin.TextCommand):
    """Save edited blackboard entry."""
    def run(self, edit) -> None:
        key = self.view.settings().get("blackboard_key")
        if not key:
            return

        from . import mcp_server

        content = self.view.substr(sublime.Region(0, self.view.size()))

        # Try to parse as JSON, otherwise store as string
        try:
            value = json.loads(content)
        except:
            value = content

        mcp_server._blackboard[key] = {
            "value": value,
            "timestamp": __import__("time").time(),
        }
        sublime.status_message(f"Blackboard '{key}' updated")
        self.view.set_scratch(True)  # Mark as not modified


class ClaudeCodeRestartCommand(sublime_plugin.WindowCommand):
    """Restart session, keeping the output view."""
    def run(self) -> None:
        old_session = get_active_session(self.window)
        old_view = None

        if old_session:
            old_view = old_session.output.view
            old_session.stop()
            if old_view and old_view.id() in _sessions:
                del _sessions[old_view.id()]

        # Create new session
        new_session = Session(self.window)

        # Reuse existing view if available
        if old_view and old_view.is_valid():
            new_session.output.view = old_view
            new_session.output.clear()
            _sessions[old_view.id()] = new_session

        new_session.start()
        if new_session.output.view:
            new_session.output.view.set_name("Claude")
            if new_session.output.view.id() not in _sessions:
                _sessions[new_session.output.view.id()] = new_session
        new_session.output.show()
        sublime.status_message("Session restarted")


class ClaudeCodeQuerySelectionCommand(sublime_plugin.TextCommand):
    def run(self, edit: sublime.Edit) -> None:
        sel = self.view.sel()
        if not sel or sel[0].empty():
            return

        text = self.view.substr(sel[0])
        fname = self.view.file_name() or "untitled"

        self.view.window().show_input_panel(
            "Ask about selection:",
            "",
            lambda p: self._done(p, text, fname),
            None, None
        )

    def _done(self, prompt: str, selection: str, fname: str) -> None:
        if not prompt.strip():
            return
        window = self.view.window()
        s = get_active_session(window)
        if not s:
            s = create_session(window)
        q = f"{prompt}\n\nFrom `{fname}`:\n```\n{selection}\n```"
        s.output.show()
        s.output._move_cursor_to_end()
        if s.initialized:
            s.query(q)
        else:
            sublime.set_timeout(lambda: s.query(q), 500)


class ClaudeCodeQueryFileCommand(sublime_plugin.WindowCommand):
    """Send current file as prompt."""
    def run(self) -> None:
        view = self.window.active_view()
        if not view or not view.file_name():
            sublime.status_message("No file to send")
            return

        s = get_active_session(self.window)
        if not s:
            s = create_session(self.window)
        content = view.substr(sublime.Region(0, view.size()))
        fname = view.file_name()

        self.window.show_input_panel(
            "Ask about file:",
            "",
            lambda p: self._done(p, content, fname),
            None, None
        )

    def _done(self, prompt: str, content: str, fname: str) -> None:
        if not prompt.strip():
            return
        s = get_active_session(self.window)
        if not s:
            return
        q = f"{prompt}\n\nFile: `{fname}`\n```\n{content}\n```"
        s.output.show()
        s.output._move_cursor_to_end()
        if s.initialized:
            s.query(q)
        else:
            sublime.set_timeout(lambda: s.query(q), 500)


class ClaudeCodeAddFileCommand(sublime_plugin.WindowCommand):
    """Add current file to context."""
    def run(self) -> None:
        view = self.window.active_view()
        if not view or not view.file_name():
            sublime.status_message("No file to add")
            return
        s = get_active_session(self.window)
        if not s:
            sublime.status_message("No active session. Use 'Claude: New Session' first.")
            return
        content = view.substr(sublime.Region(0, view.size()))
        s.add_context_file(view.file_name(), content)
        name = view.file_name().split("/")[-1]
        sublime.status_message(f"Added: {name}")


class ClaudeCodeAddSelectionCommand(sublime_plugin.WindowCommand):
    """Add selection to context."""
    def run(self) -> None:
        view = self.window.active_view()
        if not view:
            sublime.status_message("No active view")
            return
        sel = view.sel()
        if not sel or sel[0].empty():
            sublime.status_message("No selection")
            return
        s = get_active_session(self.window)
        if not s:
            sublime.status_message("No active session. Use 'Claude: New Session' first.")
            return
        content = view.substr(sel[0])
        path = view.file_name() or "untitled"
        s.add_context_selection(path, content)
        name = path.split("/")[-1] if "/" in path else path
        sublime.status_message(f"Added selection from: {name}")


class ClaudeCodeAddOpenFilesCommand(sublime_plugin.WindowCommand):
    """Add all open files to context."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if not s:
            sublime.status_message("No active session. Use 'Claude: New Session' first.")
            return
        count = 0
        for view in self.window.views():
            if view.file_name() and not view.settings().get("claude_output"):
                content = view.substr(sublime.Region(0, view.size()))
                s.add_context_file(view.file_name(), content)
                count += 1
        sublime.status_message(f"Added {count} files")


class ClaudeCodeAddFolderCommand(sublime_plugin.WindowCommand):
    """Add current file's folder path to context."""
    def run(self) -> None:
        import os

        view = self.window.active_view()
        if not view or not view.file_name():
            sublime.status_message("No file open")
            return

        s = get_active_session(self.window)
        if not s:
            sublime.status_message("No active session. Use 'Claude: New Session' first.")
            return

        folder = os.path.dirname(view.file_name())
        s.add_context_folder(folder)
        folder_name = folder.split("/")[-1]
        sublime.status_message(f"Added folder: {folder_name}/")


class ClaudeCodeClearContextCommand(sublime_plugin.WindowCommand):
    """Clear pending context."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if s:
            s.clear_context()
            sublime.status_message("Context cleared")


class ClaudeCodeInterruptCommand(sublime_plugin.WindowCommand):
    def run(self) -> None:
        s = get_active_session(self.window)
        if s:
            s.interrupt()


class ClaudeCodeClearCommand(sublime_plugin.WindowCommand):
    def run(self) -> None:
        s = get_active_session(self.window)
        if s:
            s.output.clear()


class ClaudeCodeResetInputCommand(sublime_plugin.WindowCommand):
    """Force reset input mode state when it gets corrupted."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if s:
            s.output.reset_input_mode()
            sublime.status_message("Input mode reset")


class ClaudeCodeRenameCommand(sublime_plugin.WindowCommand):
    """Rename the current session."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if not s:
            return
        current = s.name or ""
        self.window.show_input_panel(
            "Session name:",
            current,
            lambda name: self._done(name),
            None, None
        )

    def _done(self, name: str) -> None:
        if name.strip():
            s = get_active_session(self.window)
            if s:
                s._set_name(name.strip())


class ClaudeCodeToggleCommand(sublime_plugin.WindowCommand):
    def run(self) -> None:
        s = get_active_session(self.window)
        if s and s.output.view and s.output.view.is_valid():
            # View exists - toggle visibility
            group, _ = self.window.get_view_index(s.output.view)
            if group >= 0:
                # Visible - hide it
                self.window.focus_view(s.output.view)
                self.window.run_command("close_file")
            else:
                # Hidden/closed - show it
                s.output.show()
        elif s:
            # No view yet - show it
            s.output.show()


class ClaudeCodeStopCommand(sublime_plugin.WindowCommand):
    def run(self) -> None:
        s = get_active_session(self.window)
        if s and s.output.view:
            view_id = s.output.view.id()
            s.stop()
            if view_id in _sessions:
                del _sessions[view_id]


class ClaudeCodeResumeCommand(sublime_plugin.WindowCommand):
    """Resume a previous session."""
    def run(self) -> None:
        sessions = load_saved_sessions()
        if not sessions:
            sublime.status_message("No saved sessions to resume")
            return

        # Build quick panel items
        items = []
        for s in sessions:
            name = s.get("name") or "(unnamed)"
            project = s.get("project", "")
            if project:
                project = "  " + project.split("/")[-1]
            cost = s.get("total_cost", 0)
            cost_str = f"  ${cost:.4f}" if cost else ""
            items.append([name, f"{project}{cost_str}"])

        def on_select(idx):
            if idx >= 0:
                session_id = sessions[idx].get("session_id")
                name = sessions[idx].get("name")
                s = create_session(self.window, resume_id=session_id)
                if name:
                    s.name = name
                    s.output.show()
                    s.output.set_name(name)
                    s._update_status_bar()

        self.window.show_quick_panel(items, on_select)


class ClaudeCodeSwitchCommand(sublime_plugin.WindowCommand):
    """Switch between active sessions in this window."""
    def run(self) -> None:
        # Get all sessions in this window
        sessions_in_window = []
        for view_id, session in _sessions.items():
            if session.window == self.window:
                sessions_in_window.append((view_id, session))

        if not sessions_in_window:
            sublime.status_message("No active sessions")
            return

        if len(sessions_in_window) == 1:
            # Only one session - just focus it
            sessions_in_window[0][1].output.show()
            return

        # Build quick panel items
        active_view_id = self.window.settings().get("claude_active_view")
        items = []
        for view_id, s in sessions_in_window:
            name = s.name or "(unnamed)"
            # ◉ = active+working, ◇ = active+idle, • = inactive+working, (space) = inactive+idle
            if view_id == active_view_id:
                marker = "◉ " if s.working else "◇ "
            else:
                marker = "• " if s.working else "  "
            status = "working..." if s.working else "ready"
            cost = f"${s.total_cost:.4f}" if s.total_cost > 0 else ""
            detail = f"{status}  {cost}  {s.query_count}q" if cost else f"{status}  {s.query_count}q"
            items.append([f"{marker}{name}", detail])

        def on_select(idx):
            if idx >= 0:
                view_id, session = sessions_in_window[idx]
                session.output.show()

        self.window.show_quick_panel(items, on_select)


class ClaudeCodeForkCommand(sublime_plugin.WindowCommand):
    """Fork the current active session."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if not s or not s.session_id:
            sublime.status_message("No active session to fork")
            return

        # Create forked session
        forked = create_session(self.window, resume_id=s.session_id, fork=True)
        forked_name = f"{s.name or 'session'} (fork)"
        forked.name = forked_name
        forked.output.set_name(forked_name)
        sublime.status_message(f"Forked session: {forked_name}")


class ClaudeCodeForkFromCommand(sublime_plugin.WindowCommand):
    """Fork from a session selected from list."""
    def run(self) -> None:
        # Combine active sessions and saved sessions
        items = []
        sources = []  # Track source of each item: ("active", view_id) or ("saved", session_id)

        # Active sessions in this window
        for view_id, session in _sessions.items():
            if session.window == self.window and session.session_id:
                name = session.name or "(unnamed)"
                cost = f"${session.total_cost:.4f}" if session.total_cost > 0 else ""
                items.append([f"● {name}", f"active  {cost}  {session.query_count}q"])
                sources.append(("active", view_id, session.session_id, name))

        # Saved sessions
        saved = load_saved_sessions()
        for s in saved:
            session_id = s.get("session_id")
            name = s.get("name") or "(unnamed)"
            # Skip if already in active list
            if any(src[2] == session_id for src in sources):
                continue
            project = s.get("project", "")
            if project:
                project = project.split("/")[-1]
            cost = s.get("total_cost", 0)
            cost_str = f"${cost:.4f}" if cost else ""
            items.append([name, f"saved  {project}  {cost_str}"])
            sources.append(("saved", None, session_id, name))

        if not items:
            sublime.status_message("No sessions to fork from")
            return

        def on_select(idx):
            if idx >= 0:
                source_type, view_id, session_id, name = sources[idx]
                forked = create_session(self.window, resume_id=session_id, fork=True)
                forked_name = f"{name} (fork)"
                forked.name = forked_name
                forked.output.set_name(forked_name)
                sublime.status_message(f"Forked session: {forked_name}")

        self.window.show_quick_panel(items, on_select)


class ClaudeCodeEventListener(sublime_plugin.EventListener):
    def on_window_command(self, window: sublime.Window, command: str, args: dict) -> None:
        if command == "close_window":
            # Stop all sessions in this window
            to_remove = []
            for view_id, session in _sessions.items():
                if session.window == window:
                    session.stop()
                    to_remove.append(view_id)
            for view_id in to_remove:
                del _sessions[view_id]

    def on_activated(self, view: sublime.View) -> None:
        """Handle view activated - check if it's for context adding from goto."""
        import time
        window = view.window()
        if not window:
            return

        # Skip Claude output views
        if view.settings().get("claude_output"):
            return

        # Check if we have a pending context session
        session_view_id = window.settings().get("claude_pending_context_session")
        if not session_view_id:
            return

        # Check timestamp - only process if at least 300ms have passed
        # This prevents processing the initial activation when overlay opens
        pending_time = window.settings().get("claude_pending_context_time", 0)
        if time.time() - pending_time < 0.3:
            return

        # Clear the pending state
        window.settings().erase("claude_pending_context_session")
        window.settings().erase("claude_pending_context_time")

        # Get the session
        session = _sessions.get(session_view_id)
        if not session:
            return

        # Add the file as context
        path = view.file_name()
        if path:
            content = view.substr(sublime.Region(0, view.size()))
            session.add_context_file(path, content)
            sublime.status_message(f"Added context: {path.split('/')[-1]}")

            # Focus back to Claude output and re-enter input mode
            def refocus():
                session.output.show()
                session.output.enter_input_mode()
                # Restore draft if any
                if session.draft_prompt:
                    session.output.view.run_command("append", {"characters": session.draft_prompt})
                    end = session.output.view.size()
                    session.output.view.sel().clear()
                    session.output.view.sel().add(sublime.Region(end, end))

            sublime.set_timeout(refocus, 100)

    def on_close(self, view: sublime.View) -> None:
        # Clean up session when output view is closed
        if view.id() in _sessions:
            _sessions[view.id()].stop()
            del _sessions[view.id()]


class ClaudeOutputEventListener(sublime_plugin.ViewEventListener):
    """Handle keys in the Claude output view."""

    @classmethod
    def is_applicable(cls, settings):
        return settings.get("claude_output", False)

    def on_activated(self):
        """Update status bar and title when this output view becomes active."""
        window = self.view.window()
        if not window:
            return

        # Mark this as the "active" session for the window
        old_active = window.settings().get("claude_active_view")
        switched = old_active != self.view.id()
        window.settings().set("claude_active_view", self.view.id())

        # Update this session's status and title
        s = get_session_for_view(self.view)
        if s:
            s._update_status_bar()
            s.output.set_name(s.name or "Claude")

            # Auto-enter input mode if session is idle and not already in input mode
            if s.initialized and not s.working and not s.output.is_input_mode():
                s._enter_input_with_draft()

        # Remove active marker from previous active session
        if old_active and old_active != self.view.id() and old_active in _sessions:
            old_session = _sessions[old_active]
            old_session.output.set_name(old_session.name or "Claude")



    def on_text_command(self, command_name, args):
        """Intercept text commands to restrict edits in input mode."""
        s = get_session_for_view(self.view)
        if not s or not s.output.is_input_mode():
            return None

        # Allow these commands always
        if command_name in ("copy", "select_all", "undo", "redo", "claude_submit_input"):
            return None

        # For insert/delete commands, check if cursor is in input region
        sel = self.view.sel()
        if sel:
            for region in sel:
                if not s.output.is_in_input_region(region.begin()):
                    # Block edit outside input region
                    return ("noop", {})

        return None

    def on_selection_modified(self):
        """Keep cursor within input region when in input mode."""
        s = get_session_for_view(self.view)
        if not s or not s.output.is_input_mode():
            return

        # Check if cursor is before input start
        input_start = s.output._input_start
        sel = self.view.sel()
        needs_fix = False

        for region in sel:
            if region.begin() < input_start or region.end() < input_start:
                needs_fix = True
                break

        if needs_fix:
            # Move cursor to input start
            self.view.sel().clear()
            self.view.sel().add(sublime.Region(input_start, input_start))

    def on_modified(self):
        """Track modifications for draft saving and @ autocomplete."""
        s = get_session_for_view(self.view)
        if not s or not s.output.is_input_mode():
            return

        # Save draft
        input_text = s.output.get_input_text()
        s.draft_prompt = input_text

        # Check for @ trigger at cursor
        sel = self.view.sel()
        if sel and len(sel) == 1:
            cursor = sel[0].end()
            # Check if char before cursor is @
            if cursor > 0:
                char_before = self.view.substr(cursor - 1)
                print(f"[Claude] on_modified: cursor={cursor}, char_before={repr(char_before)}")
                if char_before == "@":
                    print("[Claude] on_modified: @ detected, showing context popup")
                    self._show_context_popup(s, cursor)

    def _show_context_popup(self, session: Session, cursor: int) -> None:
        """Show @ context autocomplete via quick panel."""
        window = self.view.window()
        if not window:
            return

        # Remove the @ character first
        self.view.run_command("claude_replace", {
            "start": cursor - 1,
            "end": cursor,
            "text": ""
        })

        # Build menu items with action keys
        items = []  # (key, [title, description])

        # Current file
        active_view = None
        for v in window.views():
            if not v.settings().get("claude_output") and v.file_name():
                active_view = v
                break

        if active_view and active_view.file_name():
            name = active_view.file_name().split("/")[-1]
            items.append(("file", ["@ file", name]))

            # Selection in active view
            sel = active_view.sel()
            if sel and not sel[0].empty():
                preview = active_view.substr(sel[0])[:50].replace("\n", " ")
                items.append(("selection", ["@ selection", preview]))

        # Open files
        open_count = sum(1 for v in window.views() if v.file_name() and not v.settings().get("claude_output"))
        if open_count > 0:
            items.append(("open", ["@ open", f"{open_count} open files"]))

        # Current folder
        if active_view and active_view.file_name():
            import os
            folder = os.path.dirname(active_view.file_name())
            items.append(("folder", ["@ folder", folder]))

        # File picker
        items.append(("browse", ["@ browse...", "Choose file from project"]))

        # Clear context (only show if there's pending context)
        if session.pending_context:
            count = len(session.pending_context)
            items.append(("clear", ["@ clear", f"Clear {count} pending item{'s' if count > 1 else ''}"]))

        if not items:
            return

        def on_select(idx):
            if idx >= 0:
                key = items[idx][0]
                self._handle_context_choice(session, cursor, key)

        window.show_quick_panel(
            [item[1] for item in items],
            on_select,
            placeholder="Add context..."
        )

    def _handle_context_choice(self, session: Session, cursor: int, choice: str) -> None:
        """Handle context menu selection."""
        window = self.view.window()
        if not window:
            return

        active_view = None
        for v in window.views():
            if not v.settings().get("claude_output") and v.file_name():
                active_view = v
                break

        if choice == "file":
            if active_view and active_view.file_name():
                content = active_view.substr(sublime.Region(0, active_view.size()))
                session.add_context_file(active_view.file_name(), content)
        elif choice == "selection":
            if active_view:
                sel = active_view.sel()
                if sel and not sel[0].empty():
                    content = active_view.substr(sel[0])
                    path = active_view.file_name() or "untitled"
                    session.add_context_selection(path, content)
        elif choice == "open":
            for v in window.views():
                if v.file_name() and not v.settings().get("claude_output"):
                    content = v.substr(sublime.Region(0, v.size()))
                    session.add_context_file(v.file_name(), content)
        elif choice == "folder":
            if active_view and active_view.file_name():
                import os
                folder = os.path.dirname(active_view.file_name())
                session.add_context_folder(folder)
        elif choice == "browse":
            self._show_file_picker(session)
        elif choice == "clear":
            session.clear_context()
            sublime.status_message("Context cleared")

    def _show_file_picker(self, session: Session) -> None:
        """Show Ctrl+P file picker for context."""
        import time
        window = self.view.window()
        if not window:
            return

        # Store session and timestamp for the callback
        window.settings().set("claude_pending_context_session", session.output.view.id())
        window.settings().set("claude_pending_context_time", time.time())

        # Show the goto file overlay (Ctrl+P)
        window.run_command("show_overlay", {"overlay": "goto", "show_files": True})


class ClaudeSubmitInputCommand(sublime_plugin.TextCommand):
    """Handle Enter key in input mode - submit the prompt."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if not s:
            return

        if not s.output.is_input_mode():
            return

        # Get input text and exit input mode
        text = s.output.exit_input_mode(keep_text=False)
        s.draft_prompt = ""

        if text.strip():
            s.query(text)


class ClaudeEnterInputModeCommand(sublime_plugin.TextCommand):
    """Enter input mode in the Claude output view."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            s.output.enter_input_mode()
            # Restore draft if any
            if s.draft_prompt:
                self.view.run_command("append", {"characters": s.draft_prompt})
                # Move cursor to end
                end = self.view.size()
                self.view.sel().clear()
                self.view.sel().add(sublime.Region(end, end))


class ClaudeExitInputModeCommand(sublime_plugin.TextCommand):
    """Exit input mode, keeping the draft."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s and s.output.is_input_mode():
            # Save draft before exiting
            s.draft_prompt = s.output.get_input_text()
            s.output.exit_input_mode(keep_text=False)


class ClaudeInsertNewlineCommand(sublime_plugin.TextCommand):
    """Insert newline in input mode (Shift+Enter)."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s and s.output.is_input_mode():
            # Insert newline at cursor
            for region in self.view.sel():
                if s.output.is_in_input_region(region.begin()):
                    self.view.insert(edit, region.begin(), "\n")


class ClaudePermissionAllowCommand(sublime_plugin.TextCommand):
    """Handle Y key - allow permission."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            s.output.handle_permission_key("y")


class ClaudePermissionDenyCommand(sublime_plugin.TextCommand):
    """Handle N key - deny permission."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            s.output.handle_permission_key("n")


class ClaudePermissionAllowSessionCommand(sublime_plugin.TextCommand):
    """Handle S key - allow for 30s."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            s.output.handle_permission_key("s")


class ClaudePermissionAllowAllCommand(sublime_plugin.TextCommand):
    """Handle A key - allow all for this tool."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            s.output.handle_permission_key("a")


class ClaudeCodeTogglePermissionModeCommand(sublime_plugin.WindowCommand):
    """Toggle between permission modes."""

    MODES = ["default", "acceptEdits", "bypassPermissions"]
    MODE_LABELS = {
        "default": "Default (prompt for all)",
        "acceptEdits": "Accept Edits (auto-approve file ops)",
        "bypassPermissions": "Bypass (allow all - use with caution)",
    }

    def run(self):
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        current = settings.get("permission_mode", "default")

        items = []
        current_idx = 0
        for i, mode in enumerate(self.MODES):
            label = self.MODE_LABELS[mode]
            if mode == current:
                label = f"● {label}"
                current_idx = i
            else:
                label = f"  {label}"
            items.append(label)

        def on_select(idx):
            if idx >= 0:
                new_mode = self.MODES[idx]
                settings.set("permission_mode", new_mode)
                sublime.save_settings("ClaudeCode.sublime-settings")
                sublime.status_message(f"Claude: permission mode = {new_mode}")

                # Update active session if any
                s = get_active_session(self.window)
                if s and s.client:
                    # Notify bridge of mode change (for future requests)
                    s.client.send("set_permission_mode", {"mode": new_mode})

        self.window.show_quick_panel(items, on_select, selected_index=current_idx)


# ─── Quick Prompt Commands ────────────────────────────────────────────────────

QUICK_PROMPTS = {
    "refresh": "Re-read docs/agent/knowledge_index.md and the relevant guide for the current task. Then continue.",
    "retry": "That didn't work. Read the error carefully and try again with a different approach.",
    "continue": "Continue.",
}


class ClaudeQuickPromptCommand(sublime_plugin.TextCommand):
    """Send a quick prompt by key."""
    def run(self, edit, key: str):
        s = get_session_for_view(self.view)
        if not s:
            return
        prompt = QUICK_PROMPTS.get(key)
        if prompt and s.initialized and not s.working:
            s.query(prompt)
