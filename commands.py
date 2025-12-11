"""Claude Code commands for Sublime Text."""
import sublime
import sublime_plugin

from .core import get_active_session, get_session_for_view, create_session
from .session import Session, load_saved_sessions


class ClaudeCodeStartCommand(sublime_plugin.WindowCommand):
    """Start a new session. Shows profile picker if profiles are configured."""
    def run(self, profile: str = None) -> None:
        from .mcp_server import _load_profiles_and_checkpoints

        profiles, checkpoints = _load_profiles_and_checkpoints()

        # If profile specified directly, use it
        if profile:
            profile_config = profiles.get(profile, {})
            create_session(self.window, profile=profile_config)
            return

        # Build options list
        options = []

        # Profiles
        for name, config in profiles.items():
            desc = config.get("description", f"{config.get('model', 'default')} model")
            options.append(("profile", name, f"📋 {name}", desc))

        # Checkpoints
        for name, config in checkpoints.items():
            desc = config.get("description", "Saved checkpoint")
            options.append(("checkpoint", name, f"📍 {name}", desc))

        # Default option (always available)
        options.insert(0, ("default", None, "🆕 New Session", "Start fresh with default settings"))

        if len(options) == 1:
            # Only default, just start
            create_session(self.window)
            return

        # Show quick panel
        items = [[opt[2], opt[3]] for opt in options]

        def on_select(idx):
            if idx < 0:
                return
            opt_type, opt_name, _, _ = options[idx]
            if opt_type == "default":
                create_session(self.window)
            elif opt_type == "profile":
                profile_config = profiles.get(opt_name, {})
                create_session(self.window, profile=profile_config)
            elif opt_type == "checkpoint":
                checkpoint = checkpoints.get(opt_name, {})
                session_id = checkpoint.get("session_id")
                if session_id:
                    create_session(self.window, resume_id=session_id, fork=True)
                else:
                    sublime.error_message(f"Checkpoint '{opt_name}' has no session_id")

        self.window.show_quick_panel(items, on_select)


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


class ClaudeCodeQueuePromptCommand(sublime_plugin.WindowCommand):
    """Queue a prompt to be sent when current query finishes."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if not s:
            sublime.status_message("No active session")
            return
        s.show_queue_input()


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


class ClaudeCodeCopyCommand(sublime_plugin.WindowCommand):
    """Copy entire conversation to clipboard."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if s and s.output.view and s.output.view.is_valid():
            content = s.output.view.substr(sublime.Region(0, s.output.view.size()))
            sublime.set_clipboard(content)
            sublime.status_message("Conversation copied to clipboard")


class ClaudeCodeSaveCheckpointCommand(sublime_plugin.WindowCommand):
    """Save current session as a named checkpoint for future forking."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if not s or not s.session_id:
            sublime.status_message("No active session with ID to checkpoint")
            return

        def on_done(name: str) -> None:
            name = name.strip()
            if not name:
                return

            from .mcp_server import _save_checkpoint
            if _save_checkpoint(name, s.session_id, s.name or "Checkpoint"):
                sublime.status_message(f"Checkpoint '{name}' saved")
            else:
                sublime.error_message(f"Failed to save checkpoint '{name}'")

        default_name = (s.name or "checkpoint").lower().replace(" ", "-")[:20]
        self.window.show_input_panel("Checkpoint name:", default_name, on_done, None, None)


class ClaudeCodeUsageCommand(sublime_plugin.WindowCommand):
    """Show API usage statistics."""
    def run(self) -> None:
        # Get current session usage
        s = get_active_session(self.window)
        current_usage = []
        if s:
            current_usage = [
                f"## Current Session: {s.name}",
                f"",
                f"Queries: {s.query_count}",
                f"Total Cost: ${s.total_cost:.4f}",
                f"",
            ]

        # Get all saved sessions usage
        sessions = load_saved_sessions()
        total_cost = sum(sess.get("total_cost", 0) for sess in sessions)
        total_queries = sum(sess.get("query_count", 0) for sess in sessions)

        lines = [
            "# API Usage Statistics",
            "",
            f"Total (All Sessions): ${total_cost:.4f} ({total_queries} queries)",
            "",
        ]

        if current_usage:
            lines.extend(current_usage)

        if sessions:
            lines.extend([
                "## Recent Sessions",
                ""
            ])
            for sess in sessions[:10]:  # Show last 10
                name = sess.get("name", "Untitled")
                cost = sess.get("total_cost", 0)
                queries = sess.get("query_count", 0)
                lines.append(f"- {name}: ${cost:.4f} ({queries} queries)")

        # Show in quick panel with monospace font
        content = "\n".join(lines)

        # Create a new output panel to show usage
        panel = self.window.create_output_panel("claude_usage")
        panel.set_read_only(False)
        panel.run_command("append", {"characters": content})
        panel.set_read_only(True)
        panel.settings().set("word_wrap", False)
        panel.settings().set("gutter", False)
        self.window.run_command("show_panel", {"panel": "output.claude_usage"})


class ClaudeCodeViewHistoryCommand(sublime_plugin.WindowCommand):
    """View session history from Claude's stored conversation."""
    def run(self) -> None:
        from .session import load_saved_sessions
        sessions = load_saved_sessions()
        if not sessions:
            sublime.status_message("No saved sessions")
            return

        # Build quick panel items
        items = []
        for s in sessions:
            name = s.get("name", "Unnamed")[:40]
            sid = s.get("session_id", "")[:8]
            cost = s.get("total_cost", 0)
            queries = s.get("query_count", 0)
            project = os.path.basename(s.get("project", ""))
            items.append([f"{name}", f"{project} | {queries} queries | ${cost:.2f} | {sid}..."])

        def on_select(idx: int) -> None:
            if idx < 0:
                return
            session = sessions[idx]
            self._show_history(session)

        self.window.show_quick_panel(items, on_select)

    def _show_history(self, session: dict) -> None:
        """Extract and display user messages from session history."""
        import json

        sid = session.get("session_id", "")
        project = session.get("project", "")
        # Convert project path to Claude's format
        project_key = project.replace("/", "-").lstrip("-")
        history_file = os.path.expanduser(f"~/.claude/projects/{project_key}/{sid}.jsonl")

        if not os.path.exists(history_file):
            sublime.status_message(f"History file not found: {history_file}")
            return

        # Extract user messages
        messages = []
        with open(history_file, "r") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if d.get("type") == "user":
                        msg = d.get("message", {})
                        content = msg.get("content", [])
                        if isinstance(content, str):
                            messages.append(content)
                        elif isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text = c.get("text", "")
                                    if text and not text.startswith("[Request interrupted"):
                                        messages.append(text)
                except:
                    pass

        # Create output view
        view = self.window.new_file()
        view.set_name(f"History: {session.get('name', sid[:8])}")
        view.set_scratch(True)
        view.assign_syntax("Packages/Markdown/Markdown.sublime-syntax")

        # Format output
        output = f"# Session: {session.get('name', 'Unnamed')}\n"
        output += f"**ID:** {sid}\n"
        output += f"**Project:** {project}\n"
        output += f"**Queries:** {session.get('query_count', 0)} | **Cost:** ${session.get('total_cost', 0):.2f}\n\n"
        output += "---\n\n"

        for i, msg in enumerate(messages, 1):
            output += f"## [{i}]\n{msg}\n\n"

        view.run_command("append", {"characters": output})


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

        # Add "Restart Session" option when in a session window
        if in_output_view and active_session:
            items.append(["Restart Session", "Restart current session, keep output"])
            actions.append(("restart", active_session))

        # Add profiles and checkpoints
        from .mcp_server import _load_profiles_and_checkpoints
        profiles, checkpoints = _load_profiles_and_checkpoints()

        for name, config in profiles.items():
            desc = config.get("description", f"{config.get('model', 'default')} model")
            items.append([f"📋 {name}", desc])
            actions.append(("profile", config))

        for name, config in checkpoints.items():
            desc = config.get("description", "Saved checkpoint")
            items.append([f"📍 {name}", desc])
            actions.append(("checkpoint", config))

        # Add "New Session" option at end
        items.append(["🆕 New Session", "Start fresh with default settings"])
        actions.append(("new", None))

        def on_select(idx):
            if idx >= 0:
                action, data = actions[idx]
                if action == "restart" and data:
                    # Show profile picker for restart
                    self._show_restart_picker(data, profiles, checkpoints)
                elif action == "new":
                    create_session(self.window)
                elif action == "profile":
                    create_session(self.window, profile=data)
                elif action == "checkpoint":
                    session_id = data.get("session_id")
                    if session_id:
                        create_session(self.window, resume_id=session_id, fork=True)
                elif action == "focus" and data:
                    data.output.show()

        self.window.show_quick_panel(items, on_select)

    def _show_restart_picker(self, session, profiles, checkpoints):
        """Show profile/checkpoint picker for restart."""
        from .core import _sessions, create_session

        items = []
        actions = []

        # Default restart
        items.append(["🆕 Fresh Start", "Restart with default settings"])
        actions.append(("default", None))

        # Profiles
        for name, config in profiles.items():
            desc = config.get("description", f"{config.get('model', 'default')} model")
            items.append([f"📋 {name}", desc])
            actions.append(("profile", config))

        # Checkpoints
        for name, config in checkpoints.items():
            desc = config.get("description", "Saved checkpoint")
            items.append([f"📍 {name}", desc])
            actions.append(("checkpoint", config))

        def on_select(idx):
            if idx < 0:
                return

            action, data = actions[idx]
            old_view = session.output.view

            # Stop old session
            session.stop()
            if old_view and old_view.id() in _sessions:
                del _sessions[old_view.id()]

            # Create new session with selected config
            if action == "checkpoint":
                session_id = data.get("session_id")
                new_session = Session(self.window, resume_id=session_id, fork=True)
            elif action == "profile":
                new_session = Session(self.window, profile=data)
            else:
                new_session = Session(self.window)

            # Reuse existing view
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

        text = s.output.get_input_text().strip()

        # Ignore empty input
        if not text:
            return

        s.output.exit_input_mode(keep_text=False)
        s.draft_prompt = ""

        # If session is working, queue the prompt instead
        if s.working:
            s.queue_prompt(text)
        else:
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
