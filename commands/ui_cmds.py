"""Claude Code commands for Sublime Text."""
import os
import sublime
import sublime_plugin
import platform

from ..core import get_active_session, get_session_for_view, create_session
from ..session import Session, load_saved_sessions, load_bookmarks, toggle_bookmark
from ..prompt_builder import PromptBuilder
from ..command_parser import CommandParser
from .. import backends

# Fallback model lists per backend (used when no cache/settings available).
# Snapshot of built-ins at import time; custom providers are looked up live via
# backends.get(backend).default_models in ClaudeSelectModelCommand._get_models.
DEFAULT_MODELS = backends.default_models_dict()


class ClaudeCodeClearCommand(sublime_plugin.WindowCommand):
    def run(self) -> None:
        s = get_active_session(self.window)
        if s:
            s.output.clear()
            # Refresh status bar so loop banner / context tokens remain visible
            s._update_status_bar()
            # Re-render the pending-context indicator if any (Session-level state
            # survives clear, but the view region was reset and needs re-write)
            if s.context.items:
                s.output.set_pending_context(s.context.items)


class ClaudeCodeCopyCommand(sublime_plugin.WindowCommand):
    """Copy entire conversation to clipboard."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if s and s.output.view and s.output.view.is_valid():
            content = s.output.view.substr(sublime.Region(0, s.output.view.size()))
            sublime.set_clipboard(content)
            sublime.status_message("Conversation copied to clipboard")


class ClaudeCodeSaveCheckpointCommand(sublime_plugin.WindowCommand):
    """Save current session as a named checkpoint for future forking."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if not s or not s.session_id:
            sublime.status_message("No active session with ID to checkpoint")
            return

        def on_done(name: str) -> None:
            name = name.strip()
            if not name:
                return

            from ..mcp_server import _save_checkpoint
            if _save_checkpoint(name, s.session_id, s.name or "Checkpoint"):
                sublime.status_message(f"Checkpoint '{name}' saved")
            else:
                sublime.error_message(f"Failed to save checkpoint '{name}'")

        default_name = (s.name or "checkpoint").lower().replace(" ", "-")[:20]
        self.window.show_input_panel("Checkpoint name:", default_name, on_done, None, None)


class ClaudeCodeUsageCommand(sublime_plugin.WindowCommand):
    """Show API usage statistics."""
    def run(self) -> None:
        # Get current session usage
        s = get_active_session(self.window)
        current_usage = []
        if s:
            current_usage = [
                f"## Current Session: {s.name}",
                f"",
                f"Queries: {s.query_count}",
                f"Total Cost: ${s.total_cost:.4f}",
                f"",
            ]

        # Get all saved sessions usage
        sessions = load_saved_sessions()
        total_cost = sum(sess.get("total_cost", 0) for sess in sessions)
        total_queries = sum(sess.get("query_count", 0) for sess in sessions)

        lines = [
            "# API Usage Statistics",
            "",
            f"Total (All Sessions): ${total_cost:.4f} ({total_queries} queries)",
            "",
        ]

        if current_usage:
            lines.extend(current_usage)

        if sessions:
            lines.extend([
                "## Recent Sessions",
                ""
            ])
            for sess in sessions[:10]:  # Show last 10
                name = sess.get("name", "Untitled")
                cost = sess.get("total_cost", 0)
                queries = sess.get("query_count", 0)
                lines.append(f"- {name}: ${cost:.4f} ({queries} queries)")

        # Show in quick panel with monospace font
        content = "\n".join(lines)

        # Create a new output panel to show usage
        panel = self.window.create_output_panel("claude_usage")
        panel.set_read_only(False)
        panel.run_command("append", {"characters": content})
        panel.set_read_only(True)
        panel.settings().set("word_wrap", False)
        panel.settings().set("gutter", False)
        self.window.run_command("show_panel", {"panel": "output.claude_usage"})


class ClaudeSearchSessionsCommand(sublime_plugin.WindowCommand):
    """Search all Claude sessions by title/summary."""
    def run(self) -> None:
        self.window.show_input_panel("Search sessions:", "", self._on_done, None, None)

    def _on_done(self, query: str) -> None:
        if not query.strip():
            return
        import threading
        q = query.lower()

        def search():
            import os, json, time
            from ..session import load_saved_sessions

            # Build lookup of sublime-claude session names by session_id
            saved = {s["session_id"]: s.get("name", "") for s in load_saved_sessions() if s.get("session_id")}

            projects_dir = os.path.expanduser("~/.claude/projects")
            results = []  # [(session_id, title, mtime, proj_key)]
            if not os.path.isdir(projects_dir):
                return

            for proj_key in os.listdir(projects_dir):
                proj_path = os.path.join(projects_dir, proj_key)
                if not os.path.isdir(proj_path):
                    continue
                for fname in os.listdir(proj_path):
                    if not fname.endswith(".jsonl"):
                        continue
                    fpath = os.path.join(proj_path, fname)
                    sid = fname[:-6]  # strip .jsonl
                    # Check sublime-claude saved name first
                    saved_name = saved.get(sid, "")
                    # Read first few lines to find JSONL title
                    jsonl_title = None
                    try:
                        with open(fpath, "r") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                entry = json.loads(line)
                                if entry.get("type") == "custom-title":
                                    jsonl_title = entry.get("title", "")
                                    break
                                # First real user prompt as fallback
                                if entry.get("type") == "user" and not entry.get("isSidechain"):
                                    msg = entry.get("message", {})
                                    content = msg.get("content", [])
                                    if isinstance(content, list):
                                        has_tool_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                                        if has_tool_result:
                                            continue
                                        for b in content:
                                            if isinstance(b, dict) and b.get("type") == "text":
                                                t = b.get("text", "")
                                                if t and not t.startswith("[Request interrupted"):
                                                    jsonl_title = t[:80]
                                                    break
                                    elif isinstance(content, str) and not content.startswith("[Request interrupted"):
                                        jsonl_title = content[:80]
                                    if jsonl_title:
                                        break
                    except Exception:
                        continue
                    # Match against both saved name and JSONL title
                    searchable = f"{saved_name} {jsonl_title or ''}".lower()
                    if q not in searchable:
                        continue
                    # Use saved name as display title if available
                    title = saved_name or jsonl_title or "untitled"
                    mtime = os.path.getmtime(fpath)
                    results.append((sid, title, mtime, proj_key))

            results.sort(key=lambda x: x[2], reverse=True)
            results = results[:50]

            if not results:
                sublime.set_timeout(lambda: sublime.status_message(f"No sessions matching '{query}'"), 0)
                return

            items = []
            for sid, title, mtime, proj_key in results:
                ts = time.strftime("%m/%d %H:%M", time.localtime(mtime))
                proj_short = proj_key.rsplit("-", 1)[-1] if "-" in proj_key else proj_key
                items.append([title, f"{proj_short} | {ts} | {sid[:8]}..."])

            def show_panel():
                from ..core import create_session

                def on_select(idx):
                    if idx < 0:
                        return
                    sid = results[idx][0]
                    # Look up backend from saved sessions
                    saved_backend = "claude"
                    for saved in load_saved_sessions():
                        if saved.get("session_id") == sid:
                            saved_backend = saved.get("backend", "claude")
                            break
                    create_session(self.window, resume_id=sid, fork=True, backend=saved_backend)

                self.window.show_quick_panel(items, on_select)

            sublime.set_timeout(show_panel, 0)

        threading.Thread(target=search, daemon=True).start()


class ClaudeCodeViewHistoryCommand(sublime_plugin.WindowCommand):
    """View session history from Claude's stored conversation."""
    def run(self) -> None:
        import os
        from ..session import load_saved_sessions
        sessions = load_saved_sessions()
        if not sessions:
            sublime.status_message("No saved sessions")
            return

        # Build quick panel items
        items = []
        for s in sessions:
            name = (s.get("name") or "Unnamed")[:40]
            sid = (s.get("session_id") or "")[:8]
            cost = s.get("total_cost") or 0
            queries = s.get("query_count") or 0
            project = os.path.basename(s.get("project") or "")
            items.append([f"{name}", f"{project} | {queries} queries | ${cost:.2f} | {sid}..."])

        def on_select(idx: int) -> None:
            if idx < 0:
                return
            session = sessions[idx]
            self._show_history(session)

        self.window.show_quick_panel(items, on_select)

    def _show_history(self, session: dict) -> None:
        """Extract and display user messages from session history."""
        import json, os

        sid = session.get("session_id", "")
        project = session.get("project", "")
        # Convert project path to Claude's format
        project_key = project.replace("/", "-").lstrip("-")
        history_file = os.path.expanduser(f"~/.claude/projects/{project_key}/{sid}.jsonl")

        if not os.path.exists(history_file):
            sublime.status_message(f"History file not found: {history_file}")
            return

        # Extract user messages
        messages = []
        with open(history_file, "r") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if d.get("type") == "user":
                        msg = d.get("message", {})
                        content = msg.get("content", [])
                        if isinstance(content, str):
                            messages.append(content)
                        elif isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text = c.get("text", "")
                                    if text and not text.startswith("[Request interrupted"):
                                        messages.append(text)
                except:
                    pass

        # Create output view
        view = self.window.new_file()
        view.set_name(f"History: {session.get('name', sid[:8])}")
        view.set_scratch(True)
        view.assign_syntax("Packages/Markdown/Markdown.sublime-syntax")

        # Format output
        output = f"# Session: {session.get('name', 'Unnamed')}\n"
        output += f"**ID:** {sid}\n"
        output += f"**Project:** {project}\n"
        output += f"**Queries:** {session.get('query_count', 0)} | **Cost:** ${session.get('total_cost', 0):.2f}\n\n"
        output += "---\n\n"

        for i, msg in enumerate(messages, 1):
            output += f"## [{i}]\n{msg}\n\n"

        view.run_command("append", {"characters": output})


class ClaudeCodeResetInputCommand(sublime_plugin.WindowCommand):
    """Force reset input mode state when it gets corrupted."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if s:
            s.output.reset_input_mode()
            sublime.status_message("Input mode reset")



# --- recovered classes dropped by split ---
class ClaudeGarageSearchCommand(sublime_plugin.WindowCommand):
    """Search indexed sessions with garage CLI and fork/resume."""

    def run(self) -> None:
        self.window.show_input_panel(
            "Search sessions:",
            "",
            self._on_query,
            None,
            None
        )

    def _on_query(self, query: str) -> None:
        if not query.strip():
            return

        import subprocess
        try:
            result = subprocess.run(
                ["garage", "search", query, "--k", "10"],
                capture_output=True,
                text=True,
                timeout=10
            )
            # garage may crash partway through but still output useful results
            # so we parse stdout regardless of return code
            if result.stdout.strip():
                self._parse_and_show(result.stdout, query)
            elif result.returncode != 0:
                sublime.error_message(f"garage search failed: {result.stderr}")
        except FileNotFoundError:
            sublime.error_message("garage CLI not found. Install it first.")
        except subprocess.TimeoutExpired:
            sublime.error_message("garage search timed out")

    def _parse_and_show(self, output: str, query: str) -> None:
        """Parse garage search output and show quick panel."""
        import re
        # New format: 1. [0.696] 2ccb865b  [pil]  Turns: 268
        #                - Summary text here...
        # Old format: 1. [0.610] f400b570
        #                Project: /path/to/project
        #                Created: 2026-01-17T02:38:12.192Z  Turns: 41
        results = []
        lines = output.strip().split("\n")
        i = 0
        while i < len(lines):
            # Try new format: 1. [0.696] 2ccb865b  [pil]  Turns: 268
            #                   - Summary...
            #                   ID: full-uuid-here
            new_match = re.match(r'\d+\.\s+\[([0-9.]+)\]\s+([a-f0-9]+)\s+\[([^\]]+)\]\s+Turns:\s*(\d+)', lines[i])
            if new_match:
                score = float(new_match.group(1))
                short_id = new_match.group(2)
                project = new_match.group(3)
                turns = int(new_match.group(4))
                summary = ""
                full_id = short_id  # Default to short if full not found
                # Parse following lines for summary and full ID
                while i + 1 < len(lines) and not re.match(r'\d+\.', lines[i + 1]):
                    i += 1
                    line = lines[i].strip()
                    if line.startswith("- "):
                        summary = line[2:]  # Remove "- " prefix
                    elif line.startswith("ID: "):
                        full_id = line[4:]  # Full UUID
                results.append({
                    "session_id": full_id,
                    "short_id": short_id,
                    "score": score,
                    "project": project,
                    "turns": turns,
                    "summary": summary,
                })
                i += 1
                continue

            # Try old format: 1. [0.610] f400b570
            old_match = re.match(r'\d+\.\s+\[([0-9.]+)\]\s+([a-f0-9]+)', lines[i])
            if old_match:
                score = float(old_match.group(1))
                session_id = old_match.group(2)
                project = ""
                turns = 0
                summary = ""
                # Parse following lines for metadata
                while i + 1 < len(lines) and not re.match(r'\d+\.', lines[i + 1]):
                    i += 1
                    line = lines[i].strip()
                    if line.startswith("Project:"):
                        project = line.replace("Project:", "").strip()
                    elif line.startswith("Created:"):
                        if "Turns:" in line:
                            turns = int(line.split("Turns:")[-1].strip())
                results.append({
                    "session_id": session_id,
                    "score": score,
                    "project": project,
                    "turns": turns,
                    "summary": summary,
                })
            i += 1

        if not results:
            sublime.status_message("No sessions found")
            return

        # Build quick panel items
        items = []
        for r in results:
            import os
            proj_name = os.path.basename(r["project"]) if r["project"] else r["project"]
            summary = r.get("summary", "")
            if len(summary) > 80:
                summary = summary[:77] + "..."
            short_id = r.get("short_id", r["session_id"][:8])
            items.append([
                f"[{r['score']:.2f}] {short_id}  [{proj_name}]  {r['turns']} turns",
                summary or "(no summary)"
            ])

        def on_select(idx):
            if idx >= 0:
                self._show_action_panel(results[idx])

        self.window.show_quick_panel(items, on_select, placeholder=f"Results for: {query}")

    def _show_action_panel(self, result: dict) -> None:
        """Show fork/resume options for selected session."""
        session_id = result["session_id"]  # Full UUID
        short_id = result.get("short_id", session_id[:8])
        # Look up backend from saved sessions
        src_backend = "claude"
        for saved in load_saved_sessions():
            if saved.get("session_id") == session_id:
                src_backend = saved.get("backend", "claude")
                break

        items = [
            ["Fork", f"Create new session branching from {short_id}"],
            ["Resume", f"Continue session {short_id} (same ID)"],
        ]

        def on_action(idx):
            if idx == 0:
                # Fork
                s = create_session(self.window, resume_id=session_id, fork=True, backend=src_backend)
                s.name = f"fork:{short_id}"
                s.output.set_name(s.name)
                sublime.status_message(f"Forked session {short_id}")
            elif idx == 1:
                # Resume
                s = create_session(self.window, resume_id=session_id, fork=False, backend=src_backend)
                s.name = f"resume:{short_id}"
                s.output.set_name(s.name)
                sublime.status_message(f"Resumed session {short_id}")

        self.window.show_quick_panel(items, on_action)

class ClaudeCodeAddMcpCommand(sublime_plugin.WindowCommand):
    """Add MCP tools config to project."""
    def run(self) -> None:
        import os
        import json

        folders = self.window.folders()
        if not folders:
            sublime.status_message("No project folder open")
            return

        project_root = folders[0]
        claude_dir = os.path.join(project_root, ".claude")
        settings_path = os.path.join(claude_dir, "settings.json")
        tools_dir = os.path.join(claude_dir, "sublime_tools")

        os.makedirs(claude_dir, exist_ok=True)
        os.makedirs(tools_dir, exist_ok=True)

        plugin_dir = os.path.dirname(os.path.dirname(__file__))  # package root
        mcp_server = os.path.join(plugin_dir, "mcp", "server.py")

        settings = {}
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r") as f:
                    settings = json.load(f)
            except:
                pass

        if "mcpServers" not in settings:
            settings["mcpServers"] = {}

        settings["mcpServers"]["sublime"] = {
            "command": "python3",
            "args": [mcp_server]
        }

        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)

        example_tool = os.path.join(tools_dir, "example.py")
        if not os.path.exists(example_tool):
            with open(example_tool, "w") as f:
                f.write('''# Example sublime tool
# Run with: sublime_eval(tool="example")

window = sublime.active_window()
view = window.active_view()

return {
    "file": view.file_name() if view else None,
    "selection": view.substr(view.sel()[0]) if view and view.sel() else None,
    "cursor": view.rowcol(view.sel()[0].begin()) if view and view.sel() else None,
}
''')

        sublime.status_message(f"MCP config added to {claude_dir}")
        self.window.open_file(settings_path)

class ClaudeCodeTogglePermissionModeCommand(sublime_plugin.WindowCommand):
    """Toggle between permission modes."""

    MODES = ["default", "acceptEdits", "auto", "dontAsk", "bypassPermissions"]
    MODE_LABELS = {
        "default": "Default (prompt for all)",
        "acceptEdits": "Accept Edits (auto-approve file ops)",
        "auto": "Auto (classifier: run safe ops, ask on risky, block exfil)",
        "dontAsk": "Don't Ask (never prompt; deny if not pre-approved)",
        "bypassPermissions": "Bypass (allow ALL - use with caution)",
    }

    def run(self):
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        current = settings.get("permission_mode", "default")

        items = []
        current_idx = 0
        for i, mode in enumerate(self.MODES):
            label = self.MODE_LABELS[mode]
            if mode == current:
                label = f"● {label}"
                current_idx = i
            else:
                label = f"  {label}"
            items.append(label)

        def on_select(idx):
            if idx >= 0:
                new_mode = self.MODES[idx]
                settings.set("permission_mode", new_mode)
                sublime.save_settings("ClaudeCode.sublime-settings")
                sublime.status_message(f"Claude: permission mode = {new_mode}")

                s = get_active_session(self.window)
                if s:
                    s.permission_mode = new_mode
                    if s.client:
                        s.client.send("set_permission_mode", {"mode": new_mode})
                    if hasattr(s, "_update_permission_banner"):
                        s._update_permission_banner(show=True)

        self.window.show_quick_panel(items, on_select, selected_index=current_idx)


# --- Input Mode Commands ---
