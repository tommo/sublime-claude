"""Logging utilities for Claude Code plugin."""
import os
from typing import Optional
from pathlib import Path


class Logger:
    """Simple file-based logger with context support."""

    def __init__(self, log_path: str, prefix: str = ""):
        self.log_path = log_path
        self.prefix = prefix

    def log(self, message: str, prefix: Optional[str] = None) -> None:
        """Write a log message to the log file.

        Args:
            message: Message to log
            prefix: Optional prefix override (uses instance prefix if not provided)
        """
        actual_prefix = prefix if prefix is not None else self.prefix
        try:
            with open(self.log_path, "a") as f:
                f.write(f"{actual_prefix}{message}\n")
        except Exception:
            # Silently fail - don't break the app if logging fails
            pass

    def info(self, message: str) -> None:
        """Log an info message."""
        self.log(message, "  ")

    def error(self, message: str) -> None:
        """Log an error message."""
        self.log(message, "ERROR: ")

    def debug(self, message: str) -> None:
        """Log a debug message."""
        self.log(message, "DEBUG: ")

    def separator(self, char: str = "=", length: int = 50) -> None:
        """Log a separator line."""
        self.log(char * length, "")

    def clear(self) -> None:
        """Clear the log file."""
        try:
            if os.path.exists(self.log_path):
                os.remove(self.log_path)
        except Exception:
            pass


class ContextLogger:
    """Logger with automatic context tracking."""

    def __init__(self, logger: Logger, context: str = ""):
        self.logger = logger
        self.context = context

    def log(self, message: str, prefix: Optional[str] = None) -> None:
        """Log with context prefix."""
        ctx_prefix = f"[{self.context}] " if self.context else ""
        full_message = f"{ctx_prefix}{message}"
        self.logger.log(full_message, prefix)

    def info(self, message: str) -> None:
        self.log(message, "  ")

    def error(self, message: str) -> None:
        self.log(message, "ERROR: ")

    def debug(self, message: str) -> None:
        self.log(message, "DEBUG: ")


# Global logger instances
_bridge_logger: Optional[Logger] = None
_plugin_logger: Optional[Logger] = None


def get_bridge_logger(log_path: str = "/tmp/claude_bridge.log") -> Logger:
    """Get or create the bridge logger singleton."""
    global _bridge_logger
    if _bridge_logger is None:
        _bridge_logger = Logger(log_path, prefix="")
    return _bridge_logger


def get_plugin_logger() -> Logger:
    """Get or create the plugin logger singleton (uses Sublime's console)."""
    global _plugin_logger
    if _plugin_logger is None:
        # For plugin, we just print to console (captured by Sublime)
        class ConsoleLogger(Logger):
            def __init__(self):
                super().__init__("", prefix="[Claude] ")

            def log(self, message: str, prefix: Optional[str] = None) -> None:
                actual_prefix = prefix if prefix is not None else self.prefix
                print(f"{actual_prefix}{message}")

        _plugin_logger = ConsoleLogger()

    return _plugin_logger


# Convenience functions
def log_bridge(message: str, context: str = "") -> None:
    """Log a message to the bridge log."""
    logger = get_bridge_logger()
    if context:
        logger = ContextLogger(logger, context)
    logger.info(message)


def log_bridge_error(message: str, context: str = "") -> None:
    """Log an error to the bridge log."""
    logger = get_bridge_logger()
    if context:
        logger = ContextLogger(logger, context)
    logger.error(message)


def log_plugin(message: str) -> None:
    """Log a message to the plugin console."""
    get_plugin_logger().info(message)


def log_plugin_error(message: str) -> None:
    """Log an error to the plugin console."""
    get_plugin_logger().error(message)
