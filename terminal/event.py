import sublime
import sublime_plugin

import logging
import difflib
from random import random

from .clipboard import g_clipboard_history
from .recency import RecencyManager
from .terminal import Terminal

logger = logging.getLogger('Terminus')


class ClaudeTerminalEventListener(sublime_plugin.EventListener):

    def on_activated_async(self, view):
        recency_manager = RecencyManager.from_view(view)
        if not recency_manager:
            return

        if not view.settings().get("claude_terminal", False):
            recency_manager.cycling_panels = False
            return

        if random() > 0.7:
            # occassionally cull zombie terminals
            Terminal.cull_terminals()
            # clear undo stack
            view.run_command("claude_terminal_clear_undo_stack")

        terminal = Terminal.from_id(view.id())
        if terminal:
            recency_manager.set_recent_terminal(view)
            return

        reactivable = view.settings().get("claude_terminal.reactivable", False)
        finished = view.settings().get("claude_terminal.finished", False)
        if not reactivable or finished:
            return
        # A view may specify its own reactivation command (e.g. Claude Code
        # rebuilds argv to --resume its session + refresh the MCP view scope)
        # instead of the generic verbatim restore.
        custom = view.settings().get("claude_terminal.reactivate_command")
        if custom:
            sublime.set_timeout(lambda: view.run_command(custom), 100)
            return
        kwargs = view.settings().get("claude_terminal.args", {})
        if "cmd" not in kwargs:
            return
        sublime.set_timeout(lambda: view.run_command("claude_terminal_activate", kwargs), 100)

    def on_pre_close(self, view):
        # panel doesn't trigger on_pre_close
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return
        # Revealed PTY-engine session: closing the view must NOT kill the borrowed
        # pty — hand it back to the engine's native view (view is already closing).
        if view.settings().get("pty_reveal_owner"):
            sess = getattr(sublime, "_claude_sessions", {}).get(view.id())
            if sess is not None and getattr(sess, "terminal_revealed", False):
                sess.return_to_native(close_view=False)
            else:
                terminal.release()
            return
        terminal.kill()

    def on_modified(self, view):
        # to catch unicode input
        terminal = Terminal.from_id(view.id())
        if not terminal or not getattr(terminal, 'process', None) or not terminal.process.isalive():
            return
        command, args, _ = view.command_history(0)
        if command.startswith("claude_terminal"):
            return
        elif command == "insert" and "characters" in args and \
                len(view.sel()) == 1 and view.sel()[0].empty():
            chars = args["characters"]
            current_cursor = view.sel()[0].end()
            region = sublime.Region(
                max(current_cursor - len(chars), self._cursor), current_cursor)
            text = view.substr(region)
            self._cursor = current_cursor
            logger.debug("text {} detected".format(text))
            terminal._track_char(text)
            view.run_command("claude_terminal_paste_text", {"text": text, "bracketed": False})
        elif command:
            logger.debug("undo {}".format(command))
            view.run_command("soft_undo")

    def on_selection_modified(self, view):
        terminal = Terminal.from_id(view.id())
        if not terminal or not getattr(terminal, 'process', None) or not terminal.process.isalive():
            return
        if len(view.sel()) != 1 or not view.sel()[0].empty():
            return
        self._cursor = view.sel()[0].end()

    def on_text_command(self, view, name, args):
        if not view.settings().get('claude_terminal'):
            return
        if name == "copy":
            return ("claude_terminal_copy", None)
        elif name == "paste":
            return ("claude_terminal_paste", None)
        elif name == "paste_and_indent":
            return ("claude_terminal_paste", None)
        elif name == "paste_from_history":
            return ("claude_terminal_paste_from_history", None)
        elif name == "paste_selection_clipboard":
            self._pre_paste = view.substr(view.visible_region())
        elif name == "undo":
            return ("noop", None)

    def on_post_text_command(self, view, name, args):
        if not view.settings().get('claude_terminal'):
            return
        if name == 'claude_terminal_copy':
            g_clipboard_history.push_text(sublime.get_clipboard())
        elif name == "paste_selection_clipboard":
            added = [
                df[2:] for df in difflib.ndiff(self._pre_paste, view.substr(view.visible_region()))
                if df[0] == '+']
            view.run_command("claude_terminal_paste_text", {"text": "".join(added)})

    def on_query_context(self, view, key, operator, operand, match_all):
        if key == "claude_terminal":
            val = view.settings().get("claude_terminal", False)
            result = val == operand if operator == sublime.OP_EQUAL else val != operand if operator == sublime.OP_NOT_EQUAL else bool(val)
            return result
        if key == "claude_terminal_capture_scroll":
            # true when the wheel should drive the app (alt-screen / mouse tracking)
            terminal = Terminal.from_id(view.id())
            val = bool(terminal and terminal.wants_scroll_capture())
            logger.info("query claude_terminal_capture_scroll -> %s", val)
            if operator == sublime.OP_EQUAL:
                return val == operand
            if operator == sublime.OP_NOT_EQUAL:
                return val != operand
            return val
        # Catch-all for `claude_terminal_view.*` keys (e.g.
        # claude_terminal_view.finished), resolved from view settings — like
        # Terminus. WITHOUT this the escape keybinding's
        # `claude_terminal_view.finished != true` condition is unresolved, so the
        # whole binding never matches and Escape isn't forwarded to the pty.
        if key.startswith("claude_terminal_view"):
            val = view.settings().get(key, None)
            if operator == sublime.OP_EQUAL:
                return val == operand
            if operator == sublime.OP_NOT_EQUAL:
                return val != operand
            return bool(val)
        return None

    def on_window_command(self, window, command_name, args):
        if command_name == "show_panel":
            panel = args["panel"].replace("output.", "")
            view = window.find_output_panel(panel)
            if view:
                terminal = Terminal.from_id(view.id())
                if terminal and terminal.show_in_panel:
                    recency_manager = RecencyManager.from_view(view)
                    if recency_manager:
                        recency_manager.set_recent_terminal(view)
