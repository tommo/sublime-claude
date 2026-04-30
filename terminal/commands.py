# Sublime Text commands for Claude Terminal

import os
import sublime
import sublime_plugin

import logging
from .terminal import Terminal
from .key import get_key_code

logger = logging.getLogger('Terminus')


class ClaudeTerminalOpenCommand(sublime_plugin.WindowCommand):
    """Open a new Claude terminal view and start a shell."""
    def run(self, tag=None, cwd=None, cmd=None, env=None):
        if tag and Terminal.from_tag(tag):
            # Already open — just focus it
            t = Terminal.from_tag(tag)
            self.window.focus_view(t.view)
            return

        session_name = tag or "Terminal"
        view = self.window.new_file()
        view.set_name(session_name)
        view.set_scratch(True)
        view.settings().set("claude_terminal", True)
        view.settings().set("claude_terminal_tag", tag or "")
        view.settings().set("line_numbers", False)
        view.settings().set("gutter", False)
        view.settings().set("highlight_line", False)
        view.settings().set("draw_centered", False)
        view.settings().set("word_wrap", False)
        view.settings().set("auto_complete", False)
        view.settings().set("draw_white_space", "none")
        view.settings().set("draw_unicode_white_space", False)
        view.settings().set("draw_indent_guides", False)
        view.settings().set("scroll_past_end", True)
        view.settings().set("color_scheme", "Packages/ClaudeCode/ClaudeCode.hidden-color-scheme")

        if not cwd and self.window.folders():
            cwd = self.window.folders()[0]

        if not cmd:
            cmd = [os.environ.get("SHELL", "/bin/bash"), "-i", "-l"]

        env = dict(os.environ, **(env or {}))

        terminal = Terminal(view)
        terminal.start(cmd=cmd, cwd=cwd, env=env, tag=tag or "", default_title=session_name)


class ClaudeTerminalKeypressCommand(sublime_plugin.TextCommand):
    def run(self, edit, **kwargs):
        terminal = Terminal.from_id(self.view.id())
        if terminal:
            terminal._track_key(**kwargs)
            terminal.send_key(**kwargs)


class ClaudeTerminalPasteCommand(sublime_plugin.TextCommand):
    def run(self, edit, text=None, bracketed=True):
        terminal = Terminal.from_id(self.view.id())
        if not terminal:
            return
        if text is None:
            text = sublime.get_clipboard()
        terminal._track_paste(text)
        if bracketed and terminal.bracketed_paste_mode_enabled():
            terminal.send_key(key="bracketed_paste_mode_start")
            terminal.send_string(text)
            terminal.send_key(key="bracketed_paste_mode_end")
        else:
            terminal.send_string(text)


class ClaudeTerminalCopyCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if self.view.sel():
            text = self.view.substr(self.view.sel()[0])
            sublime.set_clipboard(text)


class ClaudeTerminalSendStringCommand(sublime_plugin.TextCommand):
    """Used programmatically to send a string to the terminal (e.g. from MCP)."""
    def run(self, edit, string=""):
        terminal = Terminal.from_id(self.view.id())
        if terminal:
            terminal.send_string(string)


class ClaudeTerminalCloseCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        terminal = Terminal.from_id(self.view.id())
        if terminal:
            terminal.close()
        self.view.close()


class ClaudeTerminalPasteTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, text="", bracketed=True):
        terminal = Terminal.from_id(self.view.id())
        if not terminal:
            return
        if bracketed and terminal.bracketed_paste_mode_enabled():
            terminal.send_key(key="bracketed_paste_mode_start")
            terminal.send_string(text)
            terminal.send_key(key="bracketed_paste_mode_end")
        else:
            terminal.send_string(text)


class ClaudeTerminalClearUndoStackCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        # pyte maintains the screen; the Sublime undo stack must stay clear
        # so user "undo" doesn't try to revert terminal renders
        self.view.run_command("erase_undo_stack", {"size": 1024})


class NoopCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        pass


class ClaudeTerminalActivateCommand(sublime_plugin.TextCommand):
    """Reattach a terminal session after Sublime restarts (hot-exit restore)."""
    def run(self, edit, cmd=None, cwd=None, tag=None, env=None):
        if Terminal.from_id(self.view.id()):
            return
        session_name = tag or "Terminal"
        self.view.set_scratch(True)
        env = dict(os.environ, **(env or {}))
        terminal = Terminal(self.view)
        terminal.start(cmd=cmd, cwd=cwd or None, env=env,
                       tag=tag or "", default_title=session_name)


class ClaudeTerminalResetCommand(sublime_plugin.TextCommand):
    def run(self, edit, soft=False):
        terminal = Terminal.from_id(self.view.id())
        if not terminal:
            return
        if not soft:
            self.view.replace(edit, sublime.Region(0, self.view.size()), "")
            terminal.offset = 0
        terminal.screen.reset()
        terminal._pending_to_reset[0] = None
