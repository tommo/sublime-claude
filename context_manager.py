"""Pending context items attached to the next query.

A ContextManager owns one session's queued context (files, selections, folders,
images) and the prompt-building logic that prepends them to the next query. The
output view's `📎 ...` indicator is updated whenever the list changes.

The Session class still exposes `add_context_file/selection/folder/image`,
`clear_context`, and `pending_context` as thin shims pointing at this object,
so existing callers (commands.py, listeners.py, output.py) keep working.
"""
import base64
import os
import tempfile
from typing import List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .session import Session


class ContextItem:
    """A pending context item to attach to next query."""
    def __init__(self, kind: str, name: str, content: str):
        self.kind = kind  # "file" | "selection" | "folder" | "image"
        self.name = name  # Display name
        self.content = content  # Actual content (or __IMAGE__:mime:b64 for images)


class ContextManager:
    """Owns the pending-context list for one session."""

    def __init__(self, session: "Session"):
        self.session = session
        self.items: List[ContextItem] = []

    # ── Queries ────────────────────────────────────────────────────────

    def __bool__(self) -> bool:
        return bool(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    # ── Mutations ──────────────────────────────────────────────────────

    def add_file(self, path: str, content: str) -> None:
        name = os.path.basename(path)
        self.items.append(ContextItem("file", name, f"File: {path}\n```\n{content}\n```"))
        self._refresh_display()

    def add_selection(self, path: str, content: str) -> None:
        name = os.path.basename(path) if path else "selection"
        self.items.append(ContextItem("selection", name, f"Selection from {path}:\n```\n{content}\n```"))
        self._refresh_display()

    def add_folder(self, path: str) -> None:
        name = os.path.basename(path) + "/"
        self.items.append(ContextItem("folder", name, f"Folder: {path}"))
        self._refresh_display()

    def add_image(self, image_data: bytes, mime_type: str) -> None:
        ext = ".png" if "png" in mime_type else ".jpg" if "jpeg" in mime_type or "jpg" in mime_type else ".img"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(image_data)
            temp_path = f.name
        encoded = base64.b64encode(image_data).decode("utf-8")
        name = f"image{ext}"
        # Special marker format that build() detects to extract image data
        self.items.append(ContextItem("image", name, f"__IMAGE__:{mime_type}:{encoded}"))
        print(f"[Claude] Added image to context: {name} ({len(image_data)} bytes, saved to {temp_path})")
        self._refresh_display()

    def clear(self) -> None:
        self.items = []
        self._refresh_display()

    def take(self) -> Tuple[List[ContextItem], List[str]]:
        """Consume the current items: returns (items_snapshot, names) and clears."""
        items = self.items
        names = [it.name for it in items]
        self.items = []
        self._refresh_display()
        return items, names

    # ── Prompt assembly ────────────────────────────────────────────────

    def build_prompt(self, prompt: str) -> Tuple[str, List[dict]]:
        """Prepend pending text-context items; extract image items separately.

        Returns (full_prompt, images) where images = [{"mime_type", "data"}, ...].
        Does not clear the list — callers do that explicitly via take().
        """
        if not self.items:
            return prompt, []
        parts: List[str] = []
        images: List[dict] = []
        for item in self.items:
            if item.content.startswith("__IMAGE__:"):
                _, mime_type, data = item.content.split(":", 2)
                images.append({"mime_type": mime_type, "data": data})
            else:
                parts.append(item.content)
        parts.append(prompt)
        return "\n\n".join(parts), images

    # ── Internal ───────────────────────────────────────────────────────

    def _refresh_display(self) -> None:
        """Notify the output view that the indicator should re-render."""
        self.session.output.set_pending_context(self.items)
