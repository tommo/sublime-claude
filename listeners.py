"""Claude Code event listeners for Sublime Text."""
import sublime
import sublime_plugin

from .core import get_session_for_view, get_active_session, _sessions
from .session import Session


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
                if char_before == "@":
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

        # Build menu items: browse, clear, then open files
        items = []  # (action, data, [title, description])

        # Browse option
        items.append(("browse", None, ["Browse...", "Choose file from project"]))

        # Clear context (only show if there's pending context)
        if session.pending_context:
            count = len(session.pending_context)
            items.append(("clear", None, ["Clear context", f"{count} pending item{'s' if count > 1 else ''}"]))

        # Open files in this window
        for v in window.views():
            if v.file_name() and not v.settings().get("claude_output"):
                import os
                name = os.path.basename(v.file_name())
                path = v.file_name()
                items.append(("file", v, [name, path]))

        def on_select(idx):
            if idx >= 0:
                action, data, _ = items[idx]
                if action == "browse":
                    self._show_file_picker(session)
                elif action == "clear":
                    session.clear_context()
                    sublime.status_message("Context cleared")
                elif action == "file" and data:
                    content = data.substr(sublime.Region(0, data.size()))
                    session.add_context_file(data.file_name(), content)

        window.show_quick_panel(
            [item[2] for item in items],
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
