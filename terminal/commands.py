# Sublime Text commands for Claude Terminal

import os
import re
import sys
import webbrowser
import sublime
import sublime_plugin

import logging
from .terminal import Terminal
from .key import get_key_code

logger = logging.getLogger('Terminus')


def _setup_logging():
    """Route the Terminus logger to the ST console when the ClaudeTerminal "debug"
    setting is on. Re-runnable (drops our prior handler on reload)."""
    lg = logging.getLogger('Terminus')
    for h in list(lg.handlers):
        if getattr(h, '_claude_terminal', False):
            lg.removeHandler(h)
    s = sublime.load_settings("ClaudeTerminal.sublime-settings")
    debug = s.get("debug", False)
    trace = s.get("trace", False)  # also emit the verbose per-frame render logs
    if debug or trace:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("[ClaudeTerminal] %(message)s"))
        h._claude_terminal = True
        lg.addHandler(h)
        lg.setLevel(logging.DEBUG if trace else logging.INFO)
    else:
        lg.setLevel(logging.WARNING)
    lg.propagate = False


_setup_logging()


def new_terminal_view(window, name, tag=None):
    """Create + configure a blank terminal view (no process started yet).

    Exposed so callers that need the view id before spawning (e.g. wiring an
    MCP --view-id into the launched command) can create the view, read its id,
    then run their own Terminal(view).start(...)."""
    view = window.new_file()
    view.set_name(name)
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
    # terminal scrolling: pin the live screen to the viewport bottom (no
    # scroll-past-end slack). The renderer follows the bottom from the viewport
    # position, so no follow flag is seeded here.
    view.settings().set("scroll_past_end", False)
    view.settings().set("color_scheme", "Packages/User/ClaudeTerminal.hidden-color-scheme")
    # standalone font size, independent of the editor's global font_size
    fs = sublime.load_settings("ClaudeTerminal.sublime-settings").get("font_size")
    if fs:
        view.settings().set("font_size", fs)
    return view


class ClaudeTerminalOpenCommand(sublime_plugin.WindowCommand):
    """Open a new Claude terminal view and start a shell."""
    def run(self, tag=None, cwd=None, cmd=None, env=None):
        if tag and Terminal.from_tag(tag):
            # Already open — just focus it
            t = Terminal.from_tag(tag)
            self.window.focus_view(t.view)
            return

        session_name = tag or "Terminal"

        # Expand Sublime variables (e.g. keymap cwd "${file_path:${folder}}") —
        # Sublime does NOT auto-expand command args, so resolve them here as
        # Terminus does, then fall back to the project folder / home.
        if cwd and "${" in cwd:
            cwd = sublime.expand_variables(cwd, self.window.extract_variables()) or ""
        if not cwd or not os.path.isdir(cwd):
            folders = self.window.folders()
            cwd = folders[0] if folders else os.path.expanduser("~")

        view = new_terminal_view(self.window, session_name, tag)

        if not cmd:
            cmd = [os.environ.get("SHELL", "/bin/bash"), "-i", "-l"]

        env = dict(os.environ, **(env or {}))

        terminal = Terminal(view)
        terminal.start(cmd=cmd, cwd=cwd, env=env, tag=tag or "", default_title=session_name)


class ClaudeTerminalKeypressCommand(sublime_plugin.TextCommand):
    def run(self, edit, **kwargs):
        terminal = Terminal.from_id(self.view.id())
        if terminal:
            # typing returns to the live prompt, like a real terminal: pin the
            # next render to the bottom regardless of current scroll position.
            self.view.settings().set("claude_terminal_view.pin_bottom", True)
            terminal._track_key(**kwargs)
            terminal.send_key(**kwargs)


class ClaudeTerminalPasteCommand(sublime_plugin.TextCommand):
    def run(self, edit, text=None, bracketed=True):
        terminal = Terminal.from_id(self.view.id())
        if not terminal:
            return
        if text is None:
            text = sublime.get_clipboard()
        self.view.settings().set("claude_terminal_view.pin_bottom", True)
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


_URL_RE = re.compile(r'(?:https?|file)://[^\s\]\)>\'"]+')

# A path token + optional location. CC prints paths as plain text (no OSC-8
# links), both absolute (Update(/abs/x.nim)) and cwd-relative (Update(api/x.nim),
# Write(test/test.nim)). Three path shapes, then an optional location in either
# `:line[:col]` (compiler/grep) or `(line, col)` (Nim) form:
#   1. a path with an internal slash (relative or absolute): api/raw.nim, /a/b.c
#   2. a leading-slash absolute path: /Volumes/.../x.nim
#   3. a bare filename with an extension: vendor_opencv.nim
_PATH_RE = re.compile(
    r'(?P<path>~?/?[\w.+@-]*[\w]/[\w./+@-]+|/[\w./+@-]+|[\w.+@-]+\.[A-Za-z]\w*)'
    r'(?::(?P<l1>\d+)(?::(?P<c1>\d+))?|\((?P<l2>\d+),\s*(?P<c2>\d+)\))?')


class ClaudeTerminalOpenLinkCommand(sublime_plugin.TextCommand):
    """Cmd+click a file path or URL under the cursor.

    Claude Code (and shell tools) print file paths as plain text relative to the
    session cwd, so we detect the token under the cursor and resolve it against
    that cwd (persisted on the view by Terminal.start) before opening. Supports
    `path:line[:col]` and Nim-style `path(line, col)` locations."""

    def run(self, edit, event=None):
        view = self.view
        if event:
            pt = view.window_to_text((event["x"], event["y"]))
        else:
            sel = view.sel()
            if not sel:
                return
            pt = sel[0].begin()
        line_region = view.line(pt)
        line = view.substr(line_region)
        col = pt - line_region.begin()

        for m in _URL_RE.finditer(line):
            if m.start() <= col <= m.end():
                webbrowser.open(m.group())
                return

        cwd = self._cwd()
        for m in _PATH_RE.finditer(line):
            if not (m.start() <= col <= m.end()):
                continue
            resolved = self._resolve(m.group("path"), cwd)
            if resolved:
                self._open(resolved, m.group("l1") or m.group("l2"),
                           m.group("c1") or m.group("c2"))
                return
        sublime.status_message("No file path or URL under cursor")

    def _cwd(self):
        # cwd persisted by Terminal.start (same value the launch used); falls back
        # to the window's first folder for terminals started without an explicit cwd.
        args = self.view.settings().get("claude_terminal.args") or {}
        cwd = args.get("cwd")
        if cwd:
            return cwd
        win = self.view.window()
        folders = win.folders() if win else []
        return folders[0] if folders else None

    @staticmethod
    def _resolve(path, cwd):
        path = os.path.expanduser(path)
        candidates = [path]
        if not os.path.isabs(path) and cwd:
            candidates.append(os.path.join(cwd, path))
        for p in candidates:
            if os.path.isfile(p):
                return p
        return None

    def _open(self, path, row, col):
        win = self.view.window()
        if not win:
            return
        if row:
            win.open_file("{}:{}:{}".format(path, row, col or 1), sublime.ENCODED_POSITION)
        else:
            win.open_file(path)

    def want_event(self):
        return True


class ClaudeTerminalSendStringCommand(sublime_plugin.TextCommand):
    """Used programmatically to send a string to the terminal (e.g. from MCP)."""
    def run(self, edit, string=""):
        terminal = Terminal.from_id(self.view.id())
        if terminal:
            terminal.send_string(string)


class ClaudeTerminalCloseCommand(sublime_plugin.TextCommand):
    def run(self, edit, force=False):
        view = self.view
        # A revealed PTY-engine session: cmd+w hands the live process back to the
        # engine's native view — never kill the borrowed pty.
        if not force and view.settings().get("pty_reveal_owner"):
            sess = getattr(sublime, "_claude_sessions", {}).get(view.id())
            if sess is not None and getattr(sess, "terminal_revealed", False):
                sess.return_to_native()
                return
        terminal = Terminal.from_id(view.id())
        # Nothing running (or forced) → close immediately.
        if force or not terminal or not terminal.is_alive():
            if terminal:
                terminal.close()
            view.close()
            return
        # Confirm before killing a live terminal session. set_timeout so the
        # dialog doesn't block command dispatch (blocking mid-dispatch can make
        # the next Cmd+W bypass this binding).
        def _ask():
            t = Terminal.from_id(view.id())
            if not t or not t.is_alive():
                view.close()
                return
            if sublime.ok_cancel_dialog("Close this terminal session?", "Close"):
                t.close()
                view.close()
        sublime.set_timeout(_ask, 0)


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


class ClaudeTerminalScrollCommand(sublime_plugin.TextCommand):
    """Wheel handler for terminal views (bound for all terminal views).

    - mouse-tracking TUIs (Grok widgets with scrollbars): SGR wheel at pointer
    - alt-screen without mouse tracking: cursor keys
    - normal buffer: discrete line-based scrollback scroll

    Linux/Windows: mousemap delivers scroll_up/scroll_down here.
    macOS: ST does not synthesize scroll_* for the trackpad; wheel cannot
    be captured without fighting the viewport — use keys or enable Grok
    mouse reporting and use a platform that delivers scroll buttons.
    """
    def run(self, edit, action="up", lines=3):
        terminal = Terminal.from_id(self.view.id())
        if not terminal:
            return
        if terminal.wants_scroll_capture():
            logger.info("scroll -> app: %s x%d", action, lines)
            terminal.scroll(action, lines)
            return
        v = self.view
        lh = v.line_height()
        x, y = v.viewport_position()
        new_y = y + (-lines * lh if action == "up" else lines * lh)
        if new_y < 0:
            new_y = 0.0
        # bottom = last content line at viewport bottom (ignore scroll_past_end slack)
        last_y = v.text_to_layout(v.size())[1]
        max_y = max(0.0, last_y - v.viewport_extent()[1] + lh)
        if new_y > max_y:
            new_y = max_y
        v.set_viewport_position((x, new_y), False)
        logger.info("scroll view: %s x%d -> y=%d", action, lines, int(new_y))


class ClaudeTerminalAdjustFontSizeCommand(sublime_plugin.TextCommand):
    """Standalone per-terminal font size: increase / decrease / reset.

    Only changes the view's font_size. Changing it alters em_width/line_height,
    so the pty needs resizing to the new row/col count -- but that is handled
    safely by the render loop's was_resized() poller on the renderer thread,
    under its own lock. We must NOT call handle_resize() here: acquiring the
    terminal lock on the main thread while the renderer holds it (and needs the
    main thread to run commands) deadlocks Sublime.
    """
    MIN, MAX, STEP = 6, 72, 1

    def run(self, edit, action="increase"):
        view = self.view
        if not Terminal.from_id(view.id()):
            return
        default = sublime.load_settings("ClaudeTerminal.sublime-settings").get("font_size") \
            or view.settings().get("font_size", 12)
        current = view.settings().get("font_size", default)
        if action == "increase":
            new = current + self.STEP
        elif action == "decrease":
            new = current - self.STEP
        elif action == "reset":
            new = default
        else:
            return
        view.settings().set("font_size", max(self.MIN, min(self.MAX, new)))
