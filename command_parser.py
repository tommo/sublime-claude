"""Slash command parsing for /command syntax."""
import re
from typing import Optional, List, Callable
from dataclasses import dataclass


@dataclass
class LoopCommand:
    """A parsed loop:[duration] prompt command."""
    interval_sec: Optional[int]  # None = use default
    prompt: str
    cancel: bool = False  # True if "loop:cancel"


_DURATION_RE = re.compile(r'^(\d+)\s*([smhd]?)$', re.IGNORECASE)


def _parse_duration(s: str) -> Optional[int]:
    """Parse "5m", "30s", "1h", "2d" or plain seconds. Returns seconds or None."""
    m = _DURATION_RE.match(s.strip())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower() or "s"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def parse_loop(text: str) -> Optional[LoopCommand]:
    """Parse loop:[duration] prompt or loop:cancel.

    Examples:
        loop:5m fix lint errors    → interval=300, prompt="fix lint errors"
        loop: monitor build        → interval=None, prompt="monitor build"
        loop:cancel                → cancel=True
    """
    text = text.strip()
    if not text.lower().startswith("loop:"):
        return None
    rest = text[5:].strip()
    if rest.lower() in ("cancel", "stop", "off"):
        return LoopCommand(interval_sec=None, prompt="", cancel=True)
    # Try parsing first token as duration
    parts = rest.split(None, 1)
    if not parts:
        return None
    interval = _parse_duration(parts[0])
    if interval is not None and len(parts) > 1:
        return LoopCommand(interval_sec=interval, prompt=parts[1].strip())
    # No duration → entire rest is the prompt
    return LoopCommand(interval_sec=None, prompt=rest)


@dataclass
class SlashCommand:
    """A parsed slash command."""
    name: str  # Command name without /
    args: str  # Arguments after command name
    raw: str  # Original input


@dataclass
class CommandDef:
    """Definition of a slash command."""
    name: str
    description: str
    handler: Optional[Callable] = None  # Local handler, or None to send to bridge


class CommandParser:
    """Parses input for /command patterns."""

    BUILTIN_COMMANDS = {
        "clear": "Clear conversation history (local)",
        "compact": "Summarize conversation to reduce context",
        "context": "Show pending context items",
    }

    @staticmethod
    def parse(text: str) -> Optional[SlashCommand]:
        """Parse text for slash command.

        Returns SlashCommand if text starts with /, None otherwise.
        """
        text = text.strip()
        if not text.startswith("/"):
            return None

        # Split into command and args
        parts = text[1:].split(None, 1)  # Split on whitespace, max 2 parts
        if not parts:
            return None

        name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        return SlashCommand(name=name, args=args, raw=text)

    @staticmethod
    def is_builtin(name: str) -> bool:
        """Check if command is a builtin."""
        return name in CommandParser.BUILTIN_COMMANDS

    @staticmethod
    def get_completions() -> List[CommandDef]:
        """Get list of available commands for autocomplete."""
        return [
            CommandDef(name=name, description=desc)
            for name, desc in CommandParser.BUILTIN_COMMANDS.items()
        ]
