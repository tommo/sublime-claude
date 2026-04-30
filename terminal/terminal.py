import sublime

import os
import re
import shutil
import textwrap
import time
import base64
import logging
import tempfile
import threading
from queue import Queue, Empty

from .ptty import TerminalPtyProcess, TerminalScreen, TerminalStream
from .utils import responsive, intermission
from .view import get_panel_window, view_size
from .key import get_key_code
from .image import get_image_info, image_resize

_KNOWN_SHELLS = {"bash", "zsh", "fish", "sh", "ksh", "ksh93", "mksh", "tcsh", "csh", "dash"}

_STRIP_ANSI = re.compile(
    r'\x1b\[[0-9;?]*[ -/]*[@-~]'          # CSI: covers \x1b[?25l, \x1b[1;32m, etc.
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)' # OSC: BEL or ST terminator
    r'|\x1b[PX^_][^\x1b]*\x1b\\'          # DCS/SOS/PM/APC
    r'|\x1b[^[\]PX^_]'                    # other 2-char ESC sequences
    r'|\r'
    r'|[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f]'  # control chars (keep \t \n \x1b)
)
_STRIP_BS = re.compile(r'[^\n]\x08')       # char + backspace = erase

_PROMPT_RE = re.compile(r'[$%#>❯]\s*$', re.MULTILINE)
_OSC133_PROMPT = '\x1b]133;A\x07'
_OSC7_RE = re.compile(r'\x1b\]7;file://[^\x07/]*(\/[^\x07]*)\x07')


def _setup_shell_integration(cmd, env):
    """
    Inject OSC 133;A shell integration so the reader thread can detect prompts
    reliably regardless of prompt appearance. Returns (cmd, env, tmpdir_or_None).
    tmpdir must be deleted by caller when terminal closes.
    """
    shell = os.path.basename(cmd[0]) if cmd else ""
    home = env.get("HOME", os.path.expanduser("~"))

    if shell == "zsh":
        tmpdir = tempfile.mkdtemp(prefix="ct_zdotdir_")
        orig_zdotdir = env.get("ZDOTDIR", home)
        zshrc = textwrap.dedent("""\
            ZDOTDIR={orig}
            [ -f {orig}/.zshrc ] && source {orig}/.zshrc
            _ct_precmd() {{
              printf '\\e]133;A\\a'
              printf '\\e]7;file://%s%s\\a' "$(hostname -s 2>/dev/null || echo localhost)" "$PWD"
            }}
            autoload -Uz add-zsh-hook 2>/dev/null \\
              && add-zsh-hook precmd _ct_precmd \\
              || precmd_functions+=(_ct_precmd)
            PS2=$'\\e]133;A\\a> '
        """).format(orig=orig_zdotdir)
        with open(os.path.join(tmpdir, ".zshrc"), "w") as f:
            f.write(zshrc)
        env = dict(env, ZDOTDIR=tmpdir)
        return cmd, env, tmpdir

    elif shell == "bash":
        tmpdir = tempfile.mkdtemp(prefix="ct_bash_")
        rc = os.path.join(tmpdir, ".bashrc")
        bashrc = textwrap.dedent("""\
            [ -f {home}/.bash_profile ] && source {home}/.bash_profile \\
              || [ -f {home}/.bashrc ] && source {home}/.bashrc
            _ct_prompt_cmd() {{
              printf '\\e]133;A\\a'
              printf '\\e]7;file://%s%s\\a' "${{HOSTNAME:-localhost}}" "$PWD"
            }}
            PROMPT_COMMAND="${{PROMPT_COMMAND:+$PROMPT_COMMAND; }}_ct_prompt_cmd"
            PS2=$'\\e]133;A\\a> '
        """).format(home=home)
        with open(rc, "w") as f:
            f.write(bashrc)
        new_cmd = [cmd[0], "--rcfile", rc, "-i"] + \
                  [a for a in cmd[1:] if a not in ("-i", "-l", "--login")]
        return new_cmd, env, tmpdir

    elif shell == "fish":
        init = (
            "functions --copy fish_prompt __ct_orig_fish_prompt 2>/dev/null; "
            "function fish_prompt; "
            "  printf '\\e]133;A\\a'; "
            "  printf '\\e]7;file://%s%s\\a' (hostname -s 2>/dev/null; or echo localhost) $PWD; "
            "  __ct_orig_fish_prompt; "
            "end"
        )
        new_cmd = [cmd[0], "--init-command", init] + cmd[1:]
        return new_cmd, env, None

    return cmd, env, None


IMAGE = """
<style>
body {{
    margin: 1px;
}}
</style>
<img src="data:image/{what};base64,{data}" width="{width}" height="{height}"/>
"""

logger = logging.getLogger('Terminus')


class Terminal:
    _terminals = {}
    _detached_terminals = []
    _next_index = 1

    def __init__(self, view=None):
        self.view = view
        self._cached_cursor = [0, 0]
        self._size = sublime.load_settings('Terminus.sublime-settings').get('size', (None, None))
        self._cached_cursor_is_hidden = [True]
        self.image_count = 0
        self.images = {}
        self._strings = Queue()
        self._pending_to_send_string = [False]
        self._pending_to_clear_scrollback = [False]
        self._pending_to_reset = [None]
        self.lock = threading.Lock()
        self._user_history = []   # [{"time": float, "line": str}]
        self._input_buf = ""      # chars typed since last Enter
        self._input_navigating = False  # True after up/down (shell history mode)

    # ─── User input tracking ───────────────────────────────────────────────

    def _track_char(self, text: str):
        self._input_buf += text
        self._input_navigating = False

    def _track_key(self, key: str, ctrl: bool = False, **_):
        if key == "enter":
            self._flush_input()
        elif key == "backspace":
            self._input_buf = self._input_buf[:-1]
        elif key in ("up", "down"):
            self._input_buf = ""
            self._input_navigating = True
        elif ctrl and key == "c":
            self._record_input("^C")
            self._input_buf = ""
            self._input_navigating = False
        elif ctrl and key == "d":
            self._record_input("^D")
            self._input_buf = ""
        elif ctrl and key in ("u", "k"):
            self._input_buf = ""
        elif ctrl and key == "w":
            parts = self._input_buf.rsplit(None, 1)
            self._input_buf = parts[0] + " " if len(parts) > 1 else ""

    def _flush_input(self):
        line = self._input_buf.strip()
        if not line and self._input_navigating:
            # User pressed up/down to select shell history then Enter —
            # read the echoed command from the screen cursor line.
            try:
                row = self.screen.cursor.y
                row_buf = self.screen.buffer.get(row, {})
                raw = "".join(row_buf[c].data for c in sorted(row_buf.keys())).strip()
                m = re.search(r'[$%#>❯]\s+(.*)', raw)
                line = m.group(1).strip() if m else raw
            except Exception:
                line = ""
        if line:
            self._record_input(line)
        self._input_buf = ""
        self._input_navigating = False

    def _record_input(self, line: str):
        self._user_history.append({"time": time.time(), "line": line})
        if len(self._user_history) > 100:
            self._user_history = self._user_history[-100:]

    def _track_paste(self, text: str):
        lines = [l for l in text.splitlines() if l.strip()]
        if not lines:
            return
        self._input_buf += lines[0]
        for extra in lines[1:]:
            self._flush_input()
            self._input_buf = extra
        if len(lines) > 1:
            self._flush_input()

    @classmethod
    def from_id(cls, vid):
        if vid not in cls._terminals:
            return None
        return cls._terminals[vid]

    @classmethod
    def from_tag(cls, tag, current_window_only=True):
        # restrict to only current window
        for terminal in cls._terminals.values():
            if terminal.tag == tag:
                if current_window_only:
                    active_window = sublime.active_window()
                    if terminal.window and active_window:
                        if terminal.window == active_window:
                            return terminal
                else:
                    return terminal
        return None

    @classmethod
    def cull_terminals(cls):
        terminals_to_kill = []
        for terminal in cls._terminals.values():
            if not terminal.is_hosted():
                terminals_to_kill.append(terminal)

        for terminal in terminals_to_kill:
            terminal.kill()

    @property
    def window(self):
        if self.detached:
            return None
        if self.show_in_panel:
            return get_panel_window(self.view)
        else:
            return self.view.window()

    def attach_view(self, view, offset=None):
        with self.lock:
            self.view = view
            self.detached = False
            Terminal._terminals[view.id()] = self
            if self in Terminal._detached_terminals:
                Terminal._detached_terminals.remove(self)
            # allow screen to be rerendered
            self.screen.dirty.update(range(self.screen.lines))
            self.set_offset(offset)

    def detach_view(self):
        with self.lock:
            self.detached = True
            Terminal._detached_terminals.append(self)
            if self.view.id() in Terminal._terminals:
                del Terminal._terminals[self.view.id()]
            self.view = None

    @responsive(period=1, default=True)
    def is_hosted(self):
        if self.detached:
            # irrelevant if terminal is detached
            return True
        return self.window is not None

    def _need_to_render(self):
        flag = False
        if self.screen.dirty:
            flag = True
        elif self.screen.cursor.x != self._cached_cursor[0] or \
                self.screen.cursor.y != self._cached_cursor[1]:
            flag = True
        elif self.screen.cursor.hidden != self._cached_cursor_is_hidden[0]:
            flag = True

        if flag:
            self._cached_cursor[0] = self.screen.cursor.x
            self._cached_cursor[1] = self.screen.cursor.y
            self._cached_cursor_is_hidden[0] = self.screen.cursor.hidden
        return flag

    def _start_rendering(self):
        data = [""]
        done = [False]

        @responsive(period=1, default=False)
        def was_resized():
            size = view_size(self.view, force=self._size)
            return self.screen.lines != size[0] or self.screen.columns != size[1]

        def reader():
            while True:
                try:
                    temp = self.process.read(1024)
                except EOFError:
                    break

                with self.lock:
                    data[0] += temp

                    if self._use_osc133 and _OSC133_PROMPT in temp:
                        self._update_title(busy=False)

                    m = _OSC7_RE.search(temp)
                    if m:
                        cwd = m.group(1)
                        if self.view:
                            args = self.view.settings().get("claude_terminal.args", {})
                            args["cwd"] = cwd
                            self.view.settings().set("claude_terminal.args", args)

                    if getattr(self, '_capturing', False):
                        self._capture_raw += temp
                        self._capture_buf.append(temp)
                        self._capture_last_data_time = time.time()
                        if self._use_osc133:
                            if _OSC133_PROMPT in self._capture_raw:
                                self._capture_event.set()
                        else:
                            s = _STRIP_ANSI.sub('', self._capture_raw)
                            lines = [l for l in s.splitlines() if l.strip()]
                            if lines and _PROMPT_RE.search(lines[-1]):
                                self._capture_event.set()

                    if done[0] or not self.is_hosted():
                        logger.debug("reader breaks")
                        break

            done[0] = True

        threading.Thread(target=reader).start()

        def renderer():

            def feed_data():
                if len(data[0]) > 0:
                    logger.debug("receieved: {}".format(data[0]))
                    self.stream.feed(data[0])
                    data[0] = ""

            while True:
                with intermission(period=0.03), self.lock:
                    feed_data()
                    if not self.detached:
                        if was_resized():
                            self.handle_resize()
                            self.view.run_command("claude_terminal_show_cursor")

                        if self._need_to_render():
                            self.view.run_command("claude_terminal_render")
                            self.screen.dirty.clear()

                    if done[0] or not self.is_hosted():
                        logger.debug("renderer breaks")
                        break

            feed_data()
            done[0] = True

            def _cleanup():
                if self.view:
                    self.view.run_command("claude_terminal_cleanup")

            sublime.set_timeout(_cleanup)

        threading.Thread(target=renderer).start()

    def set_offset(self, offset=None):
        if offset is not None:
            self.offset = offset
        else:
            if self.view and self.view.size() > 0:
                view = self.view
                self.offset = view.rowcol(view.size())[0] + 1
            else:
                self.offset = 0
        logger.debug("activating with offset %s", self.offset)

    def start(
            self, cmd, cwd=None, env=None, default_title=None, title=None,
            show_in_panel=None, panel_name=None, tag=None, auto_close=True, cancellable=False,
            timeit=False):

        view = self.view
        if view:
            self.detached = False
            Terminal._terminals[view.id()] = self
        else:
            Terminal._detached_terminals.append(self)
            self.detached = True

        self.show_in_panel = show_in_panel
        self.panel_name = panel_name
        self.tag = tag
        self.auto_close = auto_close
        self.cancellable = cancellable
        self.timeit = timeit
        if timeit:
            self.start_time = time.time()
        self.default_title = default_title
        self.title = title

        if view:
            self.set_offset()

        size = view_size(
            view or sublime.active_window().active_view(), default=(40, 80), force=self._size)
        logger.debug("view size: {}".format(str(size)))
        _env = os.environ.copy()
        _env["TERM"] = "xterm-256color"
        _env.update(env)
        saved_index = view.settings().get("claude_terminal.index") if view else None
        if saved_index:
            self.index = saved_index
            if saved_index >= Terminal._next_index:
                Terminal._next_index = saved_index + 1
        else:
            self.index = Terminal._next_index
            Terminal._next_index += 1

        _orig_cmd = list(cmd)
        cmd, _env, self._integration_dir = _setup_shell_integration(cmd, _env)
        self._use_osc133 = self._integration_dir is not None or \
                           os.path.basename(cmd[0]) == "fish"
        self._is_shell = os.path.basename(_orig_cmd[0]) in _KNOWN_SHELLS if _orig_cmd else False
        self.process = TerminalPtyProcess.spawn(cmd, cwd=cwd, env=_env, dimensions=size)
        self._update_title(busy=False)

        if view:
            view.settings().set("claude_terminal.args", {
                "cmd": _orig_cmd,
                "cwd": cwd or "",
                "tag": tag or "",
                "env": env or {},
            })
            view.settings().set("claude_terminal.index", self.index)
            view.settings().set("claude_terminal.reactivable", True)
        self.screen = TerminalScreen(
            size[1], size[0], process=self.process, history=10000,
            clear_callback=self.clear_callback, reset_callback=self.reset_callback)
        self.stream = TerminalStream(self.screen)

        self._start_rendering()

    def is_alive(self):
        return self.process is not None and self.process.isalive()

    def close(self):
        self.kill()

    def kill(self):
        logger.debug("kill")
        if self.process:
            self.process.terminate()
        if self.view:
            vid = self.view.id()
            if vid in self._terminals:
                del self._terminals[vid]
        self._cleanup_integration()

    def _cleanup_integration(self):
        d = getattr(self, "_integration_dir", None)
        if d:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
            self._integration_dir = None

    def _update_title(self, busy):
        idx = getattr(self, 'index', '?')
        prefix = "▶ " if busy else "▫ "
        name = "#{} {}".format(idx, self.default_title) if self.default_title else "#{}".format(idx)
        view = self.view
        if view:
            sublime.set_timeout(lambda: view.set_name(prefix + name) if view.is_valid() else None)

    def start_capture(self):
        self._capture_buf = []
        self._capture_raw = ""
        self._capture_event = threading.Event()
        self._capture_last_data_time = None  # set on first byte; quiescence only counts from there
        self._capturing = True
        self._update_title(busy=True)
        threading.Thread(target=self._capture_pgid_watcher, daemon=True).start()

    def _capture_pgid_watcher(self):
        is_shell = getattr(self, '_is_shell', False)
        try:
            fd = self.process.fd
            shell_pgrp = os.getpgid(self.process.pid)
        except Exception:
            return
        idle_streak = 0
        while getattr(self, '_capturing', False):
            time.sleep(0.05)
            last_data = getattr(self, '_capture_last_data_time', None)
            if not is_shell:
                # SSH / direct programs: wait for first byte, then 300ms quiescence.
                # Initializing from capture-start time would fire before SSH responds.
                if last_data is None:
                    continue
                if time.time() - last_data >= 0.3:
                    if getattr(self, '_capturing', False):
                        self._capture_event.set()
                    return
                continue
            try:
                fg_pgrp = os.tcgetpgrp(fd)
            except Exception:
                return
            if fg_pgrp == shell_pgrp:
                idle_streak += 1
                if idle_streak >= 2:  # 100ms stable idle → local shell at prompt
                    if getattr(self, '_capturing', False):
                        self._capture_event.set()
                    return
            else:
                idle_streak = 0
                # Local subprocess (REPL, ssh, etc.) — quiescence after first data
                if last_data is not None and time.time() - last_data >= 0.3:
                    if getattr(self, '_capturing', False):
                        self._capture_event.set()
                    return

    def stop_capture(self):
        self._capturing = False
        timed_out = not self._capture_event.is_set()
        raw = "".join(self._capture_buf)
        output = _STRIP_ANSI.sub('', raw)
        # collapse backspace sequences until none remain
        while '\x08' in output:
            output = _STRIP_BS.sub('', output)
            output = output.replace('\x08', '')
        self._update_title(busy=False)
        return output, timed_out

    @classmethod
    def list_all(cls):
        return list(cls._terminals.values())

    def handle_resize(self):
        size = view_size(self.view, force=self._size)
        logger.debug("handle resize {} {} -> {} {}".format(
            self.screen.lines, self.screen.columns, size[0], size[1]))
        try:
            # pywinpty will rasie an runtime error
            self.process.setwinsize(*size)
            self.screen.resize(*size)
        except RuntimeError:
            pass

    def clear_callback(self):
        self._pending_to_clear_scrollback[0] = True

    def reset_callback(self):
        if self._pending_to_reset[0] is None:
            self._pending_to_reset[0] = False
        else:
            self._pending_to_reset[0] = True

    def send_key(self, *args, **kwargs):
        kwargs["application_mode"] = self.application_mode_enabled()
        kwargs["new_line_mode"] = self.new_line_mode_enabled()
        self.send_string(get_key_code(*args, **kwargs), normalized=False)

    def send_string(self, string, normalized=True):
        if normalized:
            # normalize CR and CRLF to CR (or CRLF if LNM)
            string = string.replace("\r\n", "\n")
            if self.new_line_mode_enabled():
                string = string.replace("\n", "\r\n")
            else:
                string = string.replace("\n", "\r")

        no_queue = not self._pending_to_send_string[0]
        if no_queue and len(string) <= 512:
            self.process.write(string)
        else:
            for i in range(0, len(string), 512):
                self._strings.put(string[i:i+512])
            if no_queue:
                self._pending_to_send_string[0] = True
                threading.Thread(target=self.process_send_string).start()

    def process_send_string(self):
        while True:
            try:
                string = self._strings.get(False)
                logger.debug("sent: {}".format(string[0:64] if len(string) > 64 else string))
                self.process.write(string)
            except Empty:
                self._pending_to_send_string[0] = False
                return
            else:
                time.sleep(0.1)

    def bracketed_paste_mode_enabled(self):
        return (2004 << 5) in self.screen.mode

    def new_line_mode_enabled(self):
        return (20 << 5) in self.screen.mode

    def application_mode_enabled(self):
        return (1 << 5) in self.screen.mode

    def find_image(self, pt):
        view = self.view
        for pid in self.images:
            region = view.query_phantom(pid)[0]
            if region.end() == pt:
                return pid
        return None

    def show_image(self, data, args, cr=None):
        view = self.view

        if "inline" not in args or not args["inline"]:
            return

        cursor = self.screen.cursor
        pt = view.text_point(self.offset + cursor.y, cursor.x)

        databytes = base64.decodebytes(data.encode())

        image_info = get_image_info(databytes)
        if not image_info:
            logger.error("cannot get image info")
            return

        what, width, height = image_info

        _, image_path = tempfile.mkstemp(suffix="." + what)
        with open(image_path, "wb") as f:
            f.write(databytes)

        width, height = image_resize(
            width,
            height,
            args["width"] if "width" in args else None,
            args["height"] if "height" in args else None,
            view.em_width(),
            view.viewport_extent()[0] - 3 * view.em_width(),
            args["preserveAspectRatio"] if "preserveAspectRatio" in args else 1
        )

        if self.find_image(pt):
            self.view.run_command("claude_terminal_insert", {"point": pt, "character": " "})
            pt += 1

        self.image_count += 1
        p = view.add_phantom(
            "claude_terminal_image#{}".format(self.image_count),
            sublime.Region(pt, pt),
            IMAGE.format(
                what=what,
                data=data,
                width=width,
                height=height,
                count=self.image_count),
            sublime.LAYOUT_INLINE,
        )
        self.images[p] = image_path

        if cr:
            self.screen.index()

    def clean_images(self):
        view = self.view
        for pid in list(self.images.keys()):
            region = view.query_phantom(pid)[0]
            if region.empty() and region.begin() == 0:
                view.erase_phantom_by_id(pid)
                if pid in self.images:
                    try:
                        os.remove(self.images[pid])
                    except Exception:
                        pass
                    del self.images[pid]

    def __del__(self):
        self._cleanup_integration()
        # make sure the process is terminated
        self.process.terminate(force=True)

        # remove images
        for image_path in list(self.images.values()):
            try:
                os.remove(image_path)
            except Exception:
                pass

        if self.process.isalive():
            logger.debug("process becomes orphaned")
        else:
            logger.debug("process is terminated")
