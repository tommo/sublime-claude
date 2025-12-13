"""Prompt building utilities - self-contained unit for constructing prompts."""
from typing import List, Optional


class PromptBuilder:
    """Builder for constructing Claude prompts with context."""

    def __init__(self, base_prompt: str = ""):
        self.base_prompt = base_prompt
        self.parts: List[str] = []
        if base_prompt:
            self.parts.append(base_prompt)

    def add_text(self, text: str) -> 'PromptBuilder':
        """Add plain text to the prompt."""
        self.parts.append(text)
        return self

    def add_file(self, path: str, content: str) -> 'PromptBuilder':
        """Add a file with syntax highlighting."""
        self.parts.append(f"\n\nFile: `{path}`\n```\n{content}\n```")
        return self

    def add_selection(self, path: str, content: str) -> 'PromptBuilder':
        """Add a code selection."""
        self.parts.append(f"\n\nSelection from {path}:\n```\n{content}\n```")
        return self

    def add_folder(self, path: str) -> 'PromptBuilder':
        """Add a folder reference."""
        self.parts.append(f"\n\nFolder: {path}")
        return self

    def add_context_items(self, items: List) -> 'PromptBuilder':
        """Add multiple context items (ContextItem objects)."""
        for item in items:
            self.parts.append(f"\n\n{item.content}")
        return self

    def build(self) -> str:
        """Build the final prompt string."""
        return "".join(self.parts)

    @staticmethod
    def file_query(prompt: str, file_path: str, content: str) -> str:
        """Quick builder for file query pattern."""
        return PromptBuilder(prompt).add_file(file_path, content).build()

    @staticmethod
    def selection_query(prompt: str, file_path: str, selection: str) -> str:
        """Quick builder for selection query pattern."""
        return PromptBuilder(prompt).add_selection(file_path, selection).build()

    @staticmethod
    def with_context(prompt: str, context_items: List) -> str:
        """Quick builder for prompt with context items."""
        builder = PromptBuilder()
        for item in context_items:
            builder.parts.append(item.content)
        if prompt:
            builder.parts.append(f"\n\n{prompt}")
        return builder.build()


def format_code_block(content: str, language: str = "") -> str:
    """Format content as a markdown code block."""
    return f"```{language}\n{content}\n```"


def format_file_reference(path: str, content: Optional[str] = None) -> str:
    """Format a file reference with optional content."""
    if content:
        return f"File: `{path}`\n{format_code_block(content)}"
    return f"File: `{path}`"


def indent_lines(text: str, indent: str = "  ") -> str:
    """Indent all lines except the first."""
    lines = text.split("\n")
    if len(lines) <= 1:
        return text
    return lines[0] + "\n" + "\n".join(indent + line for line in lines[1:])
