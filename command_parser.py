"""Slash command parsing for /command syntax."""
from typing import Optional, List, Callable
from dataclasses import dataclass


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
