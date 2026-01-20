#!/usr/bin/env python3
"""PreCompact hook - outputs retained context for compaction summary.

Reads:
  1. Static: .claude/RETAIN.md (per-project)
  2. Dynamic: .claude/sessions/{session_id}_retain.md (per-session)

Outputs combined content to stdout (included in compaction).
"""
import sys
import os
import json


def main():
    # Read hook input from stdin
    try:
        input_data = json.load(sys.stdin)
    except:
        input_data = {}

    session_id = input_data.get("session_id", "")
    cwd = os.getcwd()

    output_parts = []

    # 1. Static retain file (.claude/RETAIN.md)
    static_path = os.path.join(cwd, ".claude", "RETAIN.md")
    if os.path.exists(static_path):
        try:
            with open(static_path, "r") as f:
                content = f.read().strip()
            if content:
                output_parts.append(f"# Retained Context (Project)\n\n{content}")
        except Exception as e:
            print(f"[pre_compact] Error reading {static_path}: {e}", file=sys.stderr)

    # 2. Sublime project retain (synced from .sublime-project settings.claude_retain)
    sublime_retain_path = os.path.join(cwd, ".claude", "sublime_project_retain.md")
    if os.path.exists(sublime_retain_path):
        try:
            with open(sublime_retain_path, "r") as f:
                content = f.read().strip()
            if content:
                output_parts.append(f"# Retained Context (Sublime Project)\n\n{content}")
        except Exception as e:
            print(f"[pre_compact] Error reading {sublime_retain_path}: {e}", file=sys.stderr)

    # 3. Dynamic session retain file
    if session_id:
        dynamic_path = os.path.join(cwd, ".claude", "sessions", f"{session_id}_retain.md")
        if os.path.exists(dynamic_path):
            try:
                with open(dynamic_path, "r") as f:
                    content = f.read().strip()
                if content:
                    output_parts.append(f"# Retained Context (Session)\n\n{content}")
            except Exception as e:
                print(f"[pre_compact] Error reading {dynamic_path}: {e}", file=sys.stderr)

    # Output combined content
    if output_parts:
        print("\n\n---\n\n".join(output_parts))


if __name__ == "__main__":
    main()
