import sublime
import sublime_plugin

import time
import math
import logging
import pyte
from functools import lru_cache
from wcwidth import wcswidth


from .const import CONTINUATION
from .ptty import XTERM_256_COLORS
from .terminal import Terminal
from .utils import rev_wcwidth, get_highlight_key

logger = logging.getLogger('Terminus')


@lru_cache(maxsize=10000)
def is_supported_color(c):
    return c in ['default', 'reverse_default'] or c in XTERM_256_COLORS


RGB256 = {}
for c in pyte.graphics.FG_BG_256:
    RGB256[c] = tuple(int(c[i:i+2], 16) for i in (0, 2, 4))


# https://en.wikipedia.org/wiki/Color_difference#sRGB
@lru_cache(maxsize=10000)
def get_closest_color(c):
    r, g, b = tuple(int(c[i:i+2], 16) for i in (0, 2, 4))
    dmin = 1000000
    closest_color = "000000"
    for c, (r2, g2, b2) in RGB256.items():
        redmean = (r + r2) / 2
        d = (2 + redmean / 256) * (r - r2) ** 2 + 4 * \
            (g - g2)**2 + (2 + (255-redmean) / 256) * (b - b2)**2
        if d < dmin:
            dmin = d
            closest_color = c
    return closest_color


def reverse_fg_bg(fg, bg):
    fg, bg = bg, fg
    if fg == "default":
        fg = "reverse_default"
    if bg == "default":
        bg = "reverse_default"
    return fg, bg


def segment_buffer_line(buffer_line):
    """
    segment a buffer line based on bg and fg colors
    """
    is_wide_char = False
    text = ""
    start = 0
    counter = 0
    fg = "default"
    bg = "default"
    bold = False
    reverse = False

    if buffer_line:
        last_index = max(buffer_line.keys()) + 1
    else:
        last_index = 0

    for i in range(last_index):
        if is_wide_char:
            is_wide_char = False
            continue
        char = buffer_line[i]
        is_wide_char = wcswidth(char.data) >= 2

        if counter == 0:
            counter = i
            text = " " * i

        if fg != char.fg or bg != char.bg or bold != char.bold or reverse != char.reverse:
            if reverse:
                fg, bg = reverse_fg_bg(fg, bg)
            yield text, start, counter, fg, bg, bold
            fg = char.fg
            bg = char.bg
            bold = char.bold
            reverse = char.reverse
            text = char.data
            start = counter
        else:
            text += char.data

        counter += 1

    if reverse:
        fg, bg = reverse_fg_bg(fg, bg)
    yield text, start, counter, fg, bg, bold


class TerminusViewMixin:

    def ensure_position(self, edit, row, col=0):
        view = self.view
        lastrow = view.rowcol(view.size())[0]
        if lastrow < row:
            view.insert(edit, view.size(), "\n" * (row - lastrow))
        line_region = view.line(view.text_point(row, 0))
        lastcol = view.rowcol(line_region.end())[1]
        if lastcol < col:
            view.insert(edit, line_region.end(), " " * (col - lastcol))


class ClaudeTerminalRenderCommand(sublime_plugin.TextCommand, TerminusViewMixin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # it keeps all the highlight keys
        self.colored_lines = {}
        self.region_scopes = {}   # key -> scope, so we can snapshot/restore colors
        self._alt_active = False  # are we currently showing the alt screen?
        self._alt_snapshot = None  # saved primary view while on the alt screen
        # macOS does not deliver scroll_up/scroll_down mousemap events (only
        # Linux/Windows do). For alt-screen TUIs we pad the buffer so native
        # trackpad scrolling moves the viewport; each render converts that
        # delta into app scroll keys and re-centers.
        self._alt_scroll_pad = 40  # blank lines above + below the TUI grid
        self._alt_home_y = 0.0
        settings = sublime.load_settings("ClaudeTerminal.sublime-settings")
        self.scrollback_history_size = settings.get("scrollback_history_size", 10000)
        self.brighten_bold_text = settings.get("brighten_bold_text", False)
        self.dynamic_title = settings.get("dynamic_title", False)

    def run(self, edit):
        view = self.view
        startt = time.time()
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return

        screen = terminal.screen

        self._handle_alt_transition(edit, view, terminal, screen)

        if terminal._pending_to_clear_scrollback[0]:
            view.replace(edit, sublime.Region(0, view.size()), "")  # nuke everything
            terminal.offset = 0
            terminal.clean_images()
            terminal._pending_to_clear_scrollback[0] = False

        if terminal._pending_to_reset[0]:
            def _reset():
                logger.debug("reset terminal")
                view.run_command("claude_terminal_reset", {"soft": True})
                terminal._pending_to_reset[0] = False

            sublime.set_timeout(_reset)

        # Decide whether to follow the bottom from the *current viewport* before
        # appending — not from a wheel-driven flag. macOS scrolls the view
        # natively (trackpad/wheel never reach our wheel command), so a flag
        # would never flip and every frame would yank back to the bottom. Reading
        # the position works on every platform and naturally distinguishes "user
        # scrolled up to read" from "content grew below" (the latter doesn't move
        # the viewport, so we stay engaged).
        #
        # Alt-screen is special: the TUI owns scrolling. On Linux/Windows the
        # mousemap feeds the app; on macOS we convert viewport motion over a
        # padded buffer into app scroll keys (see _capture_alt_wheel).
        pin_bottom = view.settings().get("claude_terminal_view.pin_bottom", False)
        if pin_bottom:
            view.settings().set("claude_terminal_view.pin_bottom", False)  # one-shot
        alt = terminal.alternate_screen_enabled()
        following = (
            pin_bottom
            or (alt and not self._alt_wheel_capture_enabled())
            or (not alt and self._user_at_bottom(view))
        )
        prev_vp = view.viewport_position()

        self.update_lines(edit, terminal)
        if alt and self._alt_wheel_capture_enabled():
            self._ensure_alt_scroll_padding(edit, terminal)
            self._capture_alt_wheel(view, terminal)
        elif following:
            self.trim_trailing_spaces(edit, terminal)
            self.trim_history(edit, terminal)
            view.run_command("claude_terminal_show_cursor")
        else:
            # User is reading scrollback — hold their position. Appending text can
            # make Sublime auto-scroll to reveal the cursor/selection; undo that.
            # We skip trimming here so layout coords stay stable for prev_vp.
            if view.viewport_position() != prev_vp:
                view.set_viewport_position(prev_vp, False)
                sublime.set_timeout(lambda: view.set_viewport_position(prev_vp, False), 0)

        if self.dynamic_title:
            current_title = view.name()
            if terminal.title:
                if current_title != terminal.title:
                    view.set_name(terminal.title)
            else:
                if screen.title:
                    if current_title != screen.title:
                        view.set_name(screen.title)
                else:
                    if current_title != terminal.default_title:
                        view.set_name(terminal.default_title)

        # we should not clear dirty lines here, it shoud be done in the eventloop
        # screen.dirty.clear()
        logger.debug("updating lines takes {}s".format(str(time.time() - startt)))
        logger.debug("mode: {}, cursor: {}.{}".format(
            [m >> 5 for m in screen.mode], screen.cursor.x, screen.cursor.y))

    def _user_at_bottom(self, view, slack_lines=2):
        """True when the viewport sits at (or within a couple lines of) the
        buffer bottom. Computed from the live viewport so it tracks macOS native
        scrolling, which never reaches our wheel command."""
        lh = view.line_height()
        if lh <= 0:
            return True
        last_y = view.text_to_layout(view.size())[1]
        if last_y is None:
            return True  # layout transiently unavailable: default to following
        max_y = last_y - view.viewport_extent()[1] + lh
        if max_y <= 0:
            return True  # content shorter than the viewport: always "at bottom"
        return view.viewport_position()[1] >= max_y - slack_lines * lh

    def update_lines(self, edit, terminal):
        # cursor = screen.cursor
        screen = terminal.screen
        columns = screen.columns
        dirty_lines = sorted(screen.dirty)
        if dirty_lines:
            # replay history
            history = screen.history
            terminal.offset += len(history)
            offset = terminal.offset
            logger.debug("add {} line(s) to scroll back history".format(len(history)))

            for line in range(len(history)):
                buffer_line = history.pop()
                lf = buffer_line[columns - 1].linefeed
                self.update_line(edit, offset - line - 1, buffer_line, lf)

            # update dirty line¡s
            logger.debug("screen is dirty: {}".format(str(dirty_lines)))
            for line in dirty_lines:
                buffer_line = screen.buffer[line]
                lf = buffer_line[columns - 1].linefeed
                self.update_line(edit, line + offset, buffer_line, lf)

    def update_line(self, edit, line, buffer_line, lf):
        view = self.view
        # make sure the view has enough lines
        self.ensure_position(edit, line)
        line_region = view.line(view.text_point(line, 0))
        segments = list(segment_buffer_line(buffer_line))

        text = "".join(s[0] for s in segments)
        if lf:
            # append a zero width space if the the line ends with a linefeed
            # we will use it to do non-break copying and searching
            # this hack is much easier than rewraping the lines
            text += CONTINUATION

        text = text.rstrip()
        self.decolorize_line(line)
        view.replace(edit, line_region, text)
        self.colorize_line(edit, line, segments)

    def colorize_line(self, edit, line, segments):
        view = self.view
        if segments:
            # ensure the last segement's position exists
            self.ensure_position(edit, line, segments[-1][2])
            if line not in self.colored_lines:
                self.colored_lines[line] = []
        for s in segments:
            fg, bg, bold = s[3:]
            # foreground-only: a reversed cell already had fg/bg swapped upstream,
            # so the visible color we care about is fg.
            if not is_supported_color(fg):
                fg = get_closest_color(fg)
            if fg != "default":
                if bold and self.brighten_bold_text:
                    if fg != "reverse_default" and not fg.startswith("light_"):
                        fg = "light_" + fg
                a = view.text_point(line, s[1])
                b = view.text_point(line, s[2])
                key = get_highlight_key(view)
                scope = "claude_terminal_color.{}".format(fg)
                view.add_regions(key, [sublime.Region(a, b)], scope)
                self.colored_lines[line].append(key)
                self.region_scopes[key] = scope

    def decolorize_line(self, line):
        if line in self.colored_lines:
            for key in self.colored_lines[line]:
                self.view.erase_regions(key)
                self.region_scopes.pop(key, None)
            del self.colored_lines[line]

    # ─── Alternate-screen view swap ─────────────────────────────────────────

    def _handle_alt_transition(self, edit, view, terminal, screen):
        """Real-terminal behaviour: on entering the alt screen, hide the primary
        view (scrollback + last frame) so the Sublime view matches the TUI grid
        exactly; restore it on exit."""
        alt = screen.alternate_buffer_mode
        if alt == self._alt_active:
            return
        self._alt_active = alt
        logger.info("alt-screen %s: %s primary view",
                    "ENTER" if alt else "EXIT",
                    "hiding" if alt else "restoring")
        if alt:
            self._enter_alt(edit, view, terminal)
        else:
            self._leave_alt(edit, view, terminal)

    def _all_region_keys(self):
        keys = set()
        for ks in self.colored_lines.values():
            keys.update(ks)
        return keys

    def _erase_all_regions(self, view):
        for key in self._all_region_keys():
            view.erase_regions(key)
        self.colored_lines = {}
        self.region_scopes = {}

    def _alt_wheel_capture_enabled(self):
        # macOS never delivers scroll_up/scroll_down to mousemap (ST Default
        # (OSX) has no scroll buttons; Linux does). Use padded-viewport capture.
        return sublime.platform() == "osx"

    def _enter_alt(self, edit, view, terminal):
        # snapshot the primary view (text + offset + color regions) then clear it
        snap_regions = {}
        for key in self._all_region_keys():
            snap_regions[key] = (list(view.get_regions(key)), self.region_scopes.get(key, ""))
        self._alt_snapshot = {
            "text": view.substr(sublime.Region(0, view.size())),
            "offset": terminal.offset,
            "colored_lines": {ln: list(ks) for ln, ks in self.colored_lines.items()},
            "regions": snap_regions,
            "scroll_past_end": view.settings().get("scroll_past_end"),
        }
        self._erase_all_regions(view)
        view.replace(edit, sublime.Region(0, view.size()), "")
        view.settings().set("terminus.highlight_counter", 0)
        # macOS: pad so trackpad can move the viewport; convert → app scroll.
        if self._alt_wheel_capture_enabled():
            pad = self._alt_scroll_pad
            view.insert(edit, 0, "\n" * pad)
            terminal.offset = pad
            # Allow overscroll past the bottom pad as well.
            view.settings().set("scroll_past_end", True)
        else:
            terminal.offset = 0
        terminal.screen.dirty.update(range(terminal.screen.lines))

    def _ensure_alt_scroll_padding(self, edit, terminal):
        """Keep top pad + bottom pad around the TUI grid (macOS wheel capture)."""
        if not self._alt_wheel_capture_enabled():
            return
        view = self.view
        pad = self._alt_scroll_pad
        screen_lines = terminal.screen.lines
        # Top pad
        if terminal.offset < pad:
            view.insert(edit, 0, "\n" * (pad - terminal.offset))
            terminal.offset = pad
        # Bottom pad: after last grid line
        last_grid = terminal.offset + screen_lines - 1
        lastrow = view.rowcol(view.size())[0]
        need_last = last_grid + pad
        if lastrow < need_last:
            view.insert(edit, view.size(), "\n" * (need_last - lastrow))

    def _capture_alt_wheel(self, view, terminal):
        """Convert native viewport motion into app scroll; re-center on grid."""
        lh = view.line_height()
        if lh <= 0:
            return
        pad = self._alt_scroll_pad
        home_y = pad * lh
        self._alt_home_y = home_y
        cur_y = view.viewport_position()[1]
        delta_lines = (cur_y - home_y) / lh
        if abs(delta_lines) >= 0.6:
            n = int(round(delta_lines))
            if n != 0:
                # Scroll down in the view → user wants later content → app "down"
                action = "down" if n > 0 else "up"
                logger.info("alt-wheel capture: vpΔ=%.1f → %s x%d", delta_lines, action, abs(n))
                terminal.scroll(action, abs(n))
        # Always re-center so continuous trackpad scrolling keeps working.
        x, _ = view.viewport_position()
        if abs(view.viewport_position()[1] - home_y) > 0.5:
            view.set_viewport_position((x, home_y), False)
            sublime.set_timeout(
                lambda: view.set_viewport_position((x, home_y), False), 0)

    def _leave_alt(self, edit, view, terminal):
        snap = self._alt_snapshot
        self._alt_snapshot = None
        self._erase_all_regions(view)
        view.replace(edit, sublime.Region(0, view.size()), "")
        if snap and "scroll_past_end" in snap:
            spe = snap["scroll_past_end"]
            if spe is None:
                view.settings().erase("scroll_past_end")
            else:
                view.settings().set("scroll_past_end", spe)
        if not snap:
            terminal.offset = 0
        else:
            view.insert(edit, 0, snap["text"])
            terminal.offset = snap["offset"]
            max_key = 0
            for key, (regions, scope) in snap["regions"].items():
                if regions:
                    view.add_regions(key, regions, scope)
                    self.region_scopes[key] = scope
                    try:
                        max_key = max(max_key, int(key.split("#")[1]))
                    except (IndexError, ValueError):
                        pass
            self.colored_lines = {ln: list(ks) for ln, ks in snap["colored_lines"].items()}
            view.settings().set("terminus.highlight_counter", max_key)
        terminal.screen.dirty.update(range(terminal.screen.lines))

    def trim_trailing_spaces(self, edit, terminal):
        view = self.view
        screen = terminal.screen
        cursor = screen.cursor
        cursor_row = terminal.offset + screen.cursor.y
        lastrow = view.rowcol(view.size())[0]
        row = lastrow
        while row > cursor_row:
            line_region = view.line(view.text_point(row, 0))
            text = view.substr(line_region)
            if len(text.strip()) == 0 and \
                    (row not in self.colored_lines or len(self.colored_lines[row]) == 0):
                region = view.line(view.text_point(row, 0))
                view.erase(edit, sublime.Region(region.begin() - 1, region.end()))
                row = row - 1
            else:
                break
        if row == cursor_row:
            line_region = view.line(view.text_point(row, 0))
            text = view.substr(line_region)
            trailing_region = sublime.Region(
                line_region.begin() + rev_wcwidth(text, cursor.x) + 1,
                line_region.end())
            if not trailing_region.empty() and len(view.substr(trailing_region).strip()) == 0:
                view.erase(edit, trailing_region)

    def trim_history(self, edit, terminal):
        """
        If number of lines in view > n, remove n / 10 lines from the top
        """
        view = self.view

        screen = terminal.screen
        lastrow = view.rowcol(view.size())[0]
        n = self.scrollback_history_size
        if lastrow + 1 > n:
            m = max(lastrow + 1 - n, math.ceil(n / 10))
            logger.debug("removing {} lines from the top".format(m))
            for line in range(m):
                self.decolorize_line(line)
            # shift colored_lines indexes
            self.colored_lines = {k - m: v for (k, v) in self.colored_lines.items()}
            top_region = sublime.Region(0, view.line(view.text_point(m - 1, 0)).end() + 1)
            view.erase(edit, top_region)
            terminal.offset -= m
            lastrow -= m

            # delete outdated images
            terminal.clean_images()

        if lastrow > terminal.offset + screen.lines:
            tail_region = sublime.Region(
                view.text_point(terminal.offset + screen.lines, 0),
                view.size()
            )
            for line in view.lines(tail_region):
                self.decolorize_line(view.rowcol(line.begin())[0])
            view.erase(edit, tail_region)


class ClaudeTerminalShowCursorCommand(sublime_plugin.TextCommand, TerminusViewMixin):

    def run(self, edit, focus=True, scroll=True):
        view = self.view
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return

        if focus:
            self.focus_cursor(edit, terminal)
        if scroll:
            sublime.set_timeout(lambda: self.scroll_to_cursor(terminal))

    def focus_cursor(self, edit, terminal):
        view = self.view

        sel = view.sel()
        sel.clear()

        screen = terminal.screen
        if screen.cursor.hidden:
            return

        cursor = screen.cursor
        offset = terminal.offset

        if len(view.sel()) > 0 and view.sel()[0].empty():
            row, col = view.rowcol(view.sel()[0].end())
            if row == offset + cursor.y and col == cursor.x:
                return

        # make sure the view has enough lines
        self.ensure_position(edit, cursor.y + offset)

        line_region = view.line(view.text_point(cursor.y + offset, 0))
        text = view.substr(line_region)
        col = rev_wcwidth(text, cursor.x) + 1

        self.ensure_position(edit, cursor.y + offset, col)
        pt = view.text_point(cursor.y + offset, col)

        sel.add(sublime.Region(pt, pt))

    def scroll_to_cursor(self, terminal):
        view = self.view
        last_y = view.text_to_layout(view.size())[1]
        viewport_y = last_y - view.viewport_extent()[1] + view.line_height()
        offset_y = view.text_to_layout(view.text_point(terminal.offset, 0))[1]
        y = max(offset_y, viewport_y)
        view.settings().set("claude_terminal_view.viewport_y", y)
        view.set_viewport_position((0, y), False)


class ClaudeTerminalCleanupCommand(sublime_plugin.TextCommand):
    def run(self, edit, by_user=False):
        logger.debug("cleanup")
        view = self.view
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return

        # Adopted terminal being handed back to its pty owner: stop quietly,
        # never kill the borrowed pty or close the view.
        if getattr(terminal, "_adopted_release", False):
            return

        if view.settings().get("claude_terminal_view.finished"):
            return

        # to avoid double cancel
        view.settings().set("claude_terminal_view.finished", True)

        view.run_command("claude_terminal_render")

        # process might became orphan, make sure the process is terminated
        terminal.kill()
        process = terminal.process

        if terminal.auto_close is True or terminal.auto_close == "always" or \
                (process.exitstatus == 0 and terminal.auto_close == "on_success"):
            view.run_command("claude_terminal_close")

        view.run_command("claude_terminal_trim_trailing_lines")

        if by_user:
            view.run_command("append", {"characters": "[Cancelled]"})

        elif terminal.timeit:
            if process.exitstatus == 0:
                view.run_command(
                    "append",
                    {"characters": "[Finished in {:0.2f}s]".format(
                        time.time() - terminal.start_time)})
            else:
                view.run_command(
                    "append",
                    {"characters": "[Finished in {:0.2f}s with exit code {}]".format(
                        time.time() - terminal.start_time, process.exitstatus)})
        elif process.exitstatus is not None:
            view.run_command(
                "append",
                {"characters": "process is terminated with return code {}.".format(
                    process.exitstatus)})

        view.sel().clear()

        if not terminal.show_in_panel and view.settings().get("result_file_regex"):
            # if it is a tab based build, we will to refocus to enable next_result
            window = view.window()
            if window:
                active_view = window.active_view()
                view.window().focus_view(view)
                if active_view:
                    view.window().focus_view(active_view)
