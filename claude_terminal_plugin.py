"""Re-export Claude Terminal commands so Sublime Text discovers them."""
import os

import sublime

from .terminal.view import (  # noqa: F401
    ClaudeTerminalInsertCommand,
    ClaudeTerminalTrimTrailingLinesCommand,
    ClaudeTerminalNukeCommand,
)
from .terminal.render import (  # noqa: F401
    ClaudeTerminalRenderCommand,
    ClaudeTerminalShowCursorCommand,
    ClaudeTerminalCleanupCommand,
)
from .terminal.commands import (  # noqa: F401
    ClaudeTerminalOpenCommand,
    ClaudeTerminalKeypressCommand,
    ClaudeTerminalPasteCommand,
    ClaudeTerminalCopyCommand,
    ClaudeTerminalSendStringCommand,
    ClaudeTerminalCloseCommand,
    ClaudeTerminalResetCommand,
    ClaudeTerminalActivateCommand,
    ClaudeTerminalPasteTextCommand,
    ClaudeTerminalClearUndoStackCommand,
    ClaudeTerminalScrollCommand,
    ClaudeTerminalAdjustFontSizeCommand,
    NoopCommand,
)
from .terminal.event import ClaudeTerminalEventListener  # noqa: F401


def plugin_loaded():
    # Generate the fg-color scheme (16 ANSI + 256 palette, foreground-only) the
    # renderer colors cells against. Written to User so it isn't a repo artifact;
    # 24-bit truecolor is approximated to the nearest 256 color at render time.
    path = os.path.join(
        sublime.packages_path(), "User", "ClaudeTerminal.hidden-color-scheme")
    if not os.path.isfile(path):
        try:
            from .terminal.theme_generator import generate_theme_file
            generate_theme_file(path, foreground_only=True)
            print("ClaudeTerminal: generated color scheme at", path)
        except Exception as e:
            print("ClaudeTerminal: color scheme generation failed:", e)
