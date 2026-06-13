"""PtyEngineSession — run the real interactive `claude` CLI in a hidden PTY and
render natively by tailing the session transcript.

Why: from 2026-06-15 the SDK-bridge path (`claude -p`) bills against a small
Agent-SDK credit, while interactive `claude` draws on the generous subscription
bucket. This engine keeps sublime-claude's native UI but swaps the transport:

  • output  ← transcript .jsonl  (cc_transcript.Tailer → existing _on_msg_* dispatch)
  • input   → keystrokes written into the hidden PTY
  • turn state ← the PTY's rendered screen ("esc to interrupt" == working)

It subclasses Session to reuse OutputView, input mode, status bar, the whole
_on_notification/_on_msg_* renderer, and lifecycle hooks. Only the transport is
overridden; `self.client` stays None throughout.
"""
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid as uuidlib

import sublime

from . import session as session_mod
from . import cc_transcript


class PtyEngineSession(session_mod.Session):
    # Transcript-derived signals (pending tool_use ids, recent-event gap) are
    # the PRIMARY triggers — but some sessions emit no `system/turn_duration`
    # records and have multi-minute thinking gaps between events, which would
    # otherwise let the quiet timer falsely finalize while claude is still
    # working. So we add a screen-based busy GUARD: never finalize while the
    # PTY screen indicates claude is busy. The text pattern is version-fragile
    # (intentionally documented as a recalibration target); users can override
    # via the `pty_busy_marker` setting.
    # A numbered dialog option line. Tolerates a selection marker (❯ › * •)
    # before the number and collapsed spacing after the dot ("❯1.Yes", "2. No").
    DIALOG_OPTION_RE = re.compile(r"^[\s❯>›*•]*\d+\.\s*\S")
    DIALOG_MIN_OPTIONS = 3            # smallest real menu we'd care about
    QUIET_FINALIZE_AFTER_S = 8.0      # idle gap after which to consider finalizing
                                      # (guarded by screen-busy check below)
    # Screen substrings (whitespace-stripped, lowercased) that indicate the
    # TUI is in a state where the user is blocked / claude isn't truly idle.
    # Covers both "claude is working" and "claude is awaiting a dialog answer"
    # (permission prompts, AskUserQuestion, bypass-confirm, etc.). All v2.1.x.
    BUSY_MARKERS_DEFAULT = ("esctointerrupt", "esctocancel")

    def __init__(self, window):
        super().__init__(window, backend="claude")
        self._pty = None
        self._screen = None
        self._stream = None
        self._screen_lock = threading.Lock()
        # Hot-reveal: a single reader (self._read_loop) routes pty bytes to the
        # active sink — the engine stream (native view) or a revealed Terminal.
        self._sink_lock = threading.Lock()
        self._active_terminal = None       # Terminal when revealed, else None
        self._pty_exited = False           # _on_pty_exit idempotency guard
        self.terminal_revealed = False
        self._reveal_view = None
        self._reveal_terminal = None
        self._reveal_tag = None
        self._reveal_poll_active = False
        self._reveal_jsonl_pos = 0          # transcript size at reveal (replay delta on return)
        self._reader_thread = None
        self._tailer = None
        self._watch_active = False
        self._turn_start = 0.0
        self._turn_events = 0      # transcript events seen since this turn began
        self._last_event_t = 0.0   # wall time of last transcript event
        self._last_pty_byte_t = 0.0  # wall time of last byte read from the PTY
        self._pending_tools = set()  # tool_use ids awaiting their tool_result
        self._ask_questions = None  # in-flight AskUserQuestion (questions list)
        self._answer_queue = []     # remaining option indices to inject
        self._ask_pending = None    # tool_use seen, watching for the dialog
        self._ask_resolved_id = None  # tool_use_id of an auto-resolved AskUserQuestion
        self._trust_done = False     # folder-trust dialog auto-confirmed?
        self._input_reveal_done = False  # auto-revealed for an input-required dialog?
        self.sleep_disabled = True  # POC: never auto-sleep a PTY session

    # PTY sessions have no bridge to put to sleep; the discriminator the base
    # class uses (client is None) is always true here, so pin this False.
    @property
    def is_sleeping(self):
        return False

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self, resume_session_at=None):
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        claude_bin = self._resolve_claude(settings)
        if not claude_bin:
            sublime.error_message("Claude CLI not found. Set 'claude_bin' in settings or add it to PATH.")
            return

        cwd = os.path.realpath(self._cwd())
        sid = str(uuidlib.uuid4())
        self.session_id = sid

        env = os.environ.copy()
        # Match a real terminal so the hidden claude TUI renders consistently
        # (truecolor, unbuffered, known size) — better screen-state detection.
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["FORCE_COLOR"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["COLUMNS"] = "120"
        env["LINES"] = "40"
        try:
            env.update(self._load_env(settings) or {})
        except Exception as e:
            print("[ptyengine] _load_env failed: {}".format(e))

        # 'acceptEdits' auto-allows Read/Edit/Write but still prompts for Bash
        # and other potentially-dangerous tools. Prompts in the hidden PTY
        # would hang the session — so this default presumes Phase 5 (native
        # permission prompts) is in place, OR the user accepts that Bash etc.
        # will need the reveal-screen escape hatch / interrupt.
        perm = settings.get("pty_permission_mode", "acceptEdits")
        argv = [claude_bin, "--session-id", sid, "--permission-mode", perm]
        mcp_cfg = self._build_mcp_config(settings)
        if mcp_cfg:
            argv += ["--mcp-config", mcp_cfg]
        # NOTE: sublime-claude's default model IDs are *virtual* (e.g.
        # "claude-opus-4-6[1m]@400k") — invalid for the real CLI's --model.
        # For the POC we don't pass --model; claude uses its configured default.
        # A later enhancement can map virtual IDs → clean CLI model names.

        from .terminal.ptty import TerminalPtyProcess, TerminalScreen, TerminalStream
        cols, rows = 120, 40
        try:
            self._pty = TerminalPtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))
        except Exception as e:
            sublime.error_message("Failed to launch claude: {}".format(e))
            return
        self._screen = TerminalScreen(
            cols, rows, process=self._pty, history=2000,
            clear_callback=lambda: None, reset_callback=lambda: None)
        self._stream = TerminalStream(self._screen)

        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        path = cc_transcript.transcript_path(cwd, sid)
        self._tailer = cc_transcript.Tailer(path, self._on_transcript_event)
        self._tailer.start()

        self.initialized = True
        self.output.show()
        self._status("CLI session")
        self._watch_active = True
        sublime.set_timeout(self._watch_ready, 500)
        if settings.get("pty_auto_trust", True):
            sublime.set_timeout(self._auto_trust_check, 600)
        self._input_mode_entered = False
        self._enter_input_with_draft()
        print("[ptyengine] started sid={} cwd={} argv={}".format(sid, cwd, argv))

    def stop(self):
        self._watch_active = False
        if self.terminal_revealed:
            self._teardown_reveal(hand_back=False)
        if self._tailer:
            self._tailer.stop()
            self._tailer = None
        self._kill_pty()
        try:
            super().stop()  # client is None → no shutdown RPC; does loop/state cleanup
        except Exception as e:
            print("[ptyengine] super().stop error: {}".format(e))

    def sleep(self, force=False):
        return False  # POC: no sleep/wake for PTY sessions

    def _kill_pty(self):
        p = self._pty
        self._pty = None
        if not p:
            return
        try:
            p.terminate(force=True)
        except TypeError:
            try:
                p.terminate()
            except Exception:
                pass
        except Exception:
            pass

    # ── transport: input ──────────────────────────────────────────────────────
    def query(self, prompt, display_prompt=None, silent=False):
        if self.terminal_revealed:
            sublime.status_message("Session is revealed as a terminal — type in the TUI")
            return
        if not self._pty or not self.initialized:
            sublime.error_message("CLI session not ready")
            return
        if self.working:
            self.queue_prompt(prompt)
            return

        self.working = True
        self._turn_start = time.time()
        self._turn_events = 0
        self._last_event_t = time.time()
        self._input_reveal_done = False
        self._pending_tools.clear()
        self.query_count += 1
        self.draft_prompt = ""
        self._input_mode_entered = False

        # Slash commands bypass context — they must be parsed by the TUI as a
        # command, which only happens for plain (non-pasted) single-line input.
        # Mixing context in turns the whole thing into a pasted message and the
        # command is silently lost.
        stripped = prompt.strip()
        is_slash = stripped.startswith("/") and "\n" not in stripped
        if is_slash:
            full_prompt, images, context_names = stripped, [], []
        else:
            full_prompt, images = self._build_prompt_with_context(prompt)
            _, context_names = self.context.take()
        # The interactive CLI can't accept image *data* via typed text, but its
        # paste handler auto-detects image *paths* in pasted text and inlines
        # the image as a message attachment (just like drag-drop) — no Read
        # needed. So we just write each image to a temp file and append its
        # bare path; the TUI inlines it on paste.
        if images:
            paths = self._materialize_images(images)
            if paths:
                full_prompt += "\n\n" + "\n".join(paths)
        ui_prompt = display_prompt if display_prompt else prompt

        if not silent:
            self.output.show()
            if not self.name:
                self._set_name(ui_prompt[:30].strip() + ("..." if len(ui_prompt) > 30 else ""))
            self.output.prompt(ui_prompt, context_names)
        self.output._update_title()
        self._animate()

        # Write the text, then submit Enter as a SEPARATE, verified write tick.
        # A CR appended to the text (or lumped into the bracketed-paste close)
        # frequently fails to register in Ink, leaving the message sitting in the
        # input box — the root cause of "the turn never starts". So we send Enter
        # on its own and re-send while the box still holds un-submitted text.
        try:
            if is_slash:
                # Slash commands must be plain (non-pasted) single-line input —
                # Ink only parses them as a command from typed, unpasted text.
                self._pty.write(full_prompt)
            else:
                # Bracketed paste keeps embedded newlines / multibyte intact.
                self._pty.write("\x1b[200~" + full_prompt + "\x1b[201~")
            sublime.set_timeout(lambda fp=full_prompt: self._submit_enter(fp, 0), 160)
        except Exception as e:
            self.working = False
            self.output.text("\n\n*Failed to send to CLI: {}*\n".format(e))

    def _submit_enter(self, full_prompt, attempts=0):
        if not self._pty:
            return
        try:
            self._pty.write("\r")
        except Exception:
            return
        # Verify + retry: if OUR prompt is still sitting in the input box, the
        # Enter didn't land (TUI timing) — send another, up to a few times. We
        # match the box against our prompt so claude's ghost/autocomplete
        # suggestion (shown in the idle box) never triggers a spurious Enter.
        if attempts < 4:
            def _retry():
                if not (self._pty and self.working):
                    return
                box = self._input_box_text()
                if box and self._box_holds_prompt(box, full_prompt):
                    self._submit_enter(full_prompt, attempts + 1)
            sublime.set_timeout(_retry, 250)

    def _input_box_text(self):
        """Text in the TUI input box (the prompt line between the last two
        horizontal rules), or '' if empty. NOTE: claude shows a dim ghost
        suggestion here when idle — callers must match it, not just truthiness."""
        try:
            rows = self._screen_text().splitlines()
            rule_idxs = [i for i, ln in enumerate(rows)
                         if len(ln.strip()) >= 12 and set(ln.strip()) <= set("─-—")]
            if len(rule_idxs) < 2:
                return ""
            for ln in rows[rule_idxs[-2] + 1:rule_idxs[-1]]:
                s = ln.strip()
                if s and s[0] in "❯>›":  # ❯ > ›
                    return s[1:].strip()
            return ""
        except Exception:
            return ""

    @staticmethod
    def _box_holds_prompt(box, full_prompt):
        """True if the input box still holds OUR un-submitted prompt — a large
        paste placeholder, or a leading-char match on the first line (so a ghost
        suggestion, which won't match, is ignored)."""
        low = box.lower()
        if "[" in box and "pasted" in low:
            return True
        first = full_prompt.strip().splitlines()[0] if full_prompt.strip() else ""
        norm = lambda x: "".join(x.split()).lower()
        b, p = norm(box), norm(first)
        if not b or not p:
            return False
        n = min(len(b), len(p), 16)
        return b[:n] == p[:n]

    def queue_prompt(self, prompt):
        if self.working:
            self._queued_prompts.append(prompt)
            self._status("queued: {}...".format(prompt[:30]))
        else:
            self.query(prompt)

    def interrupt(self, break_channel=True):
        if self._pty:
            try:
                self._pty.write("\x1b")  # Esc cancels the current turn in the TUI
            except Exception:
                pass

    # ── transport: output (transcript) ──────────────────────────────────────
    def _on_transcript_event(self, params):
        # Called from the tailer thread → marshal onto the Sublime main thread.
        sublime.set_timeout(lambda: self._dispatch(params), 0)

    def _dispatch(self, params):
        # While revealed, the user drives the TUI directly and the watcher is
        # suspended; the transcript delta is replayed into the native view on
        # return (see _replay_reveal_delta). Suppressing live dispatch here makes
        # replay the single source of truth, so a turn can't be rendered twice.
        if self.terminal_revealed:
            return
        self._turn_events += 1
        self._last_event_t = time.time()
        try:
            self._on_notification("message", params)
        except Exception as e:
            print("[ptyengine] dispatch error: {} params-type={}".format(e, params.get("type")))
        # Transcript's turn_duration is mapped to a bridge-style "result" —
        # treat it as the authoritative turn-end (deterministic and accurate),
        # with the busy/quiet watcher as a fallback.
        t = params.get("type")
        if t == "tool_use":
            tid = params.get("id")
            if tid:
                self._pending_tools.add(tid)
            if params.get("name") == "AskUserQuestion":
                self._handle_ask_user_question(params)
        elif t == "tool_result":
            tid = params.get("tool_use_id")
            if tid:
                self._pending_tools.discard(tid)
            if self._ask_pending and tid == self._ask_pending.get("tool_id"):
                # Tool resolved on its own (auto-default / denied / etc.) before
                # the TUI dialog showed — drop the pending native panel.
                self._ask_resolved_id = self._ask_pending["tool_id"]
        elif t == "result" and self.working:
            self._finish_turn(meta_already=True)

    # ── turn state (PTY screen) ────────────────────────────────────────────────
    def _read_loop(self):
        # The ONE reader of the pty fd, for the life of the process. It routes
        # each chunk to the active sink: the engine stream (native view) or, when
        # revealed, the Terminal's queue. The sink is swapped under _sink_lock;
        # we never start a second reader (that would split frames / corrupt the
        # incremental UTF-8 decoder).
        while self._pty is not None:
            try:
                data = self._pty.read(1024)
            except (EOFError, OSError):
                break
            except Exception:
                break
            if not data:
                continue
            self._last_pty_byte_t = time.time()  # busy-signal stays valid in both modes
            with self._sink_lock:
                term = self._active_terminal
            if term is not None:
                term.feed_external(data)
            else:
                with self._screen_lock:
                    try:
                        self._stream.feed(data)
                    except Exception:
                        pass
        # EOF / claude died: propagate to a revealed renderer, then finalize.
        with self._sink_lock:
            term = self._active_terminal
        if term is not None:
            try:
                term.signal_pump_eof()
            except Exception:
                pass
        sublime.set_timeout(self._on_pty_exit, 0)

    def _screen_text(self):
        # TerminalScreen is a custom pyte subclass with a SPARSE buffer; read it
        # the way the plugin does (ptty.py:510), not via pyte's .display. Pad
        # column gaps with spaces: claude draws dialog boxes with cursor
        # positioning, leaving gap cells unstored — without padding, "1. Yes"
        # collapses to "1.Yes" and menu/text detection breaks.
        with self._screen_lock:
            scr = self._screen
            if scr is None:
                return ""
            try:
                rows = []
                for y in range(scr.lines):
                    row = scr.buffer[y]
                    if not row:
                        rows.append("")
                        continue
                    parts = []
                    prev = -1
                    for c in sorted(row):
                        if c > prev + 1:
                            parts.append(" " * (c - prev - 1))
                        parts.append(row[c].data)
                        prev = c
                    rows.append("".join(parts))
                return "\n".join(rows)
            except Exception:
                return ""

    def _watch_ready(self):
        if not self._watch_active or self._pty is None:
            return
        # PTY died → surface last screen and recover.
        try:
            alive = self._pty.isalive()
        except Exception:
            alive = False
        if not alive:
            self._on_pty_exit()
            return

        # Input-required: a dialog/permission menu is up that we don't auto-answer
        # (AskUserQuestion + folder-trust are handled elsewhere). The hidden engine
        # can't respond — reveal the live TUI so the user answers there; returning
        # replays the turn into the native view. Must run BEFORE the busy guard,
        # since an open menu reads as "busy".
        if self.working and not self._input_reveal_done \
                and not self._ask_questions and not self._ask_pending \
                and self._screen_has_menu():
            self._input_reveal_done = True
            self.output.text("\n⚠ *Claude needs your input — opening the terminal "
                             "so you can respond. Return to this view when done.*\n")
            self.reveal_as_terminal()
            return

        # Hard guard: don't finalize while the screen indicates claude is busy.
        # Without this, sessions that emit no system/turn_duration records and
        # have multi-minute thinking gaps would falsely finalize during a gap.
        if self._screen_indicates_busy():
            sublime.set_timeout(self._watch_ready, 400)
            return

        # Transcript-driven trigger: tools resolved + event-quiet long enough.
        if self.working and not self._pending_tools and self._turn_events > 0:
            now = time.time()
            if (now - self._last_event_t) > self.QUIET_FINALIZE_AFTER_S:
                self._finish_turn()
        # Anti-hang: query went out, no events at all, no pending tools, no busy
        # indicator — likely a non-submitted prompt or a stuck state. Point the
        # user at the terminal instead of dumping a blank screen.
        elif self.working and self._turn_events == 0 and not self._pending_tools \
                and (time.time() - self._turn_start) > 30:
            self.output.text("\n\n*Claude hasn't responded in 30s — use the switcher "
                             "(⇄ Reveal as terminal) to check the live session.*\n")
            self._finish_turn()
        sublime.set_timeout(self._watch_ready, 400)

    def _screen_indicates_busy(self):
        """Return True if the PTY screen shows claude is still in a state that
        blocks user input — either actively working or awaiting a dialog
        answer. Covers v2.1.x footer texts:
          - "esc to interrupt" → claude is working
          - "esc to cancel"    → claude is awaiting dialog answer (permission /
                                 AskUserQuestion / bypass confirm)
        Signals, in order of robustness:
          1. PTY output activity — while claude works its TUI spinner/elapsed
             timer animates, so bytes keep arriving; a quiet PTY (no output for
             ~`pty_busy_activity_ms`) means it stopped working. Language- and
             version-agnostic (no text scraping). [from echokit_pty]
          2. Structural menu — N+ numbered option lines = an open dialog awaiting
             an answer (also text-agnostic).
          3. Text markers (fallback) — `esc to interrupt` / `esc to cancel`.
             Version-fragile; `pty_busy_markers` overrides the substring list.
        """
        try:
            settings = sublime.load_settings("ClaudeCode.sublime-settings")
            # 1. recent PTY activity == spinner still animating == working
            window_ms = settings.get("pty_busy_activity_ms", 600)
            if self._last_pty_byte_t and \
                    (time.time() - self._last_pty_byte_t) * 1000.0 < window_ms:
                return True
            # 2. an open numbered dialog blocks the user
            if self._screen_has_menu():
                return True
            # 3. fall back to the version-fragile footer text
            markers = settings.get("pty_busy_markers") or self.BUSY_MARKERS_DEFAULT
            txt = re.sub(r"\s+", "", self._screen_text()).lower()
            return any(m and m in txt for m in markers)
        except Exception:
            return False

    def _auto_trust_check(self, attempts=0):
        """Auto-confirm only the first-run *folder trust* dialog (default = "Yes,
        proceed"), which otherwise hangs the hidden session. Scoped to that
        dialog's distinctive text so it never auto-answers permission prompts.
        [pattern from echokit_pty; disable via `pty_auto_trust`]"""
        if self._trust_done or self._pty is None or attempts > 30:
            return
        txt = self._screen_text().lower()
        if "trust the files in this folder" in txt or "do you trust the files" in txt:
            self._trust_done = True
            self._pty_write("\r")
            return
        sublime.set_timeout(lambda: self._auto_trust_check(attempts + 1), 400)

    def _on_pty_exit(self):
        if self._pty_exited:
            return
        self._pty_exited = True
        self._watch_active = False
        # If revealed when claude died, tear the reveal down (close the TUI view).
        if self.terminal_revealed:
            try:
                self._teardown_reveal(hand_back=False)
            except Exception as e:
                print("[ptyengine] reveal teardown on exit failed: {}".format(e))
        screen = self._screen_text()[-1500:]
        self.output.text("\n\n*CLI process exited. Last screen:*\n```\n" + screen + "\n```\n")
        if self.working:
            self.working = False
            try:
                self.output.meta(0, 0, usage=self.context_usage)
            except Exception:
                pass
            self._input_mode_entered = False
            self._enter_input_with_draft()

    def _finish_turn(self, meta_already=False):
        self.working = False
        self._input_reveal_done = False
        self._pending_tools.clear()
        if not meta_already:
            dur = time.time() - self._turn_start if self._turn_start else 0
            try:
                self.output.meta(dur, 0, usage=self.context_usage)
            except Exception as e:
                print("[ptyengine] meta error: {}".format(e))
        self._input_mode_entered = False
        self._enter_input_with_draft()

    # ── hot reveal: native view ⇄ raw TUI (same live pty) ──────────────────────
    def reveal_as_terminal(self):
        """Show this session's live claude TUI in an embedded terminal, WITHOUT
        restarting claude. The engine keeps the single fd reader and routes its
        bytes to the terminal; SIGWINCH makes claude repaint at the view size."""
        if self.terminal_revealed or not self._pty:
            return
        from .terminal.terminal import Terminal
        from .terminal.commands import new_terminal_view
        from .terminal.view import view_size

        # Remember the transcript position so turns typed in the TUI can be
        # replayed into the native view on return (it can't render them live:
        # they bypass query()/output.prompt(), so output.text() has no open turn).
        path = self._transcript_path()
        try:
            self._reveal_jsonl_pos = os.path.getsize(path) if path and os.path.exists(path) else 0
        except Exception:
            self._reveal_jsonl_pos = 0

        # The user drives the TUI directly while revealed — suspend the native
        # turn-state machine and any auto-dialog driver so they don't fire or
        # inject keystrokes under the user.
        self._watch_active = False
        self._ask_questions = None
        self._answer_queue = []
        self._ask_pending = None

        tag = "pty-reveal-{}".format((self.session_id or "x")[:12])
        title = "TUI: " + self.display_name
        view = new_terminal_view(self.window, title, tag)
        view.settings().set("pty_reveal_owner", self.session_id or True)

        rows, cols = view_size(view, default=(40, 120))
        term = Terminal(view)
        term.adopt(self._pty, (rows, cols), tag=tag, default_title=title)

        # Swap the sink to the terminal FIRST, then SIGWINCH: the repaint bytes
        # then flow to the (now active) terminal, not the engine screen.
        with self._sink_lock:
            self._active_terminal = term
        try:
            self._pty.setwinsize(rows, cols)
        except Exception:
            pass

        self._reveal_view = view
        self._reveal_terminal = term
        self._reveal_tag = tag
        self.terminal_revealed = True

        # Alias so get_active_session / focus resolve this session via the
        # terminal view too.
        sublime._claude_sessions[view.id()] = self
        self.window.settings().set("claude_active_view", view.id())
        self.window.focus_view(view)

        self._reveal_poll_active = True
        sublime.set_timeout(self._poll_reveal_pty, 500)
        print("[ptyengine] revealed as terminal sid={} view={}".format(self.session_id, view.id()))

    def return_to_native(self, close_view=True):
        """Hide the raw TUI and restore the native transcript view (claude keeps
        running). The tailer kept the native view current, so no replay needed.
        close_view=False when the reveal view is already closing (on_pre_close)."""
        if not self.terminal_revealed:
            return
        self._teardown_reveal(hand_back=True, close_view=close_view)

    def _teardown_reveal(self, hand_back=True, close_view=True):
        self._reveal_poll_active = False
        term = self._reveal_terminal
        view = self._reveal_view

        # Route bytes back to the engine sink BEFORE stopping the terminal, so no
        # byte is lost to a renderer we're about to release.
        with self._sink_lock:
            self._active_terminal = None
        if term is not None:
            try:
                term.release()
            except Exception:
                pass

        if view is not None:
            sublime._claude_sessions.pop(view.id(), None)
            if close_view and view.is_valid():
                view.set_scratch(True)
                view.close()

        if self.output and self.output.view and self.output.view.is_valid():
            self.window.settings().set("claude_active_view", self.output.view.id())

        self.terminal_revealed = False
        self._reveal_view = None
        self._reveal_terminal = None
        self._reveal_tag = None

        if hand_back and self._pty is not None:
            # Back to the hidden 120x40 → claude repaints into the engine screen,
            # so busy-detection is accurate again.
            try:
                self._pty.setwinsize(40, 120)
            except Exception:
                pass
            with self._screen_lock:
                try:
                    self._screen.resize(40, 120)
                except Exception:
                    pass
            self.output.show()
            self._replay_reveal_delta()  # render turns done in the TUI
            self._watch_active = True
            sublime.set_timeout(self._watch_ready, 500)
            print("[ptyengine] returned to native view sid={}".format(self.session_id))

    def _transcript_path(self):
        try:
            return cc_transcript.transcript_path(self._cwd(), self.session_id)
        except Exception:
            return None

    @staticmethod
    def _user_prompt_text(rec):
        """The human prompt text of a transcript 'user' record, or None if the
        record isn't a plain user prompt (e.g. a tool_result-carrying record)."""
        if rec.get("type") != "user":
            return None
        msg = rec.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            return c.strip() or None
        if isinstance(c, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                return None
            parts = [b.get("text", "") for b in c
                     if isinstance(b, dict) and b.get("type") == "text"]
            return "".join(parts).strip() or None
        return None

    def _replay_reveal_delta(self):
        """Render transcript records written while revealed into the native view.

        Turns typed in the TUI bypass query(), so the native renderer never
        opened a turn for them (output.text() drops without one). Here we open a
        turn per user prompt (which cc_transcript intentionally skips) and feed
        assistant text / tool calls / results through the normal pipeline."""
        path = self._transcript_path()
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r") as f:
                f.seek(self._reveal_jsonl_pos)
                lines = f.readlines()
        except Exception as e:
            print("[ptyengine] replay read failed: {}".format(e))
            return
        id2 = {}
        rendered = False
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("isSidechain") or rec.get("isMeta"):
                continue
            utext = self._user_prompt_text(rec)
            if utext is not None:
                self.output.prompt(utext, [])  # opens a native turn
                rendered = True
                continue
            try:
                for params in cc_transcript.record_to_params(rec, id2):
                    self._on_notification("message", params)
                    rendered = True
            except Exception as e:
                print("[ptyengine] replay record error: {}".format(e))
        if rendered:
            self.working = False
            self._input_mode_entered = False
            self._enter_input_with_draft()
        print("[ptyengine] replayed {} delta lines from pos {}".format(
            len(lines), self._reveal_jsonl_pos))

    def _poll_reveal_pty(self):
        if not self._reveal_poll_active:
            return
        try:
            alive = self._pty.isalive() if self._pty else False
        except Exception:
            alive = False
        if not alive:
            self._on_pty_exit()  # idempotent: tears down reveal + finalizes
            return
        view = self._reveal_view
        if view is not None and not view.is_valid():
            # View closed without going through our close guard — hand back.
            self.return_to_native()
            return
        sublime.set_timeout(self._poll_reveal_pty, 500)

    # ── helpers ────────────────────────────────────────────────────────────────
    def _resolve_claude(self, settings):
        configured = settings.get("claude_bin")
        if configured and os.path.isfile(os.path.expanduser(configured)):
            return os.path.expanduser(configured)
        found = shutil.which("claude")
        if found:
            return found
        for c in ("/usr/local/bin/claude", "/opt/homebrew/bin/claude",
                  os.path.expanduser("~/.local/bin/claude")):
            if os.path.isfile(c):
                return c
        return None

    def _build_mcp_config(self, settings):
        """Inline-JSON --mcp-config that wires the sublime MCP server (same as
        the DSR bridge), so the PTY claude has the plugin's tools. Returns "" to
        skip injection. Merged with the user's existing MCP config by claude."""
        if not settings.get("pty_inject_sublime_mcp", True):
            return ""
        if not (self.output and self.output.view):
            return ""
        server = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "mcp", "server.py")
        if not os.path.exists(server):
            return ""
        py = settings.get("python_path") or sys.executable
        return json.dumps({
            "mcpServers": {
                "sublime": {
                    "type": "stdio",
                    "command": py,
                    "args": [server, "--view-id={}".format(self.output.view.id())],
                }
            }
        })

    # ── AskUserQuestion: native quick panel + drive the hidden TUI dialog ───
    # Probe confirmed: dialog uses arrow keys + Enter; default = option 1.
    # To pick option N, send (N-1) Down arrows + Enter. Multi-question dialogs
    # advance automatically after Enter. POC scope: single-select only — multi-
    # select would need toggle/confirm orchestration (deferred); "Other" / free
    # text input also deferred.
    def _handle_ask_user_question(self, params):
        inp = params.get("input") or {}
        questions = inp.get("questions") or []
        if not questions:
            return
        if any(q.get("multiSelect") for q in questions):
            return  # surface-only fallback (multi-select keystroke driver TBD)
        if self._ask_questions is not None or self._ask_pending is not None:
            return  # one at a time
        # Don't guess based on permission mode — different modes (acceptEdits,
        # dontAsk, bypassPermissions) auto-resolve the tool without ever
        # showing the dialog, so popping a native panel speculatively is wrong.
        # Mark pending and wait for the actual TUI dialog to appear on the PTY
        # screen. If it does → pop native panel + drive keystrokes. If a
        # tool_result arrives first → tool auto-resolved → drop pending.
        self._ask_pending = {
            "questions": questions,
            "tool_id": params.get("id"),
            "deadline": time.time() + 5.0,
        }
        sublime.set_timeout(self._poll_ask_dialog, 300)

    def _poll_ask_dialog(self):
        p = self._ask_pending
        if p is None:
            return
        if self._ask_resolved_id == p["tool_id"]:
            self._ask_pending = None
            self._ask_resolved_id = None
            return
        if self._screen_has_menu():
            # TUI dialog is actually waiting on the user — pop native panel.
            questions = p["questions"]
            self._ask_pending = None
            self._ask_questions = questions
            try:
                self.output.question_request(
                    id(self), questions, self._on_question_answers)
            except Exception as e:
                print("[ptyengine] question_request error: {}".format(e))
                self._ask_questions = None
            return
        if time.time() > p["deadline"]:
            self._ask_pending = None
            return
        sublime.set_timeout(self._poll_ask_dialog, 300)

    def _screen_has_menu(self):
        """Structural detection of an interactive TUI menu: N+ consecutive
        numbered-option lines (`1. …` `2. …` `3. …`) near the bottom of the
        rendered screen. Text-agnostic — survives copy/wording changes."""
        rows = [ln.rstrip() for ln in self._screen_text().splitlines() if ln.strip()]
        if not rows:
            return False
        run = 0
        # walk from the bottom up — menus sit at the bottom of the screen
        for ln in reversed(rows[-12:]):
            if self.DIALOG_OPTION_RE.match(ln):
                run += 1
                if run >= self.DIALOG_MIN_OPTIONS:
                    return True
            elif run:
                # contiguous run broken; menus are unbroken numbered blocks
                run = 0
        return False

    def _on_question_answers(self, answers):
        qs = self._ask_questions or []
        self._ask_questions = None
        if answers is None:
            self._pty_write("\x1b")  # cancelled → Esc the TUI dialog
            return
        queue = []
        for q in qs:
            chosen = answers.get(q.get("question", ""))
            opts = q.get("options") or []
            idx = next((i for i, o in enumerate(opts)
                        if (o.get("label") if isinstance(o, dict) else o) == chosen), None)
            if idx is None:
                # "Other" or free-typed answer not in options → POC bails
                self.output.text("\n*[ask] free-text/Other answer not yet "
                                 "supported in PTY engine — cancelled*\n")
                self._pty_write("\x1b")
                return
            queue.append(idx)
        self._answer_queue = queue
        self._drive_next_answer()

    def _drive_next_answer(self):
        if not self._answer_queue or not self._pty:
            return
        idx = self._answer_queue.pop(0)
        for _ in range(idx):
            self._pty_write("\x1b[B")  # Down arrow
        # Tiny pause then Enter; if more questions remain, give the TUI a
        # moment to render the next dialog before the next batch of arrows.
        sublime.set_timeout(self._fire_enter, 200)

    def _fire_enter(self):
        self._pty_write("\r")
        if self._answer_queue:
            sublime.set_timeout(self._drive_next_answer, 500)

    def _pty_write(self, s):
        if self._pty:
            try:
                self._pty.write(s)
            except Exception:
                pass

    def _materialize_images(self, images):
        """Decode context images to temp files; return their paths."""
        paths = []
        for img in images:
            mime = img.get("mime_type", "")
            ext = ".png" if "png" in mime else ".jpg" if ("jpeg" in mime or "jpg" in mime) else ".img"
            try:
                raw = base64.b64decode(img.get("data", ""))
                with tempfile.NamedTemporaryFile(suffix=ext, prefix="cc_img_", delete=False) as f:
                    f.write(raw)
                    paths.append(f.name)
            except Exception as e:
                print("[ptyengine] image materialize failed: {}".format(e))
        return paths

    def dump_screen(self):
        """Return the PTY's current rendered screen text (debug / escape hatch)."""
        return self._screen_text()


def create_pty_session(window):
    """Mirror core.create_session for the PTY engine (no bridge)."""
    old_active = window.settings().get("claude_active_view")
    if old_active and old_active in sublime._claude_sessions:
        old = sublime._claude_sessions[old_active]
        old.output.set_name(old.name or "Claude")

    s = PtyEngineSession(window)
    s.output.show()
    s.start()
    if s.output.view:
        view_id = s.output.view.id()
        sublime._claude_sessions[view_id] = s
        window.settings().set("claude_active_view", view_id)
        print("[ptyengine] create_pty_session: view_id={}".format(view_id))
    return s
