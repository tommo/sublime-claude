"""Claude Code event listeners for Sublime Text."""
import os
import platform
import re
import urllib.parse

import sublime
import sublime_plugin

from .core import get_session_for_view, get_active_session, in_startup_quiet
from .session import Session
from .context_parser import ContextParser, ContextMenuItem, ContextMenuHandler


def settle_active_claude_view(window: sublime.Window) -> None:
    """Full reconnect + title for the window's active Claude sheet only."""
    if not window:
        return
    view = window.active_view()
    if not view or not view.settings().get("claude_output"):
        return
    # Force a normal on_activated-style settle (quiet period already ended).
    s = get_session_for_view(view)
    if not s:
        # Build a listener instance is awkward; call reconnect helper directly.
        ClaudeOutputEventListener(view)._reconnect_orphaned_view(window, quiet=False)
        s = get_session_for_view(view)
    if s:
        s._update_status_bar()
        s.output.set_name(s.display_name)
        if s.is_sleeping:
            s._apply_sleep_ui()
        elif s.initialized and not s.working and not s.output.is_input_mode():
            s._enter_input_with_draft()


def settle_startup_claude_views() -> None:
    """After quiet period: show sleep state on every restored Claude sheet.

    Quiet reconnect registers sessions without title/phantom churn so multi-tab
    restore does not raise each sheet. Once quiet ends, apply ⏸ titles and
    sleep overlays on all sleeping sessions — still without focusing them.
    Only the truly active sheet gets full live UX (input mode / start).
    """
    for w in sublime.windows():
        # Register any orphans that never received on_activated during restore.
        for view in w.views():
            if not view.settings().get("claude_output"):
                continue
            if get_session_for_view(view):
                continue
            if w.settings().get("claude_creating_session"):
                continue
            ClaudeOutputEventListener(view)._reconnect_orphaned_view(w, quiet=True)

        for s in list(sublime._claude_sessions.values()):
            if s.window != w:
                continue
            if not s.output or not s.output.view or not s.output.view.is_valid():
                continue
            if s.is_sleeping:
                # Title + overlay only; _show_overlay_phantom already skips
                # view.show() when the sheet is not focused.
                s._apply_sleep_ui()
            else:
                # Keep tab title in sync (◇ etc.) without focusing.
                s.output.set_name(s.display_name)

        settle_active_claude_view(w)


_last_copy_meta = None  # {file, regions: [(row_start, row_end), ...], text}


def _attach_order_bookmarks(view: sublime.View) -> None:
    """Add order-region/phantom markers for any pending orders matching this view's file.

    Idempotent (each call erases-then-adds), but guarded by a per-view sentinel so
    we only walk the order table once per view per plugin lifetime."""
    if view.settings().get("claude_orders_attached"):
        return
    path = view.file_name()
    if not path:
        return
    window = view.window()
    if not window:
        return
    from .order_table import get_table, _add_order_region
    table = get_table(window)
    if not table:
        return
    for order in table.list("pending"):
        if order.get("file_path") != path:
            continue
        _add_order_region(
            view, order["id"],
            order.get("row", 0), order.get("col", 0),
            order.get("selection_length"), order.get("prompt"),
        )
    view.settings().set("claude_orders_attached", True)

class ClaudeCodeEventListener(sublime_plugin.EventListener):
    def on_post_text_command(self, view, command_name, args):
        global _last_copy_meta
        if command_name not in ("copy", "cut"):
            return
        print(f"[Claude] on_post_text_command: {command_name}")
        path = view.file_name()
        if not path or view.is_scratch() or view.settings().get("claude_output"):
            return
        sel = view.sel()
        if not sel:
            print("[Claude] copy tracking skipped: no sel")
            return
        regions = []
        for r in sel:
            row_start = view.rowcol(r.begin())[0] + 1
            row_end = view.rowcol(r.end())[0] + 1
            regions.append((row_start, row_end))
        _last_copy_meta = {
            "file": path,
            "regions": regions,
            "text": sublime.get_clipboard(),
        }
        print(f"[Claude] copy tracked: {path} regions={regions}")

    def on_window_command(self, window: sublime.Window, command: str, args: dict):
        if command == "close_window":
            # Stop all sessions in this window
            to_remove = []
            for view_id, session in sublime._claude_sessions.items():
                if session.window == window:
                    session.stop()
                    to_remove.append(view_id)
            for view_id in to_remove:
                del sublime._claude_sessions[view_id]

        # Intercept close for claude output views
        if command in ("close", "close_file", "close_by_index"):
            view = window.active_view()
            if command == "close_by_index" and args:
                group = args.get("group", 0)
                index = args.get("index", 0)
                views = window.views_in_group(group)
                if index < len(views):
                    view = views[index]
            if view and view.settings().get("claude_output"):
                # Quick Agent soft-hide closes the sheet without killing the bridge
                if view.settings().get("claude_quick_soft_close"):
                    return None
                session = sublime._claude_sessions.get(view.id())
                if session and (session.initialized or session.is_sleeping):
                    def _ask():
                        s = sublime._claude_sessions.get(view.id())
                        if not s or not (s.initialized or s.is_sleeping):
                            view.close()
                            return
                        if sublime.ok_cancel_dialog("Close this Claude session?", "Close"):
                            s.stop()
                            if view.id() in sublime._claude_sessions:
                                del sublime._claude_sessions[view.id()]
                            view.close()
                    sublime.set_timeout(_ask, 0)
                    return ("noop",)


    def on_activated(self, view: sublime.View) -> None:
        """Handle view activated - check if it's for context adding from goto."""
        import time
        window = view.window()
        if not window:
            return

        # Skip Claude output views
        if view.settings().get("claude_output"):
            return

        # Lazily attach order bookmarks (only walks table once per view, no-op if already attached).
        _attach_order_bookmarks(view)

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
                # set_pending_context already restored input text if input mode was active
                if session.output.is_input_mode():
                    return
                session.output.enter_input_mode()
                if session.draft_prompt:
                    session.output.view.run_command("append", {"characters": session.draft_prompt})
                    end = session.output.view.size()
                    session.output.view.sel().clear()
                    session.output.view.sel().add(sublime.Region(end, end))

            sublime.set_timeout(refocus, 100)

    def on_load(self, view: sublime.View) -> None:
        # Lazily attach order bookmarks for this file (replaces upfront sync_orders scan).
        _attach_order_bookmarks(view)

    def on_post_save(self, view: sublime.View) -> None:
        fname = view.file_name() or ""
        if os.path.basename(fname) == "ClaudeOutput.sublime-settings":
            for session in getattr(sublime, "_claude_sessions", {}).values():
                if session.output:
                    session.output._apply_output_settings()

    def on_pre_close(self, view: sublime.View) -> None:
        # Closing a workflow detail view → drop the session's refs and refocus
        # the parent session view it was opened from.
        task_id = view.settings().get("claude_workflow_view")
        if not task_id:
            return
        parent_id = view.settings().get("claude_workflow_parent")
        win = view.window()
        sess = sublime._claude_sessions.get(parent_id)
        if sess:
            if hasattr(sess, "_workflow_views"):
                sess._workflow_views.pop(task_id, None)
            if hasattr(sess, "_workflow_view_ps"):
                sess._workflow_view_ps.pop(task_id, None)
        if parent_id is not None and win:
            def refocus():
                pv = next((v for v in win.views() if v.id() == parent_id), None)
                if pv:
                    win.focus_view(pv)
            sublime.set_timeout(refocus, 0)

    def on_close(self, view: sublime.View) -> None:
        sublime.load_settings("ClaudeOutput.sublime-settings").clear_on_change(
            f"claude_output_{view.id()}"
        )
        # Soft-hide Quick Agent: sheet goes away, bridge stays in quick_agent registry
        if view.settings().get("claude_quick_soft_close"):
            sublime._claude_sessions.pop(view.id(), None)
            return
        # Clean up session when output view is closed
        if view.id() in sublime._claude_sessions:
            sublime._claude_sessions[view.id()].stop()
            del sublime._claude_sessions[view.id()]

        # Check if closed view was a terminal mode view
        tag = view.settings().get("terminus_view.tag") or ""
        if tag.startswith("claude-terminal-"):
            for vid, session in sublime._claude_sessions.items():
                if session.terminal_mode and session._terminal_tag == tag:
                    session._on_terminal_exit()
                    break


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

        # ST package load / session restore can fire on_activated for every
        # matching ViewEventListener sheet — even when it is not the real
        # active_view. Heavy reconnect UI (set_name / phantoms / show) then
        # makes each tab "raise" in sequence. Stay quiet unless we truly own focus.
        active = window.active_view()
        is_real_active = bool(active and active.id() == self.view.id())
        quiet = in_startup_quiet() or not is_real_active

        # Mark this as the "active" session for the window only when truly focused
        old_active = window.settings().get("claude_active_view")
        if is_real_active:
            window.settings().set("claude_active_view", self.view.id())

        # Check if this is an orphaned view that needs reconnection
        s = get_session_for_view(self.view)
        if not s:
            # create_session is mid-flight (new_file → on_activated); it will
            # register the real Session — do not invent a sleeping orphan.
            if window.settings().get("claude_creating_session"):
                return
            self._reconnect_orphaned_view(window, quiet=quiet)
            s = get_session_for_view(self.view)

        # Live / intentional sessions that truly have focus still get full UX
        # even during the startup quiet window (New Session mid-reload).
        if quiet and is_real_active and s and not s.is_sleeping:
            quiet = False

        if quiet:
            # No phantom / input / focus. Sleeping tabs still get ⏸ titles.
            if s and s.is_sleeping:
                s.output.set_name(s.display_name)
            return

        # Update this session's status and title
        if s:
            s._update_status_bar()
            s.output.set_name(s.display_name)

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
            old_session.output.set_name(old_session.display_name)

    def _reconnect_orphaned_view(self, window, quiet: bool = False):
        """Reconnect an orphaned Claude output view on focus.

        quiet=True: register session only (no set_name / phantoms / show / start).
        Used during ST startup so restoring many claude_output sheets does not
        raise each tab.
        """
        # Quick Agent sheets are owned by quick_agent.py — never invent a resume orphan.
        if self.view and self.view.settings().get("claude_quick"):
            return
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

        # Strip trailing ellipsis from truncation (captured before prefix-stripping
        # so prefix-match logic below still knows the name was truncated).
        name_was_truncated = name.endswith("…")
        if name_was_truncated:
            name = name[:-1]

        # Peel status icons + any stacked backend `ABBR> ` prefixes (covers
        # custom providers too) so reconnect can't accumulate them.
        from .output import strip_title_decoration
        name = strip_title_decoration(name)

        # Extract session name (before " - " suffix if present)
        if " - " in name:
            session_name = name.split(" - ")[0]
        elif name and name != "Claude":
            session_name = name

        # Try to find session_id from saved sessions
        resume_id = None
        saved_sessions = load_saved_sessions()

        # Method 1: Match by name (exact or prefix if name was truncated)
        if session_name:
            for saved in saved_sessions:
                saved_name = saved.get("name") or ""
                if not saved.get("session_id"):
                    continue
                if saved_name == session_name or saved_name.startswith(session_name):
                    resume_id = saved.get("session_id")
                    session_name = saved_name
                    break

        # Method 2: Match by first prompt in view content
        if not resume_id:
            content = view.substr(sublime.Region(0, min(500, view.size())))
            m = re.search(r'◎ (.+?) ▶', content)
            if m:
                first_prompt = m.group(1).strip()
                for saved in saved_sessions:
                    fp = saved.get("first_prompt", "")
                    if fp and fp == first_prompt and saved.get("query_count", 0) > 0:
                        resume_id = saved.get("session_id")
                        session_name = saved.get("name") or session_name
                        break

        # Check for pending rewind point
        resume_session_at = None
        if resume_id:
            for saved in saved_sessions:
                if saved.get("session_id") == resume_id:
                    resume_session_at = saved.get("resume_session_at")
                    break

        print(f"[Claude] reconnect: view={view.name()!r}, session={session_name!r}, "
              f"resume_id={resume_id}, quiet={quiet}")

        # Create session in sleeping state — user wakes with Enter or Wake command
        saved_backend = view.settings().get("claude_backend", "claude")
        session = Session(window, resume_id=resume_id, backend=saved_backend)
        session.name = session_name
        session.output.view = view
        session.draft_prompt = ""
        if resume_session_at:
            session._pending_resume_at = resume_session_at
        sublime._claude_sessions[view.id()] = session

        # Reset active states
        session.output.reset_active_states()

        # Quiet path: registry + sleep title only — no phantom / view.show /
        # start (those can jostle multi-tab restore). Tab ⏸ is safe and is how
        # the user sees sleeping state without focusing every sheet.
        # is_sleeping is derived (session_id + no client + not initialized).
        if quiet:
            if resume_id and not session.session_id:
                session.session_id = resume_id
            if resume_id:
                view.settings().set("claude_sleeping", True)
                session.output.set_name(session.display_name)
            view.settings().erase("claude_reconnecting")
            return

        # Re-apply sheet theme (Quick Agent wins over backend tint)
        if view.settings().get("claude_quick"):
            view.settings().set(
                "color_scheme",
                "Packages/ClaudeCode/ClaudeOutput-quick.hidden-tmTheme",
            )
        else:
            backend = view.settings().get("claude_backend")
            if backend:
                backend_themes = {
                    "codex": "Packages/ClaudeCode/ClaudeOutput-codex.hidden-tmTheme",
                    "copilot": "Packages/ClaudeCode/ClaudeOutput-copilot.hidden-tmTheme",
                    "pi": "Packages/ClaudeCode/ClaudeOutput.hidden-tmTheme",
                }
                theme = backend_themes.get(backend)
                if theme:
                    view.settings().set("color_scheme", theme)

        # If we have a session_id, sleep it. Otherwise auto-reconnect (old behavior).
        if resume_id:
            session._apply_sleep_ui()
        else:
            session.start(resume_session_at=resume_session_at)

        # Clear reconnecting flag
        view.settings().erase("claude_reconnecting")

    # Buffer-mutating commands that must stay inside the ◎ input region.
    # Everything else (Cmd+D find_under_expand, multi-cursor, find, nav) is
    # allowed over history — the view is read-mostly; we only block edits.
    _INPUT_EDIT_COMMANDS = frozenset({
        "insert", "insert_snippet", "insert_best_completion",
        "left_delete", "right_delete", "delete_word", "delete_to_mark",
        "cut", "paste", "paste_and_indent", "paste_from_history",
        "swap_line_up", "swap_line_down", "join_lines", "duplicate_line",
        "permute_lines", "permute_selection", "sort_lines", "wrap_lines",
        "indent", "unindent", "reindent", "complete_under",
        "commit_completion", "auto_complete", "replace_completion_with_next_completion",
        "yank", "run_macro_file", "run_macro",
        "upper_case", "lower_case", "title_case", "swap_case",
    })

    def on_text_command(self, command_name, args):
        """Intercept text commands to restrict edits in input mode."""
        s = get_session_for_view(self.view)
        if not s:
            return None

        # Paste always routes through claude_paste_image — even when not yet
        # in input mode (otherwise ST paste hits a read-only history region
        # and image clipboards are dropped).
        if command_name in ("paste", "paste_and_indent"):
            return ("claude_paste_image", {})

        if not s.output.is_input_mode():
            return None

        # Plugin / navigation / selection / find — never block (Cmd+D etc.)
        if command_name.startswith("claude_"):
            return None
        if command_name not in self._INPUT_EDIT_COMMANDS:
            # Allow find_under_expand, drag_select, move, copy, select_*, …
            return None

        # Edit commands: keep them inside the input region only
        input_start = s.output._input_start
        sel = self.view.sel()

        for region in sel:
            if region.begin() < input_start or region.end() < input_start:
                # Typing outside history → jump to input and allow insert
                if command_name == "insert":
                    input_end = self.view.size()
                    self.view.sel().clear()
                    self.view.sel().add(sublime.Region(input_end, input_end))
                    self.view.show(input_end)
                    return None

                # Block destructive edits over conversation history
                print(
                    f"[Claude] BLOCKING {command_name} at {region.begin()}, "
                    f"input_start={input_start}")
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

        # Keep view editable - we handle protection via on_modified (Terminus approach)
        # This allows typing anywhere, then we redirect it to input area
        if self.view.is_read_only():
            self.view.set_read_only(False)

    def on_query_context(self, key, operator, operand, match_all):
        """Provide context for key bindings."""
        if key == "claude_outside_input_area":
            s = get_session_for_view(self.view)
            if not s or not s.output.is_input_mode():
                return False

            input_start = s.output._input_start
            sel = self.view.sel()

            # Check if cursor is outside input area
            for region in sel:
                if region.begin() < input_start:
                    return True
            return False

        if key == "claude_submit_with_modifier":
            val = bool(sublime.load_settings("ClaudeCode.sublime-settings")
                       .get("submit_with_modifier", False))
            if operator == sublime.OP_EQUAL:
                return val == bool(operand)
            if operator == sublime.OP_NOT_EQUAL:
                return val != bool(operand)
            return None

        return None

    _in_soft_undo = False

    def on_modified(self):
        """Track modifications and redirect typing from history to input area."""
        if self._in_soft_undo:
            return

        s = get_session_for_view(self.view)
        if not s:
            return

        # Check what command just ran (Terminus trick)
        command, args, _ = self.view.command_history(0)

        # Don't redirect during input mode setup
        if not s.output.is_input_mode():
            return

        # Handle insert command - check if typing happened outside input area
        if command == "insert" and "characters" in args and len(self.view.sel()) == 1:
            input_start = s.output._input_start
            current_cursor = self.view.sel()[0].end()

            # If the insert happened before input area, redirect it
            chars = args["characters"]
            insert_pos = max(current_cursor - len(chars), 0)

            if insert_pos < input_start:
                # Undo the insert (guard against recursion)
                self._in_soft_undo = True
                try:
                    self.view.run_command("soft_undo")
                finally:
                    self._in_soft_undo = False

                # Move cursor to end of input area
                input_end = self.view.size()
                self.view.sel().clear()
                self.view.sel().add(sublime.Region(input_end, input_end))

                # Re-insert at correct position
                self.view.run_command("insert", {"characters": chars})

                # Show cursor
                self.view.show(self.view.size())
                return

        # Soft-undo buffer edits that landed in history (not selection/find).
        # find_under_expand / multi-cursor must not be reverted here.
        elif command and not command.startswith("claude"):
            if command not in self._INPUT_EDIT_COMMANDS:
                # Non-edit (selection, nav, find) — leave alone
                pass
            else:
                input_start = s.output._input_start
                if len(self.view.sel()) > 0:
                    for region in self.view.sel():
                        if region.begin() < input_start:
                            self.view.run_command("soft_undo")
                            return

        # Don't capture an AskUserQuestion free-text answer as the prompt draft
        # (question input reuses _input_mode) — it would reappear next prompt.
        if getattr(s.output, "_question_input_mode", False):
            return

        # Save draft
        input_text = s.output.get_input_text()
        s.draft_prompt = input_text

        # Check for @ trigger at cursor
        sel = self.view.sel()
        if sel and len(sel) == 1:
            cursor = sel[0].end()
            content = self.view.substr(sublime.Region(0, self.view.size()))
            trigger = ContextParser.check_trigger(content, cursor)
            if trigger:
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

        # Build list of open files
        import os
        open_files = []
        for v in window.views():
            if v.file_name() and not v.settings().get("claude_output"):
                name = os.path.basename(v.file_name())
                path = v.file_name()
                open_files.append((name, path))

        # Use context parser to build menu
        has_pending = bool(session.pending_context)
        pending_count = len(session.pending_context) if has_pending else 0
        menu_items = ContextParser.build_menu(open_files, has_pending, pending_count)

        # Create handler for menu selection
        def on_browse():
            self._show_file_picker(session)

        def on_clear():
            session.clear_context()
            sublime.status_message("Context cleared")

        def on_add_file(path, _content):
            # Find the view for this path and read content
            for v in window.views():
                if v.file_name() == path:
                    content = v.substr(sublime.Region(0, v.size()))
                    session.add_context_file(path, content)
                    break

        handler = ContextMenuHandler(on_browse, on_clear, on_add_file)

        def on_select(idx):
            handler.handle_selection(menu_items, idx)

        window.show_quick_panel(
            ContextParser.format_menu_items(menu_items),
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
                    from .context_manager import format_line_range
                    r0 = active_view.rowcol(sel[0].begin())[0] + 1
                    r1 = active_view.rowcol(sel[0].end())[0] + 1
                    label = f"{path}:{format_line_range(r0, r1)}"
                    session.add_context_selection(label, content)
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
