"""Sublime commands for the Grok backend: login + proxy lifecycle.

Loaded by Sublime at plugin import. Lives outside grok_backend.py so the
manager logic stays importable without sublime_plugin.
"""
import os
import subprocess
import threading

import sublime
import sublime_plugin

from . import grok_backend


def _plugin_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _log_view(window, name="Grok"):
    view = window.find_output_panel(name)
    if view is None:
        view = window.create_output_panel(name)
    return view


def _append(view, text):
    def _do():
        view.run_command("append", {"characters": text})
    sublime.set_timeout(_do, 0)


class GrokLoginCommand(sublime_plugin.WindowCommand):
    """Run the xAI OAuth login in a background thread, streaming output to a panel.

    The browser opens automatically; complete authorization there. For headless
    / SSH environments where the browser can't reach localhost, run
    `python -m grok_proxy --login` from a real terminal instead.
    """

    def run(self):
        view = _log_view(self.window, "Grok")
        self.window.run_command("show_panel", {"panel": "output.Grok"})
        _append(view, "[grok] starting xAI login…\n")
        threading.Thread(target=self._worker, args=(view,), daemon=True).start()

    def _worker(self, view):
        py = grok_backend.python_path()
        data = grok_backend.data_dir()
        base = grok_backend.base_url()
        cmd = [py, "-m", "grok_proxy", "--login", "--data-dir", data, "--base-url", base]
        try:
            proc = subprocess.Popen(
                cmd, cwd=_plugin_dir(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, bufsize=1, text=True,
            )
        except FileNotFoundError:
            _append(view, "[grok] python not found: %s\n" % py)
            return
        for line in proc.stdout:
            _append(view, line if line.endswith("\n") else line + "\n")
        rc = proc.wait()
        if rc == 0 and grok_backend.credential_exists():
            _append(view, "[grok] login complete — Grok is now available in the backend picker.\n")
        else:
            _append(view, "[grok] login did not complete (exit %s).\n" % rc)


class GrokStopProxyCommand(sublime_plugin.WindowCommand):
    """Stop the running grok_proxy subprocess."""

    def run(self):
        stopped = grok_backend.GrokProxyManager().stop()
        msg = "Grok proxy stopped." if stopped else "Grok proxy was not running."
        sublime.status_message(msg)


class GrokStartProxyCommand(sublime_plugin.WindowCommand):
    """Start the grok_proxy subprocess without launching a session (debug helper)."""

    def run(self):
        mgr = grok_backend.GrokProxyManager()
        if not grok_backend.credential_exists():
            sublime.message_dialog("Grok is not logged in. Run 'Claude: Grok Login' first.")
            return
        ok = mgr.ensure_running(wait=5.0)
        sublime.status_message(
            "Grok proxy on :%d (%s)" % (mgr.port, "up" if ok else "failed"))


class GrokCcStartCommand(sublime_plugin.WindowCommand):
    """Start Grok via Claude Code bridge + Anthropic-compat proxy (legacy).

    Prefer 'Grok: New Session' (native ACP) for Grok Build. This path reuses
    the Claude agent SDK pointed at the bundled grok_proxy.
    """

    def run(self):
        if not grok_backend.credential_exists():
            # Reuse the official grok CLI's login if present (same OAuth client_id).
            if grok_backend.grok_cli_auth_exists():
                if grok_backend.import_grok_cli_auth():
                    sublime.status_message("Grok: imported login from ~/.grok/auth.json")
                else:
                    sublime.error_message(
                        "Could not import ~/.grok/auth.json. Run 'Claude: Grok Login'.")
                    return
            else:
                sublime.error_message(
                    "Grok is not logged in.\n\n"
                    "Run 'Claude: Grok Login' from the Command Palette, or log in "
                    "with the `grok` CLI, then start a session.")
                return
        from .core import create_session
        create_session(self.window, backend="grok_cc")

    def is_visible(self):
        # Always visible so the login-prompt path is reachable pre-login.
        return True


class GrokImportCliAuthCommand(sublime_plugin.WindowCommand):
    """Import the official grok CLI's OAuth login into the bundled proxy."""

    def run(self):
        if not grok_backend.grok_cli_auth_exists():
            sublime.error_message(
                "No grok CLI auth found at ~/.grok/auth.json.\n\n"
                "Log in with the `grok` CLI first, or use 'Claude: Grok Login'.")
            return
        if grok_backend.import_grok_cli_auth():
            sublime.status_message("Grok: imported login from ~/.grok/auth.json")
        else:
            sublime.error_message("Failed to import ~/.grok/auth.json.")
