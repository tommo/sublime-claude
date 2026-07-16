"""Claude Code commands for Sublime Text."""
import os
import sublime
import sublime_plugin
import platform

from ..core import get_active_session, get_session_for_view, create_session
from ..session import Session, load_saved_sessions, load_bookmarks, toggle_bookmark
from ..prompt_builder import PromptBuilder
from ..command_parser import CommandParser
from .. import backends

# Fallback model lists per backend (used when no cache/settings available).
# Snapshot of built-ins at import time; custom providers are looked up live via
# backends.get(backend).default_models in ClaudeSelectModelCommand._get_models.
DEFAULT_MODELS = backends.default_models_dict()


class ClaudeToggleSubmitModeCommand(sublime_plugin.ApplicationCommand):
    """Toggle whether Enter or Cmd/Ctrl+Enter submits the input."""
    def run(self):
        s = sublime.load_settings("ClaudeCode.sublime-settings")
        cur = bool(s.get("submit_with_modifier", False))
        s.set("submit_with_modifier", not cur)
        sublime.save_settings("ClaudeCode.sublime-settings")
        mode = "Cmd/Ctrl+Enter" if not cur else "Enter"
        sublime.status_message(f"Claude: submit with {mode}")

    def is_checked(self):
        return bool(sublime.load_settings("ClaudeCode.sublime-settings")
                    .get("submit_with_modifier", False))


# --- Input Mode Commands ---

class ClaudeSubmitInputCommand(sublime_plugin.TextCommand):
    """Handle Enter key in input mode - submit the prompt."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if not s:
            return

        # Terminal mode: focus the terminal view instead of waking
        if s.terminal_mode:
            tv = s._find_terminal_view()
            if tv:
                self.view.window().focus_view(tv)
            return

        # Wake sleeping session on Enter
        if s.is_sleeping:
            s.wake()
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

        # Manual submit = user takeover; drop the loop indicator. If the agent
        # re-arms a wake this turn, _on_msg_tool_use flips it back on.
        s.is_looping = False
        s.next_wake_at = None

        # Check for slash commands
        cmd = CommandParser.parse(text)
        if cmd:
            s.output.exit_input_mode(keep_text=False)
            s.draft_prompt = ""
            self._handle_command(s, cmd)
            return

        # If working: queue only — leave ◎ open and clear just the submitted
        # text so the user can keep typing the next message without a full exit.
        if s.working:
            s.queue_prompt(text)
            # Clear only the submitted line; do not exit sticky (preserves peel
            # + draft path). Next keystrokes are a new message.
            try:
                if s.output.is_input_mode() and s.output.view:
                    # Clear draft but keep spare bottom blank line under ◎
                    s.output.set_composer_text("")
                    s.draft_prompt = ""
                    # Queue phantom can change layout height — refocus + show ◎
                    try:
                        s._update_queue_phantom()
                    except Exception:
                        pass
                    s.output.focus_composer(force_show=True)
                    # Second pass after phantoms/layout settle
                    def _refocus_after_queue():
                        if not s.output or not s.output.is_input_mode():
                            return
                        s.output.focus_composer(force_show=True)
                    sublime.set_timeout(_refocus_after_queue, 30)
                    sublime.set_timeout(_refocus_after_queue, 120)
                    return
                s.draft_prompt = ""
            except Exception as e:
                print(f"[Claude] queue clear input: {e}")
                s.output.exit_input_mode(keep_text=False)
                s.draft_prompt = ""
                s._input_mode_entered = False

                def _rearm_after_queue():
                    if s.output and not s.output.is_input_mode():
                        s._enter_input_with_draft()
                    if s.output and s.output.is_input_mode():
                        s.output.focus_composer(force_show=True)
                sublime.set_timeout(_rearm_after_queue, 30)
            try:
                s._update_queue_phantom()
            except Exception:
                pass
            return

        s.output.exit_input_mode(keep_text=False)
        s.draft_prompt = ""
        # Allow sticky composer to re-open immediately after submit
        s._input_mode_entered = False

        # Quick Agent: message submit *is* start — dead/idle bridge → new session
        if getattr(s, "quick_mode", False):
            try:
                from .. import quick_agent as qa
                host = qa.get_host(s.window) if s.window else None
                if host and host.submit_prompt(s, text):
                    def _rearm_q():
                        if not s.output or s.output.is_input_mode():
                            return
                        # Session object may have been swapped; use host active
                        cur = host.active_session or s
                        if getattr(cur, "_quick_pending_prompt", None):
                            return  # wait for init→query
                        cur._input_mode_entered = False
                        cur._enter_input_with_draft()
                    sublime.set_timeout(_rearm_q, 30)
                    return
            except Exception as e:
                print(f"[Claude] quick submit: {e}")

        s.query(text)

        # Sticky EOF composer: re-arm ◎ so the next message can be typed
        # (and queued) while this turn streams.
        def _rearm():
            if not s.output or s.output.is_input_mode():
                return
            s._input_mode_entered = False
            s._enter_input_with_draft()
        sublime.set_timeout(_rearm, 30)

    def _handle_command(self, session, cmd):
        """Handle a slash command."""
        if cmd.name == "clear":
            self._cmd_clear(session)
        elif cmd.name == "compact":
            self._cmd_compact(session)
        elif cmd.name == "context":
            self._cmd_context(session)
        elif cmd.name == "goal":
            # Plugin-owned goal harness — do not forward raw /goal to agent
            # (would double-drive Grok's native harness).
            session.handle_goal_command(cmd.args)
        else:
            # Unknown command — send as regular prompt to Claude (the CLI's own
            # slash commands like /loop are handled by the engine forwarding
            # the slash to the TUI; for SDK-bridge sessions this hits the
            # bridge query path).
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
        session.output.enter_input_mode()  # scrolls true bottom via focus_composer


class ClaudeGoalStatusCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s:
            s.handle_goal_command("status")


class ClaudeGoalPauseCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s:
            s.handle_goal_command("pause")


class ClaudeGoalResumeCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s:
            s.handle_goal_command("resume")


class ClaudeGoalClearCommand(sublime_plugin.WindowCommand):
    def run(self):
        s = get_active_session(self.window)
        if s:
            s.handle_goal_command("clear")


class ClaudeInsertCommand(sublime_plugin.TextCommand):
    """Insert text at position in Claude output view."""
    def run(self, edit, pos, text):
        self.view.insert(edit, pos, text)


class ClaudeToggleTasksFoldCommand(sublime_plugin.TextCommand):
    """Expand/collapse the Tasks list in a Claude output view.

    Folded (default): running tasks always visible, pending capped at 3 total,
    completed hidden. Expanded: everything shown.

    Works in input mode: re-renders via refresh_preserving_input so the draft
    prompt is not lost (plain _render_current no-ops while input_mode is on).
    """
    def run(self, edit):
        view = self.view
        expanded = view.settings().get("claude_tasks_expanded", False)
        view.settings().set("claude_tasks_expanded", not expanded)
        s = get_session_for_view(view)
        if not s or not s.output:
            return
        # Always use preserve path when input is open; otherwise normal render.
        if s.output.is_input_mode():
            s.output.refresh_preserving_input()
        else:
            s.output._render_current()

    def is_enabled(self):
        return self.view.settings().get("claude_output", False)


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


class ClaudeUndoMessageCommand(sublime_plugin.TextCommand):
    """Undo last conversation turn."""
    def run(self, edit):
        s = get_session_for_view(self.view)
        if s:
            s.undo_message()


class ClaudeClearNotificationsCommand(sublime_plugin.WindowCommand):
    """List and clear active notifications."""
    def run(self) -> None:
        import threading

        def fetch():
            import json, socket
            sock_path = os.path.expanduser("~/.notalone/notalone.sock")
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect(sock_path)
                sock.sendall((json.dumps({"method": "list"}) + "\n").encode())
                data = b""
                while b"\n" not in data:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                sock.close()
                result = json.loads(data.decode().strip())
                notifications = result.get("notifications", [])
            except Exception as e:
                sublime.set_timeout(lambda: sublime.status_message(f"notalone not available: {e}"), 0)
                return

            if not notifications:
                sublime.set_timeout(lambda: sublime.status_message("No active notifications"), 0)
                return

            items = []
            for n in notifications:
                ntype = n.get("type", "?")
                nid = n.get("id", "?")
                params = n.get("params", {})
                desc = params.get("display_message") or params.get("wake_prompt", "")[:50] or str(params)[:50]
                items.append([f"{ntype}: {desc}", f"id: {nid}"])

            def show():
                def on_select(idx):
                    if idx < 0:
                        return
                    # Clear selected notification
                    nid = notifications[idx].get("id")
                    if nid:
                        threading.Thread(target=lambda: _unregister(nid, sock_path), daemon=True).start()

                self.window.show_quick_panel(items, on_select)

            sublime.set_timeout(show, 0)

        def _unregister(nid, sock_path):
            import json, socket
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect(sock_path)
                sock.sendall((json.dumps({"method": "unregister", "notification_id": nid}) + "\n").encode())
                data = sock.recv(4096)
                sock.close()
                sublime.set_timeout(lambda: sublime.status_message(f"Cleared notification {nid}"), 0)
            except Exception as e:
                sublime.set_timeout(lambda: sublime.status_message(f"Failed to clear: {e}"), 0)

        threading.Thread(target=fetch, daemon=True).start()


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
        from ..order_table import get_table, refresh_order_table

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
        from ..order_table import get_table, refresh_order_table

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
        from ..order_table import get_table, show_order_table

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
        from ..order_table import get_table, refresh_order_table

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
        from ..order_table import show_order_table
        view = show_order_table(self.window)
        if not view:
            sublime.status_message("No project folder")


class ClaudeOrderGotoCommand(sublime_plugin.TextCommand):
    """Jump to the order or edit location under cursor."""

    def run(self, edit):
        import re
        from ..order_table import get_table

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
        from ..order_table import get_table, _relative_path

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
        from ..order_table import get_table, refresh_order_table

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
        from ..order_table import get_table, refresh_order_table

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
        from ..order_table import get_table, refresh_order_table

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
        from ..order_table import get_table, _relative_path

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
        from ..order_table import get_table, refresh_order_table, _relative_path

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
        from ..order_table import _views, get_table

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
        from ..order_table import get_table, refresh_order_table

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
        from ..order_table import get_table, _relative_path

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
        from ..core import get_session_for_view

        session = get_session_for_view(self.view)
        if not session:
            sublime.status_message("No active Claude session")
            return

        image_data, mime_type, file_paths_from_clip = self._get_clipboard_image()

        def _ensure_input():
            try:
                if not session.output:
                    return
                if not session.output.is_input_mode():
                    session.output.enter_input_mode()
                elif session.output.is_input_mode():
                    session.output.focus_composer(force_show=True)
            except Exception:
                pass

        # File/dir paths from Finder — context chips (path ref / file content)
        if file_paths_from_clip:
            valid_paths = [p for p in file_paths_from_clip if os.path.exists(p)]
            if valid_paths:
                for p in valid_paths:
                    session.add_context_path(p)
                _ensure_input()
                sublime.status_message(
                    f"Added {len(valid_paths)} path(s) to context")
                return

        if image_data:
            session.add_context_image(image_data, mime_type)
            _ensure_input()
            sublime.status_message(
                f"Image added to context ({len(image_data)} bytes) — send prompt to attach")
            return

        # No image or file paths from pasteboard, check text clipboard
        text = sublime.get_clipboard()
        if text:
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            # Absolute-looking paths that exist as file or directory
            path_lines = [
                line for line in lines
                if os.path.isfile(line) or os.path.isdir(line)
            ]
            if path_lines and len(path_lines) == len(lines):
                for p in path_lines:
                    session.add_context_path(p)
                _ensure_input()
                sublime.status_message(
                    f"Added {len(path_lines)} path(s) to context")
                return

            print(f"[Claude] paste: trying context paste...")
            if self._try_paste_as_context(session, text):
                print(f"[Claude] paste: added as context")
                return
            print(f"[Claude] paste: plain text insert")
            self.view.run_command("insert", {"characters": text})

    def _try_paste_as_context(self, session, text):
        import os
        from ..listeners import _last_copy_meta
        if not _last_copy_meta:
            return False
        if _last_copy_meta["text"] != text:
            return False
        path = _last_copy_meta["file"]
        regions = _last_copy_meta["regions"]
        from ..context_manager import format_line_range
        region_str = ",".join(
            format_line_range(start, end) for start, end in regions)
        label = f"{path}:{region_str}"
        session.add_context_selection(label, text)
        sublime.status_message(
            f"Pasted as context: {os.path.basename(path)}:{region_str}")
        return True

    def _get_clipboard_image(self):
        """Check if clipboard contains image data using platform-specific helper.

        Always returns (image_bytes|None, mime|None, file_paths|None).
        """
        import os
        import platform
        import subprocess
        import base64

        try:
            # helpers/ lives at package root (sibling of commands/), not under
            # this module dir — after the commands.py → commands/ split,
            # dirname(__file__) is .../commands/.
            pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            helpers_dir = os.path.join(pkg_root, "helpers")
            system = platform.system()

            if system == "Darwin":
                helper = os.path.join(helpers_dir, "clipboard_image.js")
                cmd = ["osascript", "-l", "JavaScript", helper]
            elif system == "Linux":
                helper = os.path.join(helpers_dir, "clipboard_image_linux.sh")
                cmd = ["bash", helper]
            elif system == "Windows":
                helper = os.path.join(helpers_dir, "clipboard_image_windows.ps1")
                cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", helper]
            else:
                return None, None, None

            if not os.path.isfile(helper):
                print(f"[Claude] clipboard helper missing: {helper}")
                return None, None, None

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15)
            if result.returncode != 0 and result.stderr:
                print(f"[Claude] clipboard helper stderr: {result.stderr[:300]}")
            output = (result.stdout or "").strip()
            if not output:
                return None, None, None

            if output.startswith("file_paths"):
                # Finder/file copy → paths only (including .png). Agent can
                # read_image the path; do not embed as multimodal image data.
                paths = [p.strip() for p in output.split("\n")[1:] if p.strip()]
                return None, None, paths

            if output.startswith("image/"):
                lines = output.split("\n")
                mime_type = lines[0].strip()
                # base64 may be multi-line; join remainder
                b64_data = "".join(lines[1:]).strip()
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

        # Super+click (or plain click via same command) on fold hint / goal line
        # toggles the task list. Works in input mode (toggle preserves draft).
        if "super+click to expand" in line or "super+click to collapse" in line:
            self.view.run_command("claude_toggle_tasks_fold")
            return
        if line.strip().startswith("◎ goal ·") and self.view.settings().get("claude_output"):
            self.view.run_command("claude_toggle_tasks_fold")
            return
        # Also accept task rows when folded (hint may be the only affordance,
        # but goal+pending rows are common click targets).
        if (line.strip().startswith("… +") and "more" in line
                and self.view.settings().get("claude_output")):
            self.view.run_command("claude_toggle_tasks_fold")
            return

        # Media tool line / "· preview" → popup (not inline phantom).
        media_path = self._media_path_from_line(line)
        if media_path:
            try:
                from .. import claude_code
                session = claude_code.get_session_for_view(self.view)
                if session and session.output:
                    session.output.show_media_popup(media_path, location=pt)
                    return
            except Exception:
                pass

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

                # Media short/absolute path under cursor → popup preview
                if path_with_line.startswith(("images/", "videos/")) or (
                        path_with_line.lower().endswith(
                            (".png", ".jpg", ".jpeg", ".webp", ".gif",
                             ".mp4", ".webm", ".mov"))):
                    resolved = path_with_line if os.path.isfile(path_with_line) \
                        else self._resolve_media_short_path(path_with_line)
                    if resolved and os.path.isfile(resolved):
                        try:
                            from .. import claude_code
                            session = claude_code.get_session_for_view(self.view)
                            if session and session.output:
                                session.output.show_media_popup(
                                    resolved, location=pt)
                                return
                        except Exception:
                            pass
                        return

                # Check if file exists
                if os.path.isfile(path_with_line):
                    window = self.view.window()
                    if window:
                        # Sync session edit target from transcript path preview
                        try:
                            from .. import claude_code
                            session = claude_code.get_session_for_view(self.view)
                            if session and hasattr(session, "set_edit_target"):
                                session.set_edit_target(
                                    path_with_line,
                                    as_context=False,
                                    line=line_num,
                                    announce=True,
                                )
                        except Exception:
                            pass
                        if line_num:
                            window.open_file(f"{path_with_line}:{line_num}", sublime.ENCODED_POSITION)
                        else:
                            window.open_file(path_with_line)
                    return

        sublime.status_message("No link or file path found at cursor")

    def _media_path_from_line(self, line: str):
        """If this is a done media tool line, return absolute media path."""
        from ..tool_formatters import MEDIA_TOOLS, media_display_path, extract_media_path
        #  ✔ image_gen: … → images/1.jpg  · preview
        for name in MEDIA_TOOLS:
            if name in line and ("→" in line or "preview" in line):
                # Prefer short path token after →
                m = re.search(
                    r'→\s*((?:images|videos)/[^\s]+|\S+\.(?:png|jpe?g|webp|gif|mp4|webm|mov))',
                    line, re.I)
                short = m.group(1) if m else None
                if short:
                    resolved = self._resolve_media_short_path(short)
                    if resolved:
                        return resolved
                # Fall back: latest matching tool by name
                try:
                    from .. import claude_code
                    session = claude_code.get_session_for_view(self.view)
                    if not session or not session.output:
                        return None
                    out = session.output
                    convs = list(out.conversations)
                    if out.current is not None:
                        convs.append(out.current)
                    for conv in reversed(convs):
                        for event in reversed(getattr(conv, "events", []) or []):
                            if getattr(event, "name", "") != name:
                                continue
                            inp = getattr(event, "tool_input", None) or {}
                            path = inp.get("_media_path") or extract_media_path(
                                getattr(event, "result", None), inp)
                            if path and os.path.isfile(path):
                                return path
                except Exception:
                    return None
        return None

    def _resolve_media_short_path(self, short: str):
        """Map images/1.jpg (or basename) to absolute path via tool results."""
        try:
            from .. import claude_code
            from ..tool_formatters import media_display_path, extract_media_path, MEDIA_TOOLS
            session = claude_code.get_session_for_view(self.view)
            if not session or not session.output:
                return None
            out = session.output
            convs = list(out.conversations)
            if out.current is not None:
                convs.append(out.current)
            for conv in reversed(convs):
                for event in reversed(getattr(conv, "events", []) or []):
                    name = getattr(event, "name", "")
                    if name not in MEDIA_TOOLS:
                        continue
                    inp = getattr(event, "tool_input", None) or {}
                    path = inp.get("_media_path") or extract_media_path(
                        getattr(event, "result", None), inp)
                    if not path:
                        continue
                    disp = media_display_path(path)
                    base = os.path.basename(path)
                    if short in (disp, base, path) or path.endswith("/" + short):
                        return path
        except Exception:
            return None
        return None

    def want_event(self):
        return True


class ClaudeRetainCommand(sublime_plugin.WindowCommand):
    """Manage session retain content for compaction."""

    def run(self, action="view"):
        from ..core import get_active_session

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
