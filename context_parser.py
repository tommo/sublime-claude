"""Context parsing - self-contained unit for @ triggers and context menu handling."""
from typing import List, Tuple, Optional, Callable
from dataclasses import dataclass


@dataclass
class ContextMenuItem:
    """A single item in the context menu."""
    action: str  # "browse", "clear", "file"
    label: str
    description: str
    data: any = None  # Optional data (e.g., view object)


@dataclass
class ContextTrigger:
    """Represents a detected @ trigger."""
    position: int  # Cursor position where @ was typed
    triggered: bool = True


class ContextParser:
    """Parses input for @ context triggers and builds context menus."""

    TRIGGER_CHAR = "@"

    @staticmethod
    def check_trigger(text: str, cursor_pos: int) -> Optional[ContextTrigger]:
        """Check if @ trigger was just typed at cursor position.

        Args:
            text: Full text content
            cursor_pos: Current cursor position

        Returns:
            ContextTrigger if @ was just typed, None otherwise
        """
        if cursor_pos <= 0:
            return None

        char_before = text[cursor_pos - 1] if cursor_pos <= len(text) else ""
        if char_before == ContextParser.TRIGGER_CHAR:
            return ContextTrigger(position=cursor_pos)

        return None

    @staticmethod
    def build_menu(
        open_files: List[Tuple[str, str]],  # [(name, path), ...]
        has_pending_context: bool = False,
        pending_count: int = 0
    ) -> List[ContextMenuItem]:
        """Build context menu items.

        Args:
            open_files: List of (filename, filepath) tuples
            has_pending_context: Whether there's pending context to clear
            pending_count: Number of pending context items

        Returns:
            List of ContextMenuItem objects
        """
        items = []

        # Browse option (always first)
        items.append(ContextMenuItem(
            action="browse",
            label="Browse...",
            description="Choose file from project"
        ))

        # Clear context (only if there's pending context)
        if has_pending_context:
            plural = "s" if pending_count != 1 else ""
            items.append(ContextMenuItem(
                action="clear",
                label="Clear context",
                description=f"{pending_count} pending item{plural}"
            ))

        # Open files
        for name, path in open_files:
            items.append(ContextMenuItem(
                action="file",
                label=name,
                description=path,
                data=path
            ))

        return items

    @staticmethod
    def format_menu_items(items: List[ContextMenuItem]) -> List[List[str]]:
        """Format menu items for Sublime's quick panel.

        Returns:
            List of [label, description] pairs
        """
        return [[item.label, item.description] for item in items]

    @staticmethod
    def remove_trigger(text: str, trigger_pos: int) -> Tuple[str, int]:
        """Remove @ character from text.

        Args:
            text: Full text
            trigger_pos: Position after the @

        Returns:
            Tuple of (new_text, new_cursor_position)
        """
        if trigger_pos <= 0 or trigger_pos > len(text):
            return text, trigger_pos

        # Remove @ character
        new_text = text[:trigger_pos - 1] + text[trigger_pos:]
        new_cursor = trigger_pos - 1

        return new_text, new_cursor


class ContextMenuHandler:
    """Handles context menu selection and actions."""

    def __init__(
        self,
        on_browse: Callable[[], None],
        on_clear: Callable[[], None],
        on_add_file: Callable[[str, str], None],  # (path, content) -> None
    ):
        """Initialize handler with action callbacks.

        Args:
            on_browse: Callback when Browse is selected
            on_clear: Callback when Clear is selected
            on_add_file: Callback when a file is selected (path, content)
        """
        self.on_browse = on_browse
        self.on_clear = on_clear
        self.on_add_file = on_add_file

    def handle_selection(self, items: List[ContextMenuItem], index: int) -> None:
        """Handle menu item selection.

        Args:
            items: The menu items that were shown
            index: Selected item index (-1 if cancelled)
        """
        if index < 0:
            return  # User cancelled

        selected = items[index]

        if selected.action == "browse":
            self.on_browse()
        elif selected.action == "clear":
            self.on_clear()
        elif selected.action == "file":
            # Need to get file content - this is implementation-specific
            # The callback should handle reading the file
            if selected.data:
                # Data should be the file path
                self.on_add_file(selected.data, "")  # Content loaded by callback


def extract_context_marker(text: str) -> Optional[Tuple[str, int, int]]:
    """Extract context marker patterns like @file or @selection.

    Args:
        text: Text to search

    Returns:
        Tuple of (marker_type, start_pos, end_pos) or None
    """
    # This is a placeholder for more sophisticated marker parsing
    # Could support patterns like @file:path, @selection, @folder:path
    import re

    pattern = r'@(\w+)(?::(\S+))?'
    match = re.search(pattern, text)

    if match:
        marker_type = match.group(1)
        return marker_type, match.start(), match.end()

    return None
