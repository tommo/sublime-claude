"""Claude Code commands for Sublime Text."""
import sublime
import sublime_plugin

from .core import get_active_session, get_session_for_view, create_session
from .session import Session, load_saved_sessions


class ClaudeCodeStartCommand(sublime_plugin.WindowCommand):
    """Start a new session."""
    def run(self) -> None:
        create_session(self.window)


class ClaudeCodeQueryCommand(sublime_plugin.WindowCommand):
    """Open input for query (focuses output and enters input mode)."""
    def run(self) -> None:
        s = get_active_session(self.window) or create_session(self.window)
        s.output.show()
        s._enter_input_with_draft()


class ClaudeCodeRestartCommand(sublime_plugin.WindowCommand):
    """Restart session, keeping the output view."""
    def run(self) -> None:
        from .core import _sessions

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
        from .core import _sessions

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
        from .core import _sessions, create_session

        # Get all sessions in this window
        sessions_in_window = []
        for view_id, session in _sessions.items():
            if session.window == self.window:
                sessions_in_window.append((view_id, session))

        # Build quick panel items
        active_view_id = self.window.settings().get("claude_active_view")
        items = []
        actions = []  # ("new", None) | ("focus", session)

        # Add active session at top if exists
        active_session = None
        for view_id, s in sessions_in_window:
            if view_id == active_view_id:
                active_session = s
                break

        # Show "Active:" option only when not in a Claude output view (for quick jumping from file view)
        current_view = self.window.active_view()
        in_output_view = current_view and current_view.settings().get("claude_output")

        if active_session and not in_output_view:
            name = active_session.name or "(unnamed)"
            status = "working..." if active_session.working else "ready"
            cost = f"${active_session.total_cost:.4f}" if active_session.total_cost > 0 else ""
            detail = f"{status}  {cost}  {active_session.query_count}q" if cost else f"{status}  {active_session.query_count}q"
            items.append([f"Active: {name}", detail])
            actions.append(("focus", active_session))

        # Add other sessions (not the active one)
        for view_id, s in sessions_in_window:
            if view_id == active_view_id:
                continue  # Already shown at top
            name = s.name or "(unnamed)"
            marker = "• " if s.working else "  "
            status = "working..." if s.working else "ready"
            cost = f"${s.total_cost:.4f}" if s.total_cost > 0 else ""
            detail = f"{status}  {cost}  {s.query_count}q" if cost else f"{status}  {s.query_count}q"
            items.append([f"{marker}{name}", detail])
            actions.append(("focus", s))

        # Add "New Session" option at end
        items.append(["New Session", "Start a fresh Claude session"])
        actions.append(("new", None))

        def on_select(idx):
            if idx >= 0:
                action, session = actions[idx]
                if action == "new":
                    create_session(self.window)
                elif action == "focus" and session:
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
        from .core import _sessions

        # Combine active sessions and saved sessions
        items = []
        sources = []

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

        os.makedirs(claude_dir, exist_ok=True)
        os.makedirs(tools_dir, exist_ok=True)

        plugin_dir = os.path.dirname(__file__)
        mcp_server = os.path.join(plugin_dir, "mcp", "server.py")

        settings = {}
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r") as f:
                    settings = json.load(f)
            except:
                pass

        if "mcpServers" not in settings:
            settings["mcpServers"] = {}

        settings["mcpServers"]["sublime"] = {
            "command": "python3",
            "args": [mcp_server]
        }

        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)

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
        import json
        from . import mcp_server

        entry = mcp_server._blackboard.get(key, {})
        value = entry.get("value", "")

        view = self.window.new_file()
        view.set_name(f"Blackboard: {key}")
        view.set_scratch(True)
        view.settings().set("blackboard_key", key)

        content = value if isinstance(value, str) else json.dumps(value, indent=2)
        view.run_command("insert", {"characters": content})

        sublime.status_message("Edit and save (Cmd+S) to update blackboard, or close to discard")


class ClaudeCodeBlackboardSaveCommand(sublime_plugin.TextCommand):
    """Save edited blackboard entry."""
    def run(self, edit) -> None:
        import json

        key = self.view.settings().get("blackboard_key")
        if not key:
            return

        from . import mcp_server

        content = self.view.substr(sublime.Region(0, self.view.size()))

        try:
            value = json.loads(content)
        except:
            value = content

        mcp_server._blackboard[key] = {
            "value": value,
            "timestamp": __import__("time").time(),
        }
        sublime.status_message(f"Blackboard '{key}' updated")
        self.view.set_scratch(True)


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

                s = get_active_session(self.window)
                if s and s.client:
                    s.client.send("set_permission_mode", {"mode": new_mode})

        self.window.show_quick_panel(items, on_select, selected_index=current_idx)


# --- Input Mode Commands ---

class ClaudeSubmitInputCommand(sublime_plugin.TextCommand):
    """Handle Enter key in input mode - submit the prompt."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if not s:
            return

        if not s.output.is_input_mode():
            return

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
            if s.draft_prompt:
                self.view.run_command("append", {"characters": s.draft_prompt})
                end = self.view.size()
                self.view.sel().clear()
                self.view.sel().add(sublime.Region(end, end))


class ClaudeExitInputModeCommand(sublime_plugin.TextCommand):
    """Exit input mode, keeping the draft."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s and s.output.is_input_mode():
            s.draft_prompt = s.output.get_input_text()
            s.output.exit_input_mode(keep_text=False)


class ClaudeInsertNewlineCommand(sublime_plugin.TextCommand):
    """Insert newline in input mode (Shift+Enter)."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s and s.output.is_input_mode():
            for region in self.view.sel():
                if s.output.is_in_input_region(region.begin()):
                    self.view.insert(edit, region.begin(), "\n")


# --- Permission Commands ---

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


# --- Quick Prompts ---

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
