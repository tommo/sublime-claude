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


class ClaudeCodePtyStartCommand(sublime_plugin.WindowCommand):
    """Start a CLI session: real interactive `claude` in a hidden PTY, rendered
    natively by tailing the session transcript (rides subscription billing)."""
    def run(self) -> None:
        from .. import cc_pty_session
        cc_pty_session.create_pty_session(self.window)


class ClaudeRevealCliScreenCommand(sublime_plugin.WindowCommand):
    """Dump the hidden PTY's current rendered screen (debug / escape hatch)."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if not hasattr(s, "dump_screen"):
            sublime.status_message("No CLI (PTY) session active")
            return
        screen = s.dump_screen()
        print("[ptyengine] ===== CLI SCREEN =====\n" + screen + "\n[ptyengine] =====================")
        if s.output and s.output.view:
            s.output.text("\n\n*CLI screen snapshot:*\n```\n" + screen + "\n```\n")


class ClaudeCodeToggleTerminalRevealCommand(sublime_plugin.WindowCommand):
    """Hot-swap the active PTY-engine session between its native transcript view
    and the raw claude TUI in an embedded terminal (same live process)."""
    def run(self) -> None:
        from .. import cc_pty_session
        s = get_active_session(self.window)
        if not isinstance(s, cc_pty_session.PtyEngineSession):
            sublime.status_message("Active session is not a CLI (PTY) session")
            return
        if s.terminal_revealed:
            s.return_to_native()
        else:
            s.reveal_as_terminal()

    def is_enabled(self) -> bool:
        from .. import cc_pty_session
        return isinstance(get_active_session(self.window), cc_pty_session.PtyEngineSession)


