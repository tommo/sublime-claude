"""Pending context items attached to the next query.

A ContextManager owns one session's queued context (files, selections, folders,
images, path refs) and the prompt-building logic that prepends them to the next
query. The output view's `📎 ...` indicator is updated whenever the list changes.

The Session class still exposes `add_context_file/selection/folder/image/path`,
`clear_context`, and `pending_context` as thin shims pointing at this object,
so existing callers (commands.py, listeners.py, output.py) keep working.
"""
import base64
import os
import re
import tempfile
from typing import List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .session import Session

_IMAGE_EXTS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".tif", ".tiff", ".heic", ".heif", ".ico",
)
# Text-like extensions → open in Sublime; others → reveal in file manager.
_CODE_EXTS = (
    ".py", ".pyi", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".json", ".jsonc", ".toml", ".yaml", ".yml", ".xml", ".html", ".htm",
    ".css", ".scss", ".less", ".md", ".mdx", ".rst", ".txt", ".csv",
    ".rs", ".go", ".java", ".kt", ".kts", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".m", ".mm", ".scala", ".clj", ".lua",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".sql", ".graphql", ".proto", ".thrift", ".r", ".jl", ".nim", ".zig",
    ".vue", ".svelte", ".astro", ".tf", ".hcl", ".nix", ".cmake",
    ".makefile", ".mk", ".gradle", ".properties", ".ini", ".cfg", ".conf",
    ".env", ".gitignore", ".dockerignore", ".editorconfig",
    ".svg",  # openable as text
)

# Trailing line-range suffix: :L10, :L10-L20, :L1-L5,L10-L12
_LINE_RANGE_RE = re.compile(
    r"(:L\d+(?:-L\d+)?(?:,L\d+(?:-L\d+)?)*)\s*$"
)


def is_code_path(path: str) -> bool:
    """True if path should open in the editor (vs reveal in Finder)."""
    if not path:
        return False
    if os.path.isdir(path):
        return False
    base = os.path.basename(path)
    if base in ("Makefile", "Dockerfile", "CMakeLists.txt", "Gemfile",
                "Rakefile", "Procfile", "Justfile"):
        return True
    low = path.lower()
    return any(low.endswith(e) for e in _CODE_EXTS)


def is_image_path(path: str) -> bool:
    return bool(path) and path.lower().endswith(_IMAGE_EXTS)


def split_selection_ref(ref: str) -> Tuple[str, str]:
    """Split 'path:L10-L20' → ('path', 'L10-L20'). Range may be ''."""
    if not ref:
        return "", ""
    m = _LINE_RANGE_RE.search(ref)
    if not m:
        return ref, ""
    return ref[: m.start()], m.group(1)[1:]  # drop leading ':'


def format_line_range(row_start: int, row_end: int) -> str:
    """1-based inclusive rows → 'L10' or 'L10-L20'."""
    if row_start == row_end:
        return f"L{row_start}"
    return f"L{row_start}-L{row_end}"


def first_line_of_range(line_range: str) -> Optional[int]:
    """First line number from 'L10-L20' / 'L10' / 'L1-L2,L9'."""
    if not line_range:
        return None
    m = re.match(r"L(\d+)", line_range)
    return int(m.group(1)) if m else None


class ContextItem:
    """A pending context item to attach to next query."""
    def __init__(self, kind: str, name: str, content: str, path: str = "",
                 line_range: str = ""):
        self.kind = kind  # "file" | "selection" | "folder" | "image" | "path"
        self.name = name  # Display name (includes :L… for selections)
        self.content = content  # Actual content (or __IMAGE__:mime:b64 for images)
        self.path = path  # Absolute path on disk when known
        self.line_range = line_range  # e.g. "L10-L20" for selection chips

    @property
    def open_action(self) -> str:
        """'open' in editor for code; 'reveal' in file manager otherwise."""
        if self.kind in ("folder", "image"):
            return "reveal"
        p = self.path or self.name
        if is_image_path(p):
            return "reveal"
        if is_code_path(p) or self.kind == "selection":
            return "open"
        return "reveal"


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
        abspath = os.path.abspath(os.path.expanduser(path)) if path else ""
        name = os.path.basename(abspath or path) or "file"
        self.items.append(ContextItem(
            "file", name,
            f"File: {abspath or path}\n```\n{content}\n```",
            path=abspath or path or ""))
        self._refresh_display()

    def add_selection(self, path: str, content: str) -> None:
        # path may be "file.py:L1-L10" or plain path (range optional).
        file_part, line_range = split_selection_ref(path or "")
        abspath = (
            os.path.abspath(os.path.expanduser(file_part))
            if file_part and not file_part.startswith("untitled")
            else file_part)
        base = os.path.basename(file_part) if file_part else "selection"
        # Preserve line range on the chip (and in the agent-facing header).
        name = f"{base}:{line_range}" if line_range else base
        loc = f"{abspath or file_part or path}:{line_range}" if line_range else (
            abspath or file_part or path or "selection")
        self.items.append(ContextItem(
            "selection", name,
            f"Selection from {loc}:\n```\n{content}\n```",
            path=abspath or "",
            line_range=line_range))
        self._refresh_display()

    def add_folder(self, path: str) -> None:
        abspath = os.path.abspath(os.path.expanduser(path)) if path else path
        name = os.path.basename(abspath.rstrip(os.sep)) + "/"
        self.items.append(ContextItem(
            "folder", name, f"Folder: {abspath}", path=abspath or ""))
        self._refresh_display()

    def add_path(self, path: str) -> None:
        """Add a filesystem path as context (paste / path attach).

        - directory → folder chip
        - image / binary → path ref (agent uses read_image / path; no slurp)
        - text → file with content
        """
        path = os.path.abspath(os.path.expanduser((path or "").strip()))
        if not path:
            return
        if os.path.isdir(path):
            self.add_folder(path)
            return
        if not os.path.isfile(path):
            # dangling path still useful as a ref
            self._add_path_ref(path, missing=True)
            return
        if is_image_path(path) or self._looks_binary(path):
            self._add_path_ref(path)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read(2 * 1024 * 1024)
            self.add_file(path, content)
        except (UnicodeDecodeError, OSError):
            self._add_path_ref(path)

    def _add_path_ref(self, path: str, missing: bool = False) -> None:
        name = os.path.basename(path.rstrip(os.sep)) or path
        if is_image_path(path):
            body = (
                f"Attached image path: {path}\n"
                f"(On-disk image — for vision use use_tool with "
                f"tool_name=\"sublime__read_image\" and "
                f"tool_input={{\"path\": {path!r}}}; "
                f"search_tool query=\"read_image\" if needed. "
                f"Do not use read_file — it fails on binary images.)"
            )
            kind = "path"
        elif missing:
            body = f"Attached path (not found on disk): {path}"
            kind = "path"
        else:
            body = (
                f"Attached file path: {path}\n"
                f"(Binary or non-text — open via path; do not assume UTF-8 text.)"
            )
            kind = "path"
        # Dedupe by absolute path
        for it in self.items:
            if it.path == path and it.kind in ("path", "file", "folder", "image"):
                return
        self.items.append(ContextItem(kind, name, body, path=path))
        self._refresh_display()

    @staticmethod
    def _looks_binary(path: str) -> bool:
        try:
            with open(path, "rb") as f:
                head = f.read(512)
            return b"\x00" in head
        except OSError:
            return False

    def add_image(self, image_data: bytes, mime_type: str) -> None:
        ext = (
            ".png" if "png" in (mime_type or "")
            else ".jpg" if "jpeg" in (mime_type or "") or "jpg" in (mime_type or "")
            else ".gif" if "gif" in (mime_type or "")
            else ".webp" if "webp" in (mime_type or "")
            else ".png"
        )
        mime = mime_type or {
            ".png": "image/png", ".jpg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp",
        }.get(ext, "image/png")
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(image_data)
            temp_path = f.name
        encoded = base64.b64encode(image_data).decode("utf-8")
        # Prefer basename for chips; keep absolute path for agent tools.
        name = os.path.basename(temp_path)
        # Marker: __IMAGE__:mime:b64 — path stored separately on ContextItem.path
        self.items.append(ContextItem(
            "image", name, f"__IMAGE__:{mime}:{encoded}", path=temp_path))
        print(f"[Claude] Added image to context: {name} "
              f"({len(image_data)} bytes → {temp_path})")
        self._refresh_display()

    def clear(self) -> None:
        self.items = []
        self._refresh_display()

    def remove_at(self, index: int) -> bool:
        """Remove one pending item by index. Returns True if removed."""
        if index < 0 or index >= len(self.items):
            return False
        del self.items[index]
        self._refresh_display()
        return True

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
                # __IMAGE__:mime:base64data
                _, mime_type, data = item.content.split(":", 2)
                images.append({
                    "mime_type": mime_type,
                    "data": data,
                    "path": getattr(item, "path", "") or "",
                })
            else:
                parts.append(item.content)
        parts.append(prompt)
        return "\n\n".join(parts), images

    # ── Internal ───────────────────────────────────────────────────────

    def _refresh_display(self) -> None:
        """Notify the output view that the indicator should re-render."""
        self.session.output.set_pending_context(self.items)
