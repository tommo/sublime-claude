"""Claude Code terminal mode — run the real interactive `claude` CLI inside the
embedded terminal view, with the plugin's MCP server injected the same way the
SDK / hidden-PTY engine sessions do (so the in-terminal Claude has the editor
tools: sublime_eval, read_view, spawn_session, …).

Unlike PtyEngineSession (hidden PTY + transcript-rendered native UI), this is the
*visible* Claude Code TUI rendered by our terminal emulator.
"""
import os
import re
import uuid

import sublime
import sublime_plugin

from . import cc_launch
from . import cc_transcript
from .terminal.terminal import Terminal
from .terminal.commands import new_terminal_view


def _build_claude_argv(window, settings, claude_bin, view_id,
                       clean_model, perm, session_id, resume_existing):
    """Assemble the interactive `claude` argv.

    `session_id` pins the conversation id: a fresh launch creates it
    (`--session-id`); a restore resumes it (`--resume`). The MCP server is scoped
    to `view_id` and `--add-dir` is recomputed from the window every time, so a
    restored view rebinds the editor tools to its new id and current folders
    rather than the stale ones baked into the original launch command."""
    argv = [claude_bin, "--permission-mode", perm]
    if clean_model:
        argv += ["--model", clean_model]
    argv += cc_launch.add_dir_args(window)
    mcp_cfg = cc_launch.build_sublime_mcp_config(settings, view_id)
    if mcp_cfg:
        argv += ["--mcp-config", mcp_cfg]
    if session_id:
        argv += (["--resume", session_id] if resume_existing
                 else ["--session-id", session_id])
    return argv


class ClaudeCodeTerminalCommand(sublime_plugin.WindowCommand):
    """Open a terminal running interactive `claude` with the sublime MCP wired in.

    The MCP server is scoped to the terminal view's own id, so MCP tools
    (sublime_eval, spawn_session, …) act in this session's context.

    With no explicit `model`, prompts for a model in a quick panel first, so the
    model is chosen per-session before launch. Each interactive launch gets its
    own tag, so multiple Claude Code terminals can run side by side.
    """
    # Markers that mean the Claude Code TUI has finished booting and its input
    # box is accepting keystrokes (version-fragile; kept broad).
    _READY_MARKERS = ("for shortcuts", "Welcome to Claude", "? for help")

    def run(self, cwd=None, tag=None, resume=None, draft=None, model=None):
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        if not cc_launch.resolve_claude(settings):
            sublime.error_message(
                "Claude CLI not found. Set 'claude_bin' in settings or add it to PATH.")
            return

        # An explicit tag means "focus that one if it's already open".
        if tag:
            existing = Terminal.from_tag(tag)
            if existing and existing.view:
                self.window.focus_view(existing.view)
                return

        # Per-session model selection: pick a model unless one was given (or we're
        # resuming, where the model is fixed by the existing session).
        if model is None and not resume:
            self._pick_model_then_launch(cwd, tag, resume, draft)
            return

        self._launch(cwd, tag, resume, draft, None if model == "default" else model)

    def _pick_model_then_launch(self, cwd, tag, resume, draft):
        models = self._claude_models()
        labels = [["Default model", "claude CLI default"]]
        ids = [None]
        for mid, name in models:
            labels.append([name, mid])
            ids.append(mid)

        def on_pick(idx):
            if idx < 0:
                return
            self._launch(cwd, tag, resume, draft, ids[idx])

        # Defer so a parent quick panel (the switcher) has closed first.
        sublime.set_timeout(
            lambda: self.window.show_quick_panel(
                labels, on_pick, placeholder="Model for this Claude Code session"),
            0)

    @staticmethod
    def _claude_models():
        """[(model_id, label)] for the claude backend (settings override → defaults)."""
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        raw = (settings.get("models", {}) or {}).get("claude")
        if not raw:
            try:
                from . import backends
                raw = backends.BACKENDS["claude"].default_models
            except Exception:
                raw = []
        out = []
        for m in raw:
            if isinstance(m, str):
                out.append((m, m))
            elif isinstance(m, (list, tuple)) and len(m) >= 2:
                out.append((m[0], m[1]))
        return out

    def _launch(self, cwd, tag, resume, draft, model):
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        claude_bin = cc_launch.resolve_claude(settings)
        tag = tag or ("claude-code-" + uuid.uuid4().hex[:6])  # unique → multi-session
        cwd = cwd or cc_launch.window_cwd(self.window)

        # Create the view first so the MCP server can be scoped to its id.
        view = new_terminal_view(self.window, "Claude Code", tag)

        perm = settings.get("pty_permission_mode", "acceptEdits")
        # sublime-claude model ids are virtual ("claude-opus-4-8[1m]@400k",
        # "opus@400k"); the real CLI --model wants the bare id/alias.
        clean_model = None
        if model:
            clean_model = re.sub(r"\[[^\]]*\]", "", model).split("@", 1)[0].strip() or None

        # Resuming uses the caller's existing id; a fresh launch pins a new
        # deterministic id so this conversation can be resumed later (restart
        # restore / reconnection).
        resume_existing = bool(resume)
        session_id = resume or str(uuid.uuid4())

        argv = _build_claude_argv(self.window, settings, claude_bin, view.id(),
                                  clean_model, perm, session_id, resume_existing)
        env = cc_launch.load_env(self.window, settings, cwd)  # merged onto os.environ in start()

        Terminal(view).start(cmd=argv, cwd=cwd, env=env, tag=tag,
                             default_title="Claude Code")

        # Persist a relaunch recipe so hot-exit restore rebuilds argv (--resume +
        # a freshly-scoped MCP view id) via claude_code_terminal_reactivate,
        # instead of re-running the stale verbatim cmd.
        view.settings().set("claude_terminal.reactivate_command",
                            "claude_code_terminal_reactivate")
        view.settings().set("claude_terminal.cc_recipe", {
            "session_id": session_id,
            "model": clean_model,
            "perm": perm,
            "cwd": cwd,
            "tag": tag,
        })
        print("[claude-terminal] launched {} model={} sid={} cwd={}".format(
            claude_bin, clean_model or "default", session_id, cwd))

        # Push Sublime editor context into the session at launch (the reason the
        # harness runs inside Sublime). Default to a compact summary unless the
        # caller passed an explicit draft or disabled it via settings.
        if draft is None and settings.get("claude_terminal_push_context", True):
            draft = cc_launch.build_draft(self.window)

        # PTY-inject the starting context once the TUI is ready: bracketed-paste
        # it into the prompt box WITHOUT submitting, so the user reviews + sends.
        if draft:
            sublime.set_timeout(lambda: self._inject_draft(tag, draft), 800)

    @classmethod
    def _inject_draft(cls, tag, draft, attempts=0):
        t = Terminal.from_tag(tag, current_window_only=False)
        if not t or not t.view or not t.view.is_valid() or not t.is_alive():
            return
        ready = cls._screen_has_marker(t) or attempts >= 40  # ~8s fallback
        if ready:
            t.view.run_command("claude_terminal_paste_text",
                                {"text": draft, "bracketed": True})
            return
        sublime.set_timeout(lambda: cls._inject_draft(tag, draft, attempts + 1), 200)

    @classmethod
    def _screen_has_marker(cls, terminal):
        """Read-only peek at the rendered screen for a TUI-ready marker."""
        try:
            scr = terminal.screen
            rows = []
            for y in range(scr.lines):
                row = scr.buffer.get(y)
                if row:
                    rows.append("".join(row[x].data for x in sorted(row.keys())))
            txt = "\n".join(rows)
        except Exception:
            return False
        return any(m in txt for m in cls._READY_MARKERS)


class ClaudeCodeTerminalReactivateCommand(sublime_plugin.TextCommand):
    """Hot-exit restore for a Claude Code terminal.

    Rebuilds the launch command from the persisted recipe so the restored view
    resumes the same conversation (--resume) and re-scopes the MCP server to its
    new view id — rather than the generic verbatim restore, which would start a
    fresh empty session against a dead view id."""
    def run(self, edit):
        view = self.view
        if Terminal.from_id(view.id()):
            return
        recipe = view.settings().get("claude_terminal.cc_recipe") or {}
        sid = recipe.get("session_id")
        if not sid:
            return
        window = view.window() or sublime.active_window()
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        claude_bin = cc_launch.resolve_claude(settings)
        if not claude_bin:
            return
        cwd = recipe.get("cwd") or cc_launch.window_cwd(window)
        # Resume if the transcript was already written; otherwise re-pin the same
        # id (restart before the first turn, so --resume would error).
        resume_existing = os.path.exists(cc_transcript.transcript_path(cwd, sid))
        argv = _build_claude_argv(window, settings, claude_bin, view.id(),
                                  recipe.get("model"), recipe.get("perm", "acceptEdits"),
                                  sid, resume_existing)
        env = cc_launch.load_env(window, settings, cwd)
        view.set_scratch(True)
        Terminal(view).start(cmd=argv, cwd=cwd, env=env, tag=recipe.get("tag", ""),
                             default_title="Claude Code")
        print("[claude-terminal] reactivated sid={} resume={} cwd={}".format(
            sid, resume_existing, cwd))
