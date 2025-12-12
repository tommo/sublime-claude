"""Claude Code event listeners for Sublime Text."""
import sublime
import sublime_plugin

from .core import get_session_for_view, get_active_session
from .session import Session


class ClaudeCodeEventListener(sublime_plugin.EventListener):
    def on_window_command(self, window: sublime.Window, command: str, args: dict) -> None:
        if command == "close_window":
            # Stop all sessions in this window
            to_remove = []
            for view_id, session in sublime._claude_sessions.items():
                if session.window == window:
                    session.stop()
                    to_remove.append(view_id)
            for view_id in to_remove:
                del sublime._claude_sessions[view_id]

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
        session = sublime._claude_sessions.get(session_view_id)
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
        if view.id() in sublime._claude_sessions:
            sublime._claude_sessions[view.id()].stop()
            del sublime._claude_sessions[view.id()]


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

        # Check if this is an orphaned view that needs reconnection
        s = get_session_for_view(self.view)
        if not s:
            self._reconnect_orphaned_view(window)
            s = get_session_for_view(self.view)

        # Update this session's status and title
        if s:
            s._update_status_bar()
            s.output.set_name(s.name or "Claude")

            # Auto-enter input mode if session is idle and not already in input mode
            if s.initialized and not s.working and not s.output.is_input_mode():
                s._enter_input_with_draft()
            # If already in input mode, ensure cursor is positioned and view is responsive
            elif s.output.is_input_mode():
                # Make sure there's a valid cursor position so clicking works
                # This fixes mouse selection which requires a valid initial cursor state
                input_start = s.output._input_start
                sel = self.view.sel()
                if len(sel) == 0:
                    # No selection at all - set cursor to input start
                    self.view.sel().clear()
                    self.view.sel().add(sublime.Region(input_start, input_start))

        # Remove active marker from previous active session
        if old_active and old_active != self.view.id() and old_active in sublime._claude_sessions:
            old_session = sublime._claude_sessions[old_active]
            old_session.output.set_name(old_session.name or "Claude")

    def _reconnect_orphaned_view(self, window):
        """Reconnect an orphaned Claude output view on focus."""
        from .session import Session, load_saved_sessions

        view = self.view

        # Guard against double reconnection
        if view.id() in sublime._claude_sessions:
            return
        if view.settings().get("claude_reconnecting"):
            return
        view.settings().set("claude_reconnecting", True)

        name = view.name()
        session_name = None

        # Strip status prefix if present
        if name.startswith(("◉ ", "◇ ", "• ")):
            name = name[2:]
        if name.startswith("Claude: "):
            session_name = name[8:]
            if " - " in session_name:
                session_name = session_name.split(" - ")[0]

        # Handle spinner prefix
        for c in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏":
            if name.startswith(c + " "):
                name = name[2:]
                if " - " in name:
                    session_name = name.split(" - ")[0]
                else:
                    session_name = name
                break

        # Try to find session_id from saved sessions (only if it was actually used)
        resume_id = None
        if session_name:
            for saved in load_saved_sessions():
                if saved.get("name") == session_name and saved.get("query_count", 0) > 0:
                    resume_id = saved.get("session_id")
                    break

        # Create session - with resume_id if found, fresh otherwise
        session = Session(window, resume_id=resume_id)
        session.name = session_name
        session.output.view = view
        session.draft_prompt = ""
        sublime._claude_sessions[view.id()] = session

        # Reset active states
        session.output.reset_active_states()
        if session_name:
            view.set_name(f"Claude: {session_name}")
        else:
            view.set_name("Claude")

        session.start()

        # Clear reconnecting flag
        view.settings().erase("claude_reconnecting")

        if session_name:
            view.set_status("claude_reconnect", f"Reconnected: {session_name}")
            sublime.set_timeout(lambda v=view: v.erase_status("claude_reconnect"), 3000)

    def on_text_command(self, command_name, args):
        """Intercept text commands to restrict edits in input mode."""
        s = get_session_for_view(self.view)
        if not s or not s.output.is_input_mode():
            return None

        # Commands that are always safe (read-only, navigation, selection)
        safe_commands = {
            "copy", "select_all", "find_all_under",
            "drag_select", "context_menu",
            "move", "move_to", "scroll_lines",
            "claude_submit_input", "claude_code_interrupt"
        }

        if command_name in safe_commands:
            return None

        # All other commands are potentially destructive - check if cursor is in input region
        input_start = s.output._input_start
        sel = self.view.sel()

        # Check ALL regions in the selection
        for region in sel:
            # Block if either start or end of selection is outside input region
            if region.begin() < input_start or region.end() < input_start:
                print(f"[Claude] BLOCKING {command_name} at position {region.begin()}, input_start={input_start}")
                return ("noop", {})

        return None

    def on_selection_modified(self):
        """Dynamically toggle read_only based on cursor position to protect conversation history."""
        s = get_session_for_view(self.view)
        if not s:
            return

        # Only manage read_only state when in input mode
        if not s.output.is_input_mode():
            return

        # CRITICAL BUG FIX: Sublime Text requires at least one region in sel()
        # for mouse clicks to work. If sel is completely empty, restore a cursor.
        sel = self.view.sel()
        if len(sel) == 0:
            cursor_pos = self.view.size() if self.view.size() > 0 else 0
            self.view.sel().add(sublime.Region(cursor_pos, cursor_pos))
            return

        # Check if ALL cursors/selections are in the input region
        input_start = s.output._input_start
        all_in_input_region = True

        for region in sel:
            # If any part of any selection is before input_start, not safe to edit
            if region.begin() < input_start:
                all_in_input_region = False
                break

        # Toggle read_only based on whether we're in the input region
        # This prevents typing/pasting/any modification outside input area
        if all_in_input_region:
            # Safe to edit - in input region
            if self.view.is_read_only():
                self.view.set_read_only(False)
        else:
            # Not safe - in conversation history
            if not self.view.is_read_only():
                self.view.set_read_only(True)

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
