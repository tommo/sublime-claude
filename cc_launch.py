"""Shared helpers to launch the real `claude` CLI with the plugin's MCP server
wired in — used by both the hidden-PTY engine (cc_pty_session) and the visible
terminal mode (claude_terminal_mode). Keeping the binary resolution, env loading
and MCP-config building in one place means both paths inject the same context.
"""
import json
import os
import shutil
import sys

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_claude(settings):
    """Locate the `claude` binary: settings -> PATH -> common install dirs."""
    configured = settings.get("claude_bin")
    if configured and os.path.isfile(os.path.expanduser(configured)):
        return os.path.expanduser(configured)
    found = shutil.which("claude")
    if found:
        return found
    for c in ("/usr/local/bin/claude", "/opt/homebrew/bin/claude",
              os.path.expanduser("~/.local/bin/claude")):
        if os.path.isfile(c):
            return c
    return None


def window_cwd(window):
    """Best cwd for a window: first project folder, else active file's dir,
    else a stable scratch dir (mirrors Session._cwd)."""
    if window.folders():
        return window.folders()[0]
    view = window.active_view()
    if view and view.file_name():
        return os.path.dirname(view.file_name())
    scratch = os.path.expanduser("~/.claude/scratch")
    os.makedirs(scratch, exist_ok=True)
    return scratch


def additional_dirs(window):
    """Extra working directories, same sources as Session.initialize: the
    window's secondary project folders + project-settings `claude_additional_dirs`."""
    folders = window.folders()
    dirs = list(folders[1:]) if len(folders) > 1 else []
    project_data = window.project_data() or {}
    extra = (project_data.get("settings", {}) or {}).get("claude_additional_dirs", [])
    if isinstance(extra, list):
        dirs += [os.path.expanduser(d) for d in extra]
    return dirs


def add_dir_args(window):
    """`--add-dir <dir>` argv pairs for each additional working directory."""
    args = []
    for d in additional_dirs(window):
        args += ["--add-dir", d]
    return args


def build_draft(window, max_sel_chars=2000, max_open=30):
    """Compact Sublime editor context to PTY-inject into a fresh Claude Code at
    launch. Pushes the FOCUSED thing (active file / selection) as the payload and
    only *mentions* other open files by basename — dropping `@path` for every open
    file would make Claude Code auto-attach (and read) all of them. The rest stays
    pullable on demand via the MCP editor tools (get_window_summary, read_view…).
    """
    if window is None:
        return ""
    lines = ["[Sublime editor context]"]

    folders = window.folders()
    if folders:
        head = "Project: " + folders[0]
        if len(folders) > 1:
            head += "  (+{} more)".format(len(folders) - 1)
        lines.append(head)

    av = window.active_view()
    sel_text = None
    if av and av.file_name():
        fn = av.file_name()
        sel = av.sel()[0] if len(av.sel()) else None
        if sel is not None and not sel.empty():
            r0 = av.rowcol(sel.begin())[0] + 1
            r1 = av.rowcol(sel.end())[0] + 1
            lines.append("Active: {}:L{}-L{}".format(fn, r0, r1))
            t = av.substr(sel)
            if 0 < len(t) <= max_sel_chars:
                sel_text = t
        else:
            row = (av.rowcol(av.sel()[0].begin())[0] + 1) if len(av.sel()) else 1
            lines.append("Active: @{}  (cursor L{})".format(fn, row))  # @ = attach full file

    open_files = []
    for v in window.views():
        fn = v.file_name()
        if not fn:
            continue
        s = v.settings()
        if s.get("claude_terminal") or s.get("claude_output") or s.get("order_table_view"):
            continue
        open_files.append(fn)
    if open_files:
        shown = ", ".join(os.path.basename(f) for f in open_files[:max_open])
        suffix = "  (+{} more)".format(len(open_files) - max_open) if len(open_files) > max_open else ""
        lines.append("Open files ({}): {}{}".format(len(open_files), shown, suffix))
        lines.append("(read any with the `read_view`/`get_symbols` MCP tools)")

    if sel_text is not None:
        lines.append("Selection:\n```\n" + sel_text + "\n```")

    lines.append("")  # trailing blank: user types their request after the context
    return "\n".join(lines)


def load_env(window, settings, cwd=None):
    """Custom env vars, same precedence as Session._load_env (minus profile):
    settings.env -> project claude_env -> project .claude/settings.json env."""
    env = {}
    settings_env = settings.get("env", {})
    if isinstance(settings_env, dict):
        env.update(settings_env)
    project_data = window.project_data() or {}
    project_env = (project_data.get("settings", {}) or {}).get("claude_env", {})
    if isinstance(project_env, dict):
        env.update(project_env)
    if cwd:
        p = os.path.join(cwd, ".claude", "settings.json")
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    claude_env = (json.load(f) or {}).get("env", {})
                if isinstance(claude_env, dict):
                    env.update(claude_env)
            except Exception as e:
                print("[Claude] failed to load project env: {}".format(e))
    return env


def build_sublime_mcp_config(settings, view_id, *, enable_read_image=False):
    """Inline `--mcp-config` JSON wiring the sublime MCP stdio server, scoped to
    `view_id` (the calling session's view). Returns "" to skip injection."""
    if not settings.get("pty_inject_sublime_mcp", True):
        return ""
    server = os.path.join(PLUGIN_DIR, "mcp", "server.py")
    if not os.path.exists(server):
        return ""
    py = settings.get("python_path") or sys.executable
    args = [server, "--view-id={}".format(view_id)]
    # PTY is Claude Code — default off; opt in via settings or explicit kwarg.
    if enable_read_image or settings.get("mcp_enable_read_image") is True:
        args.append("--enable-read-image")
    elif isinstance(settings.get("mcp_enable_read_image"), str):
        if settings.get("mcp_enable_read_image").strip().lower() in (
                "1", "true", "yes", "on"):
            args.append("--enable-read-image")
    return json.dumps({
        "mcpServers": {
            "sublime": {
                "type": "stdio",
                "command": py,
                "args": args,
            }
        }
    })
