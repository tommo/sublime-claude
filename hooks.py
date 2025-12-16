"""
Hook system for Claude Code plugin.
Supports pre-compact and other hooks.
"""
import os
import subprocess
from pathlib import Path
from typing import Optional, List


def get_project_hook_prompt(hook_name: str, cwd: Optional[str] = None) -> Optional[str]:
    """
    Execute a hook script and return its output.

    Args:
        hook_name: Name of the hook (e.g., "pre-compact")
        cwd: Working directory (project root)

    Returns:
        Hook output as string, or None if hook doesn't exist or fails
    """
    if not cwd:
        return None

    # Look for hook in .claude/hooks/
    hook_path = Path(cwd) / ".claude" / "hooks" / hook_name

    if not hook_path.exists():
        return None

    if not hook_path.is_file():
        print(f"[Claude] Hook {hook_name} exists but is not a file")
        return None

    # Check if executable - if so, run it as a script
    # Otherwise, just read it as a text file (simple prompt)
    if os.access(hook_path, os.X_OK):
        try:
            # Execute hook script
            print(f"[Claude] Executing hook script: {hook_name}")
            result = subprocess.run(
                [str(hook_path)],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=30  # 30 second timeout
            )

            if result.returncode != 0:
                print(f"[Claude] Hook {hook_name} failed with exit code {result.returncode}")
                if result.stderr:
                    print(f"[Claude] Hook stderr: {result.stderr}")
                return None

            output = result.stdout.strip()
            if output:
                print(f"[Claude] Hook {hook_name} output: {len(output)} chars")
                return output
            else:
                print(f"[Claude] Hook {hook_name} produced no output")
                return None

        except subprocess.TimeoutExpired:
            print(f"[Claude] Hook {hook_name} timed out after 30 seconds")
            return None
        except Exception as e:
            print(f"[Claude] Hook {hook_name} failed: {e}")
            return None
    else:
        # Not executable - treat as simple text prompt
        try:
            print(f"[Claude] Reading hook prompt: {hook_name}")
            with open(hook_path, 'r', encoding='utf-8') as f:
                output = f.read().strip()
            if output:
                print(f"[Claude] Hook {hook_name} prompt: {len(output)} chars")
                return output
            else:
                print(f"[Claude] Hook {hook_name} is empty")
                return None
        except Exception as e:
            print(f"[Claude] Failed to read hook {hook_name}: {e}")
            return None


def combine_hook_prompts(prompts: List[Optional[str]], separator: str = "\n\n---\n\n") -> Optional[str]:
    """
    Combine multiple hook prompts into one, filtering out None values.

    Args:
        prompts: List of prompt strings (can contain None values)
        separator: Separator between prompts

    Returns:
        Combined prompt string, or None if all prompts are None
    """
    valid_prompts = [p for p in prompts if p]
    if not valid_prompts:
        return None

    return separator.join(valid_prompts)
