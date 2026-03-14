"""Claude Code commands for Sublime Text."""
import sublime
import sublime_plugin
import platform

from .core import get_active_session, get_session_for_view, create_session
from .session import Session, load_saved_sessions
from .prompt_builder import PromptBuilder
from .command_parser import CommandParser


class ClaudeCodeStartCommand(sublime_plugin.WindowCommand):
    """Start a new session. Shows profile picker if profiles are configured."""
    def run(self, profile: str = None, persona_id: int = None, backend: str = None) -> None:
        from .settings import load_profiles_and_checkpoints, load_project_settings
        import os

        # Default to claude — use "codex" backend via explicit arg or separate command
        if backend is None:
            backend = "claude"

        # If persona_id specified, acquire and start
        if persona_id:
            self._start_with_persona(persona_id)
            return

        # Get project profiles path
        project_path = None
        cwd = None
        if self.window.folders():
            cwd = self.window.folders()[0]
            project_path = os.path.join(cwd, ".claude", "profiles.json")

        profiles, checkpoints = load_profiles_and_checkpoints(project_path)
        settings = load_project_settings(cwd)

        # If profile specified directly, use it
        if profile:
            profile_config = profiles.get(profile, {})
            create_session(self.window, profile=profile_config, backend=backend)
            return

        # Build options list
        options = []

        # Default option (always available)
        options.append(("default", None, "🆕 New Session", "Start fresh with default settings"))

        # Personas - get URL from sublime settings
        sublime_settings = sublime.load_settings("ClaudeCode.sublime-settings")
        persona_url = sublime_settings.get("persona_url", "http://localhost:5002/personas")
        options.append(("persona", persona_url, "👤 From Persona...", "Acquire a persona identity"))

        # Profiles
        for name, config in profiles.items():
            desc = config.get("description", f"{config.get('model', 'default')} model")
            options.append(("profile", name, f"📋 {name}", desc))

        # Checkpoints
        for name, config in checkpoints.items():
            desc = config.get("description", "Saved checkpoint")
            options.append(("checkpoint", name, f"📍 {name}", desc))

        if len(options) == 1:
            # Only default, just start
            create_session(self.window, backend=backend)
            return

        # Show quick panel
        items = [[opt[2], opt[3]] for opt in options]

        def on_select(idx):
            if idx < 0:
                return
            opt_type, opt_name, _, _ = options[idx]
            if opt_type == "default":
                create_session(self.window, backend=backend)
            elif opt_type == "persona":
                self._show_persona_picker(opt_name)  # opt_name contains the URL
            elif opt_type == "profile":
                profile_config = profiles.get(opt_name, {})
                create_session(self.window, profile=profile_config, backend=backend)
            elif opt_type == "checkpoint":
                checkpoint = checkpoints.get(opt_name, {})
                session_id = checkpoint.get("session_id")
                if session_id:
                    create_session(self.window, resume_id=session_id, fork=True, backend=backend)
                else:
                    sublime.error_message(f"Checkpoint '{opt_name}' has no session_id")

        self.window.show_quick_panel(items, on_select)

    def _show_persona_picker(self, persona_url: str) -> None:
        """Show list of personas to pick from."""
        from . import persona_client
        import threading

        def fetch_and_show():
            personas = persona_client.list_personas(persona_url)
            if not personas:
                sublime.set_timeout(lambda: sublime.status_message("No personas available"), 0)
                return

            # Build options: unlocked first, then locked
            unlocked = [p for p in personas if not p.get("is_locked")]
            locked = [p for p in personas if p.get("is_locked")]

            options = []
            for p in unlocked:
                tags = ", ".join(p.get("tags", [])) if p.get("tags") else ""
                desc = p.get("notes", tags) or "No description"
                options.append((p["id"], f"👤 {p['alias']}", desc[:60]))

            for p in locked:
                locked_by = p.get("locked_by_session", "unknown")
                options.append((p["id"], f"🔒 {p['alias']}", f"Locked by {locked_by}"))

            def show_panel():
                items = [[opt[1], opt[2]] for opt in options]

                def on_select(idx):
                    if idx < 0:
                        return
                    persona_id = options[idx][0]
                    self._start_with_persona(persona_id, persona_url)

                self.window.show_quick_panel(items, on_select)

            sublime.set_timeout(show_panel, 0)

        threading.Thread(target=fetch_and_show, daemon=True).start()

    def _start_with_persona(self, persona_id: int, persona_url: str = None) -> None:
        """Acquire persona and start session."""
        from . import persona_client
        from .settings import load_project_settings
        import threading
        import os

        if not persona_url:
            cwd = self.window.folders()[0] if self.window.folders() else None
            settings = load_project_settings(cwd)
            persona_url = settings.get("persona_url")

        if not persona_url:
            sublime.error_message("persona_url not configured in settings")
            return

        def acquire_and_start():
            # Generate session ID for locking
            import uuid
            session_id = f"sublime-{uuid.uuid4().hex[:8]}"

            result = persona_client.acquire_persona(session_id, persona_id=persona_id, base_url=persona_url)

            if "error" in result:
                sublime.set_timeout(
                    lambda: sublime.error_message(f"Failed to acquire persona: {result['error']}"),
                    0
                )
                return

            persona = result.get("persona", {})
            ability = result.get("ability", {})
            handoff_notes = result.get("handoff_notes")

            # Build profile config from persona
            profile_config = {
                "model": ability.get("model", "sonnet"),
                "system_prompt": ability.get("system_prompt", ""),
                "persona_id": persona_id,
                "persona_session_id": session_id,
                "persona_url": persona_url,
                "description": f"Persona: {persona.get('alias', 'unknown')}"
            }

            def start():
                s = create_session(self.window, profile=profile_config)
                # Show handoff notes if present
                if handoff_notes:
                    s.output.text(f"\n*Handoff notes:* {handoff_notes}\n")
                sublime.status_message(f"Acquired persona: {persona.get('alias', 'unknown')}")

            sublime.set_timeout(start, 0)

        threading.Thread(target=acquire_and_start, daemon=True).start()


class CodexStartCommand(sublime_plugin.WindowCommand):
    """Start a new Codex session."""
    def run(self) -> None:
        import shutil
        if not shutil.which("codex"):
            sublime.error_message("Codex CLI not found. Install from: https://github.com/openai/codex")
            return
        create_session(self.window, backend="codex")


class ClaudeCodeQueryCommand(sublime_plugin.WindowCommand):
    """Open input for query (focuses output and enters input mode)."""
    def run(self) -> None:
        s = get_active_session(self.window) or create_session(self.window)
        s.output.show()
        s._enter_input_with_draft()


class ClaudeCodeRestartCommand(sublime_plugin.WindowCommand):
    """Restart session, keeping the output view."""
    def run(self) -> None:

        old_session = get_active_session(self.window)
        old_view = None

        if old_session:
            old_view = old_session.output.view
            old_session.stop()
            if old_view and old_view.id() in sublime._claude_sessions:
                del sublime._claude_sessions[old_view.id()]

        # Create new session
        new_session = Session(self.window)

        # Reuse existing view if available
        if old_view and old_view.is_valid():
            new_session.output.view = old_view
            new_session.output.clear()
            sublime._claude_sessions[old_view.id()] = new_session

        new_session.start()
        if new_session.output.view:
            new_session.output.view.set_name("Claude")
            if new_session.output.view.id() not in sublime._claude_sessions:
                sublime._claude_sessions[new_session.output.view.id()] = new_session
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
        q = PromptBuilder.selection_query(prompt, fname, selection)
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
        q = PromptBuilder.file_query(prompt, fname, content)
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
        if not s:
            return
        # If in input mode with text, clear the input instead of interrupting
        if s.output.is_input_mode() and s.output.get_input_text().strip():
            view = s.output.view
            start = s.output._input_start
            view.run_command("claude_replace", {
                "start": start,
                "end": view.size(),
                "text": ""
            })
            view.sel().clear()
            view.sel().add(sublime.Region(start, start))
            return
        s.interrupt()


class ClaudeCloseSessionCommand(sublime_plugin.TextCommand):
    """Close Claude session view with confirmation."""
    def run(self, edit):
        view = self.view
        session = sublime._claude_sessions.get(view.id())
        if not session or not session.initialized:
            view.close()
            return
        # Use set_timeout so the dialog doesn't block the command dispatch loop.
        # Blocking mid-dispatch can cause the next Cmd+W to bypass our keybinding.
        def _ask():
            # Re-check session (may have closed in the meantime)
            s = sublime._claude_sessions.get(view.id())
            if not s or not s.initialized:
                view.close()
                return
            if sublime.ok_cancel_dialog("Close this Claude session?", "Close"):
                s.stop()
                if view.id() in sublime._claude_sessions:
                    del sublime._claude_sessions[view.id()]
                view.close()
        sublime.set_timeout(_ask, 0)


class ClaudeCodeClearCommand(sublime_plugin.WindowCommand):
    def run(self) -> None:
        s = get_active_session(self.window)
        if s:
            s.output.clear()


class ClaudeCodeCompactCommand(sublime_plugin.WindowCommand):
    """Manually trigger conversation compaction/summarization."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if s:
            # Gather pre-compact prompts from multiple sources
            from .hooks import get_project_hook_prompt, combine_hook_prompts
            import os

            prompts = []
            cwd = self.window.folders()[0] if self.window.folders() else None

            # 1. Static retain file (.claude/RETAIN.md)
            if cwd:
                static_path = os.path.join(cwd, ".claude", "RETAIN.md")
                if os.path.exists(static_path):
                    try:
                        with open(static_path, "r") as f:
                            content = f.read().strip()
                        if content:
                            prompts.append(content)
                    except:
                        pass

            # 2. Session retain file
            session_retain = s.retain()
            if session_retain:
                prompts.append(session_retain)

            # 3. Project-level hook file (.claude/hooks/pre-compact)
            if cwd:
                project_prompt = get_project_hook_prompt("pre-compact", cwd)
                if project_prompt:
                    prompts.append(project_prompt)

            # 4. Project-level settings (.claude/settings.json)
            if cwd:
                from .settings import load_project_settings
                project_settings = load_project_settings(cwd)
                if project_settings and "pre_compact_prompt" in project_settings:
                    settings_prompt = project_settings["pre_compact_prompt"]
                    if settings_prompt:
                        prompts.append(settings_prompt)

            # 5. Profile-level pre-compact prompt
            if s.profile and "pre_compact_prompt" in s.profile:
                profile_prompt = s.profile["pre_compact_prompt"]
                if profile_prompt:
                    prompts.append(profile_prompt)

            # Combine and inject if we have any prompts
            combined = combine_hook_prompts(prompts)
            if combined:
                # Inject combined prompt before compaction
                s.queue_prompt(combined)
                # Wait a bit for the inject to be processed, then compact
                import sublime
                sublime.set_timeout(lambda: s.query("/compact"), 500)
                return

            # No prompts - just compact
            s.query("/compact")


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
        import os
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
        import json, os

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

        s = get_active_session(self.window)
        if s and s.output.view:
            view_id = s.output.view.id()
            s.stop()
            if view_id in sublime._claude_sessions:
                del sublime._claude_sessions[view_id]


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
    def run(self, backend: str = "claude") -> None:
        import os
        import shutil
        from .core import create_session

        backend_prefix = f"[{backend}] " if backend != "claude" else ""
        has_codex = bool(shutil.which("codex"))

        # Get all sessions in this window
        sessions_in_window = []
        for view_id, session in sublime._claude_sessions.items():
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
        current_file = current_view.file_name() if current_view else None

        # Add "New Session with This File" option when in a non-session file
        if not in_output_view and current_file:
            filename = os.path.basename(current_file)
            items.append([f"📎 {backend_prefix}New with ctx:{filename}", "Create session with this file as context"])
            actions.append(("new_with_file", current_file))

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
            items.append(["🔄 Restart Session", "Restart current session, keep output"])
            actions.append(("restart", active_session))

        # Add profiles and checkpoints
        from .settings import load_profiles_and_checkpoints

        # Get project profiles path
        project_path = None
        if self.window.folders():
            project_path = os.path.join(self.window.folders()[0], ".claude", "profiles.json")

        profiles, checkpoints = load_profiles_and_checkpoints(project_path)

        for name, config in profiles.items():
            desc = config.get("description", f"{config.get('model', 'default')} model")
            items.append([f"😶 {backend_prefix}{name}", desc])
            actions.append(("profile", config))

        for name, config in checkpoints.items():
            desc = config.get("description", "Saved checkpoint")
            items.append([f"📍 {backend_prefix}{name}", desc])
            actions.append(("checkpoint", config))

        # Add "From Persona" option
        sublime_settings = sublime.load_settings("ClaudeCode.sublime-settings")
        persona_url = sublime_settings.get("persona_url", "http://localhost:5002/personas")
        items.append(["👤 From Persona...", "Acquire a persona identity"])
        actions.append(("persona", persona_url))

        # Add "New Session" option
        items.append([f"🆕 {backend_prefix}New Session", "Start fresh with default settings"])
        actions.append(("new", None))

        # Add "Fork Session" option when in a session window
        if in_output_view and active_session:
            items.append(["🍴 Fork Session", "Create new session with copy of history"])
            actions.append(("fork", active_session))

        # Add "Switch Backend" option
        if has_codex:
            other = "codex" if backend == "claude" else "claude"
            items.append([f"⇄ Switch to {other}", f"Show {other} options"])
            actions.append(("switch_backend", other))

        def on_select(idx):
            if idx >= 0:
                action, data = actions[idx]
                if action == "switch_backend":
                    # Re-open panel with new backend
                    sublime.set_timeout(lambda: self.run(backend=data), 0)
                    return
                if action == "restart" and data:
                    # Show profile picker for restart
                    self._show_restart_picker(data, profiles, checkpoints)
                elif action == "new_with_file" and data:
                    # Create new session with current file as context
                    s = create_session(self.window, backend=backend)
                    # Read file content and add to context
                    try:
                        with open(data, "r", encoding="utf-8") as f:
                            content = f.read()
                        s.add_context_file(data, content)
                    except Exception as e:
                        print(f"[Claude] Error adding file context: {e}")
                elif action == "new":
                    create_session(self.window, backend=backend)
                elif action == "profile":
                    create_session(self.window, profile=data, backend=backend)
                elif action == "checkpoint":
                    session_id = data.get("session_id")
                    if session_id:
                        create_session(self.window, resume_id=session_id, fork=True, backend=backend)
                elif action == "fork" and data:
                    # Fork the current session
                    if data.session_id:
                        create_session(self.window, resume_id=data.session_id, fork=True)
                elif action == "persona" and data:
                    # Show persona picker
                    self._show_persona_picker(data)
                elif action == "focus" and data:
                    data.output.show()

        self.window.show_quick_panel(items, on_select)

    def _show_persona_picker(self, persona_url: str) -> None:
        """Show list of personas to pick from."""
        from . import persona_client
        from .core import create_session
        import threading

        def fetch_and_show():
            personas = persona_client.list_personas(persona_url)
            if not personas:
                sublime.set_timeout(lambda: sublime.status_message("No personas available"), 0)
                return

            # Build options: unlocked first, then locked
            unlocked = [p for p in personas if not p.get("is_locked")]
            locked = [p for p in personas if p.get("is_locked")]

            options = []
            for p in unlocked:
                tags = ", ".join(p.get("tags", [])) if p.get("tags") else ""
                desc = p.get("notes", tags) or "No description"
                options.append((p["id"], f"👤 {p['alias']}", desc[:60]))

            for p in locked:
                locked_by = p.get("locked_by_session", "unknown")
                options.append((p["id"], f"🔒 {p['alias']}", f"Locked by {locked_by}"))

            def show_panel():
                items = [[opt[1], opt[2]] for opt in options]

                def on_select(idx):
                    if idx < 0:
                        return
                    persona_id = options[idx][0]
                    self._start_with_persona(persona_id, persona_url)

                self.window.show_quick_panel(items, on_select)

            sublime.set_timeout(show_panel, 0)

        threading.Thread(target=fetch_and_show, daemon=True).start()

    def _start_with_persona(self, persona_id: int, persona_url: str) -> None:
        """Acquire persona and start session."""
        from . import persona_client
        from .core import create_session
        import threading
        import uuid

        def acquire_and_start():
            session_id = f"sublime-{uuid.uuid4().hex[:8]}"

            result = persona_client.acquire_persona(session_id, persona_id=persona_id, base_url=persona_url)

            if "error" in result:
                sublime.set_timeout(
                    lambda: sublime.error_message(f"Failed to acquire persona: {result['error']}"),
                    0
                )
                return

            persona = result.get("persona", {})
            ability = result.get("ability", {})
            handoff_notes = result.get("handoff_notes")

            profile_config = {
                "model": ability.get("model", "sonnet"),
                "system_prompt": ability.get("system_prompt", ""),
                "persona_id": persona_id,
                "persona_session_id": session_id,
                "persona_url": persona_url,
                "description": f"Persona: {persona.get('alias', 'unknown')}"
            }

            def start():
                s = create_session(self.window, profile=profile_config)
                if handoff_notes:
                    s.output.text(f"\n*Handoff notes:* {handoff_notes}\n")
                sublime.status_message(f"Acquired persona: {persona.get('alias', 'unknown')}")

            sublime.set_timeout(start, 0)

        threading.Thread(target=acquire_and_start, daemon=True).start()

    def _show_restart_picker(self, session, profiles, checkpoints):
        """Show profile/checkpoint picker for restart."""
        from .core import create_session

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
            if old_view and old_view.id() in sublime._claude_sessions:
                del sublime._claude_sessions[old_view.id()]

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
                sublime._claude_sessions[old_view.id()] = new_session

            new_session.start()
            if new_session.output.view:
                new_session.output.view.set_name("Claude")
                if new_session.output.view.id() not in sublime._claude_sessions:
                    sublime._claude_sessions[new_session.output.view.id()] = new_session
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

        # Combine active sessions and saved sessions
        items = []
        sources = []

        # Active sessions in this window
        for view_id, session in sublime._claude_sessions.items():
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


class ClaudeGarageSearchCommand(sublime_plugin.WindowCommand):
    """Search indexed sessions with garage CLI and fork/resume."""

    def run(self) -> None:
        self.window.show_input_panel(
            "Search sessions:",
            "",
            self._on_query,
            None,
            None
        )

    def _on_query(self, query: str) -> None:
        if not query.strip():
            return

        import subprocess
        try:
            result = subprocess.run(
                ["garage", "search", query, "--k", "10"],
                capture_output=True,
                text=True,
                timeout=10
            )
            # garage may crash partway through but still output useful results
            # so we parse stdout regardless of return code
            if result.stdout.strip():
                self._parse_and_show(result.stdout, query)
            elif result.returncode != 0:
                sublime.error_message(f"garage search failed: {result.stderr}")
        except FileNotFoundError:
            sublime.error_message("garage CLI not found. Install it first.")
        except subprocess.TimeoutExpired:
            sublime.error_message("garage search timed out")

    def _parse_and_show(self, output: str, query: str) -> None:
        """Parse garage search output and show quick panel."""
        import re
        # New format: 1. [0.696] 2ccb865b  [pil]  Turns: 268
        #                - Summary text here...
        # Old format: 1. [0.610] f400b570
        #                Project: /path/to/project
        #                Created: 2026-01-17T02:38:12.192Z  Turns: 41
        results = []
        lines = output.strip().split("\n")
        i = 0
        while i < len(lines):
            # Try new format: 1. [0.696] 2ccb865b  [pil]  Turns: 268
            #                   - Summary...
            #                   ID: full-uuid-here
            new_match = re.match(r'\d+\.\s+\[([0-9.]+)\]\s+([a-f0-9]+)\s+\[([^\]]+)\]\s+Turns:\s*(\d+)', lines[i])
            if new_match:
                score = float(new_match.group(1))
                short_id = new_match.group(2)
                project = new_match.group(3)
                turns = int(new_match.group(4))
                summary = ""
                full_id = short_id  # Default to short if full not found
                # Parse following lines for summary and full ID
                while i + 1 < len(lines) and not re.match(r'\d+\.', lines[i + 1]):
                    i += 1
                    line = lines[i].strip()
                    if line.startswith("- "):
                        summary = line[2:]  # Remove "- " prefix
                    elif line.startswith("ID: "):
                        full_id = line[4:]  # Full UUID
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

            # Try old format: 1. [0.610] f400b570
            old_match = re.match(r'\d+\.\s+\[([0-9.]+)\]\s+([a-f0-9]+)', lines[i])
            if old_match:
                score = float(old_match.group(1))
                session_id = old_match.group(2)
                project = ""
                turns = 0
                summary = ""
                # Parse following lines for metadata
                while i + 1 < len(lines) and not re.match(r'\d+\.', lines[i + 1]):
                    i += 1
                    line = lines[i].strip()
                    if line.startswith("Project:"):
                        project = line.replace("Project:", "").strip()
                    elif line.startswith("Created:"):
                        if "Turns:" in line:
                            turns = int(line.split("Turns:")[-1].strip())
                results.append({
                    "session_id": session_id,
                    "score": score,
                    "project": project,
                    "turns": turns,
                    "summary": summary,
                })
            i += 1

        if not results:
            sublime.status_message("No sessions found")
            return

        # Build quick panel items
        items = []
        for r in results:
            import os
            proj_name = os.path.basename(r["project"]) if r["project"] else r["project"]
            summary = r.get("summary", "")
            if len(summary) > 80:
                summary = summary[:77] + "..."
            short_id = r.get("short_id", r["session_id"][:8])
            items.append([
                f"[{r['score']:.2f}] {short_id}  [{proj_name}]  {r['turns']} turns",
                summary or "(no summary)"
            ])

        def on_select(idx):
            if idx >= 0:
                self._show_action_panel(results[idx])

        self.window.show_quick_panel(items, on_select, placeholder=f"Results for: {query}")

    def _show_action_panel(self, result: dict) -> None:
        """Show fork/resume options for selected session."""
        session_id = result["session_id"]  # Full UUID
        short_id = result.get("short_id", session_id[:8])

        items = [
            ["Fork", f"Create new session branching from {short_id}"],
            ["Resume", f"Continue session {short_id} (same ID)"],
        ]

        def on_action(idx):
            if idx == 0:
                # Fork
                s = create_session(self.window, resume_id=session_id, fork=True)
                s.name = f"fork:{short_id}"
                s.output.set_name(s.name)
                sublime.status_message(f"Forked session {short_id}")
            elif idx == 1:
                # Resume
                s = create_session(self.window, resume_id=session_id, fork=False)
                s.name = f"resume:{short_id}"
                s.output.set_name(s.name)
                sublime.status_message(f"Resumed session {short_id}")

        self.window.show_quick_panel(items, on_action)


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

        # Check for question free-text input first
        if s.output.submit_question_input():
            return

        if not s.output.is_input_mode():
            return

        text = s.output.get_input_text().strip()

        # Ignore empty input
        if not text:
            return

        # Check for slash commands
        cmd = CommandParser.parse(text)
        if cmd:
            s.output.exit_input_mode(keep_text=False)
            s.draft_prompt = ""
            self._handle_command(s, cmd)
            return

        s.output.exit_input_mode(keep_text=False)
        s.draft_prompt = ""

        # If session is working, queue the prompt instead
        if s.working:
            s.queue_prompt(text)
        else:
            s.query(text)

    def _handle_command(self, session, cmd):
        """Handle a slash command."""
        if cmd.name == "clear":
            self._cmd_clear(session)
        elif cmd.name == "compact":
            self._cmd_compact(session)
        elif cmd.name == "context":
            self._cmd_context(session)
        else:
            # Unknown command - send as regular prompt to Claude
            session.query(cmd.raw)

    def _cmd_clear(self, session):
        """Clear conversation history."""
        session.output.clear()
        sublime.status_message("Claude: conversation cleared")

    def _cmd_compact(self, session):
        """Send /compact to Claude for context summarization."""
        session.query("/compact", display_prompt="/compact")

    def _cmd_context(self, session):
        """Show pending context items."""
        if not session.pending_context:
            session.output.text("\n*No pending context.*\n")
        else:
            lines = ["\n*Pending context:*"]
            for item in session.pending_context:
                lines.append(f"  📎 {item.name}")
            lines.append("")
            session.output.text("\n".join(lines))
        session.output.enter_input_mode()


class ClaudeEnterInputModeCommand(sublime_plugin.TextCommand):
    """Enter input mode in the Claude output view."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            if s.working:
                return  # Don't enter input mode while busy
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


class ClaudeInsertCommand(sublime_plugin.TextCommand):
    """Insert text at position in Claude output view."""
    def run(self, edit, pos, text):
        self.view.insert(edit, pos, text)


class ClaudeReplaceCommand(sublime_plugin.TextCommand):
    """Replace region in Claude output view."""
    def run(self, edit, start, end, text):
        self.view.replace(edit, sublime.Region(start, end), text)


class ClaudeReplaceContentCommand(sublime_plugin.TextCommand):
    """Replace entire view content."""
    def run(self, edit, content):
        self.view.replace(edit, sublime.Region(0, self.view.size()), content)


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
    """Handle Y key - allow permission or approve plan."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            if not s.output.handle_plan_key("y"):
                s.output.handle_permission_key("y")


class ClaudePermissionDenyCommand(sublime_plugin.TextCommand):
    """Handle N key - deny permission or reject plan."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            if not s.output.handle_plan_key("n"):
                s.output.handle_permission_key("n")


class ClaudeViewPlanCommand(sublime_plugin.TextCommand):
    """Handle V key - view plan file."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            s.output.handle_plan_key("v")


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


class ClaudeQuestionKeyCommand(sublime_plugin.TextCommand):
    """Handle number/o/enter keys for inline question UI."""
    def run(self, edit, key=""):
        s = get_session_for_view(self.view)
        if s:
            s.output.handle_question_key(key)


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

class ClaudeCodeManageAutoAllowedToolsCommand(sublime_plugin.WindowCommand):
    """Manage auto-allowed MCP tools for the current project."""

    def run(self):
        """Show quick panel to manage auto-allowed tools."""
        import os
        import json

        # Get project settings path
        folders = self.window.folders()
        if not folders:
            sublime.error_message("No project folder open")
            return

        project_dir = folders[0]
        settings_dir = os.path.join(project_dir, ".claude")
        settings_path = os.path.join(settings_dir, "settings.json")

        # Load current settings
        settings = {}
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r") as f:
                    settings = json.load(f)
            except Exception as e:
                print(f"[Claude] Error loading settings: {e}")

        auto_allowed = settings.get("autoAllowedMcpTools", [])

        # Build options
        options = []
        options.append(("add", None, "➕ Add new pattern", "Add a new MCP tool pattern to auto-allow"))

        # Show current patterns
        for i, pattern in enumerate(auto_allowed):
            options.append(("remove", i, f"❌ Remove: {pattern}", "Click to remove this pattern"))

        if not auto_allowed:
            options.append(("info", None, "ℹ️  No patterns configured", "Add patterns to auto-allow MCP tools"))

        # Show quick panel
        items = [[opt[2], opt[3]] for opt in options]

        def on_select(idx):
            if idx < 0:
                return

            action, data, _, _ = options[idx]

            if action == "add":
                self.show_add_pattern_input(settings_path, settings, auto_allowed)
            elif action == "remove":
                self.remove_pattern(settings_path, settings, auto_allowed, data)

        self.window.show_quick_panel(items, on_select)

    def show_add_pattern_input(self, settings_path, settings, auto_allowed):
        """Show input panel to add a new pattern."""
        # Build common patterns list
        # Format: "Tool" or "Tool(specifier)" where specifier can be:
        #   - exact match: "Bash(git status)"
        #   - prefix match: "Bash(git:*)" matches commands starting with "git"
        #   - glob pattern: "Read(/src/**/*.py)"
        common_patterns = [
            "mcp__*__*",  # All MCP tools
            "mcp__plugin_*",  # All plugin MCP tools
            "Bash(git:*)",  # Git commands only
            "Bash(ls:*)",  # ls commands
            "Bash(cat:*)",  # cat commands
            "Bash(python:*)",  # python commands
            "Bash(npm:*)",  # npm commands
            "Read",  # All Read
            "Write",  # All Write
        ]

        # Show quick panel with common patterns + custom option
        items = []
        items.append(["✏️ Enter custom pattern", "Type your own pattern"])
        for pattern in common_patterns:
            items.append([f"Add: {pattern}", "Common pattern"])

        def on_select_pattern(idx):
            if idx < 0:
                return

            if idx == 0:
                # Custom pattern
                self.window.show_input_panel(
                    "Enter MCP tool pattern (supports wildcards like mcp__*__):",
                    "",
                    lambda pattern: self.add_pattern(settings_path, settings, auto_allowed, pattern),
                    None,
                    None
                )
            else:
                # Use common pattern
                pattern = common_patterns[idx - 1]
                self.add_pattern(settings_path, settings, auto_allowed, pattern)

        self.window.show_quick_panel(items, on_select_pattern)

    def add_pattern(self, settings_path, settings, auto_allowed, pattern):
        """Add a pattern to auto-allowed tools."""
        import os
        import json

        if not pattern or not pattern.strip():
            return

        pattern = pattern.strip()

        if pattern in auto_allowed:
            sublime.status_message(f"Pattern already exists: {pattern}")
            return

        # Add pattern
        auto_allowed.append(pattern)
        settings["autoAllowedMcpTools"] = auto_allowed

        # Save settings
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        try:
            with open(settings_path, "w") as f:
                json.dump(settings, f, indent=2)
            sublime.status_message(f"Added auto-allow pattern: {pattern}")
        except Exception as e:
            sublime.error_message(f"Failed to save settings: {e}")

    def remove_pattern(self, settings_path, settings, auto_allowed, index):
        """Remove a pattern from auto-allowed tools."""
        import json

        if 0 <= index < len(auto_allowed):
            pattern = auto_allowed.pop(index)
            settings["autoAllowedMcpTools"] = auto_allowed

            # Save settings
            try:
                with open(settings_path, "w") as f:
                    json.dump(settings, f, indent=2)
                sublime.status_message(f"Removed auto-allow pattern: {pattern}")
            except Exception as e:
                sublime.error_message(f"Failed to save settings: {e}")


class ClaudeAddOrderCommand(sublime_plugin.TextCommand):
    """Add an order at current caret position to the order table."""

    def run(self, edit):
        import os
        from .order_table import get_table, refresh_order_table

        sel = self.view.sel()
        if not sel:
            return

        region = sel[0]
        point = region.begin()
        row, col = self.view.rowcol(point)
        file_path = self.view.file_name()
        selection_length = region.size() if not region.empty() else None

        if not file_path:
            sublime.status_message("Cannot add order: file not saved")
            return

        basename = os.path.basename(file_path)
        self.view.window().show_input_panel(
            f"Order at {basename}:{row+1}:",
            "",
            lambda prompt: self._on_done(prompt, file_path, row, col, selection_length),
            None,
            None
        )

    def _on_done(self, prompt, file_path, row, col, selection_length):
        from .order_table import get_table, refresh_order_table

        if not prompt or not prompt.strip():
            return

        window = self.view.window()
        table = get_table(window)
        if not table:
            sublime.status_message("No project folder")
            return

        order = table.add(prompt.strip(), file_path, row, col, selection_length, view=self.view)
        refresh_order_table(window)
        sublime.status_message(f"Order added: {order.id}")


class ClaudeAddPlainOrderCommand(sublime_plugin.WindowCommand):
    """Add an order without file location."""

    def run(self):
        from .order_table import get_table, show_order_table

        table = get_table(self.window)
        if not table:
            sublime.status_message("No project folder")
            return

        self.window.show_input_panel(
            "Order:",
            "",
            lambda prompt: self._on_done(prompt),
            None,
            None
        )

    def _on_done(self, prompt):
        from .order_table import get_table, refresh_order_table

        if not prompt or not prompt.strip():
            return

        table = get_table(self.window)
        if not table:
            return

        order = table.add(prompt.strip())
        refresh_order_table(self.window)
        sublime.status_message(f"Order added: {order.id}")


class ClaudeShowOrderTableCommand(sublime_plugin.WindowCommand):
    """Show the order table view."""

    def run(self):
        from .order_table import show_order_table
        view = show_order_table(self.window)
        if not view:
            sublime.status_message("No project folder")


class ClaudeOrderGotoCommand(sublime_plugin.TextCommand):
    """Jump to the order or edit location under cursor."""

    def run(self, edit):
        import re
        from .order_table import get_table

        if not self.view.settings().get("order_table_view"):
            return

        # Get current line
        sel = self.view.sel()
        if not sel:
            return
        line_region = self.view.line(sel[0])
        line = self.view.substr(line_region)

        print(f"[OrderGoto] line={line!r}")

        # Check if it's an edit entry: file:line ... (not an order line)
        edit_match = re.match(r'\s+(.+?):(\d+)\s+', line)
        print(f"[OrderGoto] edit_match={edit_match}, groups={edit_match.groups() if edit_match else None}")
        if edit_match and '[order_' not in line:
            rel_path = edit_match.group(1).strip()
            line_num = int(edit_match.group(2))
            print(f"[OrderGoto] rel_path={rel_path!r}, line_num={line_num}")
            # Find full path and edit entry from edits
            edit_entry = self._find_edit_entry(rel_path, line_num)
            print(f"[OrderGoto] edit_entry={edit_entry}")
            if edit_entry:
                file_path = edit_entry["file_path"]
                # Also reveal in agent's session view (without focus)
                self._reveal_in_session(edit_entry)
                # Open file in code view (with focus)
                self._open_in_main_group(file_path, line_num, 1)
            return

        # Extract order_id from line like "  [order_1] @ file.py:10"
        match = re.search(r'\[(order_\d+)\]', line)
        if not match:
            return

        order_id = match.group(1)
        table = get_table(self.view.window())
        if not table:
            return

        # Find the order
        for o in table.list():
            if o["id"] == order_id and o.get("file_path"):
                file_path = o["file_path"]
                row = o.get("row", 0)
                col = o.get("col", 0)
                self._open_in_main_group(file_path, row + 1, col + 1)
                return

        sublime.status_message("Order has no location")

    def _open_in_main_group(self, file_path: str, row: int, col: int):
        """Open file in main editing group, not the order table's group."""
        window = self.view.window()
        if not window:
            return

        # Get order table's group
        order_group, _ = window.get_view_index(self.view)

        # Find a different group (prefer group 0 as main editing area)
        target_group = 0
        if order_group == 0 and window.num_groups() > 1:
            target_group = 1

        # Focus target group before opening
        window.focus_group(target_group)
        window.open_file(f"{file_path}:{row}:{col}", sublime.ENCODED_POSITION)

    def _find_edit_entry(self, rel_path: str, line_num: int):
        """Find edit entry from relative path and line number."""
        from .order_table import get_table, _relative_path

        window = self.view.window()
        folders = window.folders() if window else []
        table = get_table(window)
        if not table:
            return None

        # Handle truncated paths (starting with ...)
        if rel_path.startswith("..."):
            suffix = rel_path[3:]
            for e in table.list_edits():
                full_rel = _relative_path(e["file_path"], folders)
                if full_rel.endswith(suffix) and e["line_num"] == line_num:
                    return e
        else:
            for e in table.list_edits():
                if _relative_path(e["file_path"], folders) == rel_path and e["line_num"] == line_num:
                    return e
        return None

    def _reveal_in_session(self, edit_entry: dict):
        """Reveal the edit in the agent's session view without focusing it."""
        import os

        agent_view_id = edit_entry.get("agent_view_id", 0)
        if not agent_view_id:
            return

        # Find the agent's session
        if not hasattr(sublime, '_claude_sessions') or agent_view_id not in sublime._claude_sessions:
            return

        session = sublime._claude_sessions[agent_view_id]
        if not session.output.view or not session.output.view.is_valid():
            return

        session_view = session.output.view
        file_basename = os.path.basename(edit_entry["file_path"])
        line_num = edit_entry["line_num"]
        tool = edit_entry.get("tool", "Edit")

        # Search for the edit in session view
        # Look for patterns like "✔ Edit: /path/to/file.py:123" or "✔ Write: /path/to/file.py"
        content = session_view.substr(sublime.Region(0, session_view.size()))

        # Try multiple patterns
        patterns = [
            f"{tool}: {edit_entry['file_path']}:{line_num}",  # Full path with line
            f"{tool}: {edit_entry['file_path']}",  # Full path without line
            f"{tool}: {file_basename}:{line_num}",  # Basename with line
        ]

        found_pos = -1
        for pattern in patterns:
            pos = content.rfind(pattern)  # Find last occurrence (most recent)
            if pos >= 0:
                found_pos = pos
                break

        if found_pos >= 0:
            # Reveal without focusing - show the region but keep current focus
            region = sublime.Region(found_pos, found_pos + len(patterns[0]))
            session_view.show_at_center(region)
            # Add a brief highlight
            session_view.add_regions(
                "claude_edit_highlight",
                [sublime.Region(found_pos, session_view.line(found_pos).end())],
                "region.yellowish",
                "",
                sublime.DRAW_NO_FILL | sublime.DRAW_SOLID_UNDERLINE
            )
            # Clear highlight after a moment
            sublime.set_timeout(lambda: session_view.erase_regions("claude_edit_highlight"), 2000)


class ClaudeOrderDeleteCommand(sublime_plugin.TextCommand):
    """Delete order(s) - uses selection to determine which items to delete."""

    def run(self, edit):
        import re
        from .order_table import get_table, refresh_order_table

        if not self.view.settings().get("order_table_view"):
            return

        window = self.view.window()
        table = get_table(window)
        if not table:
            return

        sel = self.view.sel()
        if not sel:
            return

        # Collect order IDs from all selected lines (use set to dedupe)
        order_ids = set()
        for region in sel:
            for line_region in self.view.lines(region):
                line = self.view.substr(line_region)
                match = re.search(r'\[(order_\d+)\]', line)
                if match:
                    order_ids.add(match.group(1))

        if not order_ids:
            sublime.status_message("No orders in selection")
            return

        # Save cursor row for restoration after refresh
        cursor_row, _ = self.view.rowcol(sel[0].begin())

        # Delete all found orders
        deleted = 0
        for order_id in order_ids:
            ok, _ = table.delete(order_id)
            if ok:
                deleted += 1

        if deleted:
            sublime.status_message(f"Deleted {deleted} order(s) (u to undo)")

            def refresh_and_restore():
                refresh_order_table(window)
                # Restore cursor to same row (clamped to valid range)
                if self.view.is_valid():
                    max_row = self.view.rowcol(self.view.size())[0]
                    row = min(cursor_row, max_row)
                    pt = self.view.text_point(row, 0)
                    self.view.sel().clear()
                    self.view.sel().add(sublime.Region(pt, pt))

            sublime.set_timeout(refresh_and_restore, 10)
        else:
            sublime.status_message("No orders deleted")


class ClaudeOrderUndoCommand(sublime_plugin.TextCommand):
    """Undo last order deletion."""

    def run(self, edit):
        from .order_table import get_table, refresh_order_table

        if not self.view.settings().get("order_table_view"):
            return

        window = self.view.window()
        table = get_table(window)
        if not table:
            return

        ok, msg = table.undo_delete()
        sublime.status_message(msg)
        if ok:
            sublime.set_timeout(lambda: refresh_order_table(window), 10)


class ClaudeOrderClearDoneCommand(sublime_plugin.TextCommand):
    """Clear all done orders."""

    def run(self, edit):
        from .order_table import get_table, refresh_order_table

        if not self.view.settings().get("order_table_view"):
            return

        window = self.view.window()
        table = get_table(window)
        if not table:
            return

        count = table.clear_done()
        sublime.status_message(f"Cleared {count} done orders")
        if count > 0:
            sublime.set_timeout(lambda: refresh_order_table(window), 10)


class ClaudeEditMessageCommand(sublime_plugin.TextCommand):
    """Send a message to the agent who made an edit."""

    def run(self, edit):
        import re
        import os
        from .order_table import get_table, _relative_path

        if not self.view.settings().get("order_table_view"):
            return

        # Get current line
        sel = self.view.sel()
        if not sel:
            return

        window = self.view.window()
        table = get_table(window)
        if not table:
            return

        folders = window.folders() if window else []

        # Find which edit entry is selected
        line_region = self.view.line(sel[0])
        line = self.view.substr(line_region)

        # Parse edit line: file:line ... [agent_id]
        edit_match = re.match(r'\s+(.+?):(\d+)\s+', line)
        if not edit_match or '[order_' in line:
            sublime.status_message("Place cursor on an edit entry")
            return

        rel_path = edit_match.group(1).strip()
        line_num = int(edit_match.group(2))

        # Find the specific edit
        target_edit = None
        for e in table.list_edits():
            full_rel = _relative_path(e["file_path"], folders)
            if rel_path.startswith("..."):
                matches = full_rel.endswith(rel_path[3:])
            else:
                matches = full_rel == rel_path
            if matches and e["line_num"] == line_num:
                target_edit = e
                break

        if not target_edit:
            sublime.status_message("Could not find edit entry")
            return

        # Find the agent's session
        agent_view_id = target_edit.get("agent_view_id", 0)

        if not agent_view_id or agent_view_id not in sublime._claude_sessions:
            # Agent gone - offer to open file instead
            file_path = target_edit.get("file_path")
            line_num = target_edit.get("line_num", 1)
            if file_path:
                self._open_in_main_group(window, file_path, line_num)
                sublime.status_message(f"Agent {agent_view_id} gone, opened file")
            else:
                sublime.status_message(f"Agent session not found: {agent_view_id}")
            return

        session = sublime._claude_sessions[agent_view_id]

        # Show input panel to compose message
        file_basename = os.path.basename(target_edit["file_path"])
        edit_line_num = target_edit["line_num"]
        context = target_edit.get("context", "")[:40]

        def on_done(message):
            if not message.strip():
                return
            # Build context message about the edit
            full_message = f"About your edit to {file_basename}:{edit_line_num}"
            if context:
                full_message += f" ({context})"
            full_message += f": {message}"

            if session.working:
                session.queue_prompt(full_message)
                sublime.status_message(f"Message queued for agent {agent_view_id}")
            else:
                session.query(full_message)
                sublime.status_message(f"Message sent to agent {agent_view_id}")

        window.show_input_panel(
            f"Message to agent {agent_view_id} about edit:",
            "",
            on_done,
            None,
            None
        )

    def _open_in_main_group(self, window, file_path: str, line_num: int):
        """Open file in main editing group, not the order table's group."""
        order_group, _ = window.get_view_index(self.view)
        target_group = 0
        if order_group == 0 and window.num_groups() > 1:
            target_group = 1
        window.focus_group(target_group)
        window.open_file(f"{file_path}:{line_num}:1", sublime.ENCODED_POSITION)


class ClaudeClearEditsCommand(sublime_plugin.TextCommand):
    """Clear edit(s) or done order(s) - uses selection to determine which items to clear."""

    def run(self, edit, all_edits=False):
        import re
        from .order_table import get_table, refresh_order_table, _relative_path

        if not self.view.settings().get("order_table_view"):
            return

        window = self.view.window()
        table = get_table(window)
        if not table:
            return

        # If all_edits flag, clear everything
        if all_edits:
            table.clear_edits()
            table.clear_done()
            sublime.status_message("All edit history and done orders cleared")
            sublime.set_timeout(lambda: refresh_order_table(window), 10)
            return

        folders = window.folders() if window else []
        all_edits_list = table.list_edits()

        sel = self.view.sel()
        if not sel:
            return

        # Collect edit IDs and done order IDs from selected lines
        edits_to_clear = set()
        orders_to_delete = set()

        for region in sel:
            for line_region in self.view.lines(region):
                line = self.view.substr(line_region)

                # Check for done order line (starts with # and has [order_N])
                if line.strip().startswith('#') and '[order_' in line:
                    match = re.search(r'\[(order_\d+)\]', line)
                    if match:
                        orders_to_delete.add(match.group(1))
                    continue

                # Skip pending order lines
                if '[order_' in line:
                    continue

                # Parse edit line: file:line ... [agent_id]
                edit_match = re.match(r'\s+(.+?):(\d+)\s+', line)
                if not edit_match:
                    continue

                rel_path = edit_match.group(1).strip()
                line_num = int(edit_match.group(2))

                # Find matching edit
                for e in all_edits_list:
                    full_rel = _relative_path(e["file_path"], folders)
                    if rel_path.startswith("..."):
                        matches = full_rel.endswith(rel_path[3:])
                    else:
                        matches = full_rel == rel_path
                    if matches and e["line_num"] == line_num:
                        edits_to_clear.add(e["id"])
                        break

        if not edits_to_clear and not orders_to_delete:
            sublime.status_message("No edits or done orders in selection")
            return

        # Save cursor row for restoration after refresh
        cursor_row, _ = self.view.rowcol(sel[0].begin())

        # Clear all found edits
        for edit_id in edits_to_clear:
            table.clear_edits(edit_id=edit_id)

        # Delete all found done orders
        for order_id in orders_to_delete:
            table.delete(order_id)

        # Build status message
        parts = []
        if edits_to_clear:
            parts.append(f"{len(edits_to_clear)} edit(s)")
        if orders_to_delete:
            parts.append(f"{len(orders_to_delete)} done order(s)")
        sublime.status_message(f"Cleared {' and '.join(parts)}")

        def refresh_and_restore():
            refresh_order_table(window)
            # Restore cursor to same row (clamped to valid range)
            if self.view.is_valid():
                max_row = self.view.rowcol(self.view.size())[0]
                row = min(cursor_row, max_row)
                pt = self.view.text_point(row, 0)
                self.view.sel().clear()
                self.view.sel().add(sublime.Region(pt, pt))

        sublime.set_timeout(refresh_and_restore, 10)


class ClaudeToggleEditsGroupedCommand(sublime_plugin.TextCommand):
    """Toggle between flat and grouped-by-file edit display."""

    def run(self, edit):
        from .order_table import _views, get_table

        if not self.view.settings().get("order_table_view"):
            return

        window = self.view.window()
        table = get_table(window)
        if not table:
            return

        key = table.project_root
        if key in _views:
            grouped = _views[key].toggle_edits_grouped()
            mode = "grouped by file" if grouped else "by time"
            sublime.status_message(f"Edits: {mode}")


class ClaudeClearFileEditsCommand(sublime_plugin.WindowCommand):
    """Clear all edits for the currently focused file."""

    def run(self):
        import os
        from .order_table import get_table, refresh_order_table

        view = self.window.active_view()
        if not view or not view.file_name():
            sublime.status_message("No file focused")
            return

        # Don't operate on order table view itself
        if view.settings().get("order_table_view"):
            sublime.status_message("Focus a file view first")
            return

        file_path = view.file_name()
        table = get_table(self.window)
        if not table:
            sublime.status_message("No project folder")
            return

        # Check if there are edits for this file
        edits = [e for e in table.list_edits() if e["file_path"] == file_path]
        if not edits:
            sublime.status_message(f"No edits for {os.path.basename(file_path)}")
            return

        table.clear_edits(file_path=file_path)
        sublime.status_message(f"Cleared {len(edits)} edits for {os.path.basename(file_path)}")
        refresh_order_table(self.window)


class ClaudeFocusAgentCommand(sublime_plugin.TextCommand):
    """Focus the agent's session view that made an edit."""

    def run(self, edit):
        import re
        from .order_table import get_table, _relative_path

        if not self.view.settings().get("order_table_view"):
            return

        window = self.view.window()
        table = get_table(window)
        if not table:
            return

        folders = window.folders() if window else []

        # Get current line
        sel = self.view.sel()
        if not sel:
            return

        line_region = self.view.line(sel[0])
        line = self.view.substr(line_region)

        # Parse edit line: file:line ... [agent_id]
        edit_match = re.match(r'\s+(.+?):(\d+)\s+', line)
        if not edit_match or '[order_' in line:
            sublime.status_message("Place cursor on an edit entry")
            return

        rel_path = edit_match.group(1).strip()
        line_num = int(edit_match.group(2))

        # Find the specific edit
        target_edit = None
        for e in table.list_edits():
            full_rel = _relative_path(e["file_path"], folders)
            if rel_path.startswith("..."):
                matches = full_rel.endswith(rel_path[3:])
            else:
                matches = full_rel == rel_path
            if matches and e["line_num"] == line_num:
                target_edit = e
                break

        if not target_edit:
            sublime.status_message("Could not find edit entry")
            return

        agent_view_id = target_edit.get("agent_view_id", 0)

        # Try to focus agent session
        if agent_view_id and agent_view_id in sublime._claude_sessions:
            session = sublime._claude_sessions[agent_view_id]
            if session.output.view and session.output.view.is_valid():
                session.output.show()
                sublime.status_message(f"Focused agent {agent_view_id}")
                return

        # Agent not available - fall back to opening file at edit location
        file_path = target_edit.get("file_path")
        line_num = target_edit.get("line_num", 1)
        if file_path:
            self._open_in_main_group(window, file_path, line_num)
            sublime.status_message(f"Agent {agent_view_id} gone, opened file")
        else:
            sublime.status_message(f"Agent session not found: {agent_view_id}")

    def _open_in_main_group(self, window, file_path: str, line_num: int):
        """Open file in main editing group, not the order table's group."""
        order_group, _ = window.get_view_index(self.view)
        target_group = 0
        if order_group == 0 and window.num_groups() > 1:
            target_group = 1
        window.focus_group(target_group)
        window.open_file(f"{file_path}:{line_num}:1", sublime.ENCODED_POSITION)


class ClaudePasteImageCommand(sublime_plugin.TextCommand):
    """Paste image from clipboard into context."""

    def run(self, edit):
        import os
        from .core import get_session_for_view

        session = get_session_for_view(self.view)
        if not session:
            sublime.status_message("No active Claude session")
            return

        image_data, mime_type, file_paths_from_clip = self._get_clipboard_image()

        # File/dir paths from Finder copy — use full paths from pasteboard
        if file_paths_from_clip:
            valid_paths = [p for p in file_paths_from_clip if os.path.exists(p)]
            if valid_paths:
                # Paste paths as text into the input
                path_text = "\n".join(valid_paths)
                self.view.run_command("insert", {"characters": path_text})
                sublime.status_message(f"Pasted {len(valid_paths)} path(s)")
                return

        if image_data:
            session.add_context_image(image_data, mime_type)
            sublime.status_message(f"Image added to context ({len(image_data)} bytes)")
            return

        # No image or file paths from pasteboard, check text clipboard
        text = sublime.get_clipboard()
        if text:
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            file_paths = [line for line in lines if os.path.isfile(line)]
            if file_paths:
                for path in file_paths:
                    try:
                        with open(path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        session.add_context_file(path, content)
                    except Exception as e:
                        print(f"[Claude] Failed to add file {path}: {e}")
                sublime.status_message(f"Added {len(file_paths)} file(s) to context")
                return

            print(f"[Claude] paste: trying context paste...")
            if self._try_paste_as_context(session, text):
                print(f"[Claude] paste: added as context")
                return
            print(f"[Claude] paste: plain text insert")
            self.view.run_command("insert", {"characters": text})

    def _try_paste_as_context(self, session, text):
        import os
        from .listeners import _last_copy_meta
        if not _last_copy_meta:
            return False
        if _last_copy_meta["text"] != text:
            return False
        path = _last_copy_meta["file"]
        regions = _last_copy_meta["regions"]
        region_parts = []
        for start, end in regions:
            if start == end:
                region_parts.append(f"L{start}")
            else:
                region_parts.append(f"L{start}-L{end}")
        region_str = ",".join(region_parts)
        label = f"{path}:{region_str}"
        session.add_context_selection(label, text)
        sublime.status_message(f"Pasted as context: {os.path.basename(path)}:{region_str}")
        return True

    def _get_clipboard_image(self):
        """Check if clipboard contains image data using platform-specific helper."""
        import os
        import platform
        import subprocess
        import base64

        try:
            helpers_dir = os.path.join(os.path.dirname(__file__), "helpers")
            system = platform.system()

            if system == "Darwin":
                cmd = ["osascript", "-l", "JavaScript", os.path.join(helpers_dir, "clipboard_image.js")]
            elif system == "Linux":
                cmd = ["bash", os.path.join(helpers_dir, "clipboard_image_linux.sh")]
            elif system == "Windows":
                cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", os.path.join(helpers_dir, "clipboard_image_windows.ps1")]
            else:
                return None, None

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            output = result.stdout.strip()

            if output.startswith("file_paths"):
                paths = output.split("\n")[1:]
                paths = [p.strip() for p in paths if p.strip()]
                return None, None, paths

            if output.startswith("image/"):
                lines = output.split("\n")
                mime_type = lines[0]
                b64_data = lines[1] if len(lines) > 1 else ""
                if b64_data:
                    return base64.b64decode(b64_data), mime_type, None

            return None, None, None
        except Exception as e:
            print(f"[Claude] Clipboard error: {e}")
            import traceback
            traceback.print_exc()
            return None, None, None


class ClaudeOpenLinkCommand(sublime_plugin.TextCommand):
    """Open file path or URL under cursor with Cmd+click."""

    def run(self, edit, event=None):
        import os
        import re
        import webbrowser

        # Get click position from event or use cursor
        if event:
            pt = self.view.window_to_text((event["x"], event["y"]))
        else:
            sel = self.view.sel()
            if not sel:
                return
            pt = sel[0].begin()

        # Get the line at cursor
        line_region = self.view.line(pt)
        line = self.view.substr(line_region)
        col = pt - line_region.begin()

        # Try to find URL at position
        url_pattern = r'https?://[^\s\]\)>\'"]+|file://[^\s\]\)>\'"]+'
        for match in re.finditer(url_pattern, line):
            if match.start() <= col <= match.end():
                url = match.group()
                webbrowser.open(url)
                return

        # Try to find file path at position (absolute or relative with common extensions)
        # Match paths like /foo/bar.py, ./foo/bar.nim, src/file.ts:123
        path_pattern = r'(?:[/.]|[a-zA-Z]:)[^\s:,\]\)\}>\'\"]+(?::\d+)?'
        for match in re.finditer(path_pattern, line):
            if match.start() <= col <= match.end():
                path_with_line = match.group()
                # Extract line number if present (path:123)
                line_num = None
                if ':' in path_with_line:
                    parts = path_with_line.rsplit(':', 1)
                    if parts[1].isdigit():
                        path_with_line = parts[0]
                        line_num = int(parts[1])

                # Check if file exists
                if os.path.isfile(path_with_line):
                    window = self.view.window()
                    if window:
                        if line_num:
                            window.open_file(f"{path_with_line}:{line_num}", sublime.ENCODED_POSITION)
                        else:
                            window.open_file(path_with_line)
                    return

        sublime.status_message("No link or file path found at cursor")

    def want_event(self):
        return True


class ClaudeRetainCommand(sublime_plugin.WindowCommand):
    """Manage session retain content for compaction."""

    def run(self, action="view"):
        from .core import get_active_session

        session = get_active_session(self.window)
        if not session:
            sublime.status_message("No active session")
            return

        if action == "view":
            content = session.retain()
            if content:
                # Show in output panel
                panel = self.window.create_output_panel("claude_retain")
                panel.run_command("append", {"characters": f"# Session Retain Content\n\n{content}"})
                self.window.run_command("show_panel", {"panel": "output.claude_retain"})
            else:
                sublime.status_message("Retain file is empty")

        elif action == "edit":
            path = session._get_retain_path()
            if path:
                import os
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if not os.path.exists(path):
                    with open(path, "w") as f:
                        f.write("")
                self.window.open_file(path)
            else:
                sublime.status_message("Session not initialized yet")

        elif action == "clear":
            session.clear_retain()
            sublime.status_message("Retain content cleared")


class ClaudeProjectRetainCommand(sublime_plugin.WindowCommand):
    """Edit project retain file (.claude/RETAIN.md) for compaction."""

    def run(self):
        import os

        folders = self.window.folders()
        if not folders:
            sublime.status_message("No project folder open")
            return

        cwd = folders[0]
        retain_path = os.path.join(cwd, ".claude", "RETAIN.md")

        # Create .claude dir and file if needed
        os.makedirs(os.path.dirname(retain_path), exist_ok=True)
        if not os.path.exists(retain_path):
            with open(retain_path, "w") as f:
                f.write("")

        self.window.open_file(retain_path)


class ClaudeCodeViewPlanCommand(sublime_plugin.WindowCommand):
    """Open current or most recent plan file."""

    def run(self):
        import os
        import glob

        session = get_active_session(self.window)

        # Try session's plan file first
        if session and session.plan_file and os.path.exists(session.plan_file):
            self.window.open_file(session.plan_file)
            return

        # Otherwise find most recent plan file
        plans_dir = os.path.expanduser("~/.claude/plans")
        if not os.path.exists(plans_dir):
            sublime.status_message("No plan files found")
            return

        plan_files = glob.glob(os.path.join(plans_dir, "*.md"))
        if not plan_files:
            sublime.status_message("No plan files found")
            return

        # Open most recent
        most_recent = max(plan_files, key=os.path.getmtime)
        self.window.open_file(most_recent)

    def is_enabled(self):
        return True
