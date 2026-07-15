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


class ClaudeCodeStartCommand(sublime_plugin.WindowCommand):
    """Start a new session. Shows profile picker if profiles are configured."""
    def run(self, profile: str = None, persona_id: int = None, backend: str = None) -> None:
        from ..settings import load_profiles_and_checkpoints, load_project_settings
        import os

        # Default backend: official Claude unless the user set a different
        # default via 'Claude: Set Default Provider'. Explicit arg/command wins.
        if backend is None:
            backend = sublime.load_settings("ClaudeCode.sublime-settings").get("default_backend", "claude")

        # If persona_id specified, acquire and start
        if persona_id:
            self._start_with_persona(persona_id)
            return

        # Get project profiles path
        project_path = None
        cwd = None
        if self.window.folders():
            cwd = self.window.folders()[0]
            project_path = os.path.join(cwd, ".claude", "profiles.json")

        profiles, checkpoints = load_profiles_and_checkpoints(project_path)
        settings = load_project_settings(cwd)

        # If profile specified directly, use it
        if profile:
            profile_config = profiles.get(profile, {})
            create_session(self.window, profile=profile_config, backend=backend)
            return

        # Build options list
        options = []

        # Default option (always available). Surface which provider/model the
        # default resolves to, so 'New Session' isn't a silent surprise when
        # default_backend is set to a non-Claude provider.
        _def_spec = backends.get(backend)
        _def_label = _def_spec.label or backend
        _def_model = (sublime.load_settings("ClaudeCode.sublime-settings")
                      .get("default_models", {}) or {}).get(backend) or _def_spec.fallback_model
        if backend == "claude":
            _def_detail = "Start fresh with default settings"
        else:
            _def_model_str = " · {}".format(_def_model) if _def_model else ""
            _def_detail = "Default provider: {}{}".format(_def_label, _def_model_str)
        options.append(("default", None, "🆕 New Session", _def_detail))

        # Personas - get URL from sublime settings
        sublime_settings = sublime.load_settings("ClaudeCode.sublime-settings")
        persona_url = sublime_settings.get("persona_url", "http://localhost:5002/personas")
        options.append(("persona", persona_url, "👤 From Persona...", "Acquire a persona identity"))

        # Profiles
        for name, config in profiles.items():
            desc = config.get("description", f"{config.get('model', 'default')} model")
            options.append(("profile", name, f"📋 {name}", desc))

        # Checkpoints (Claude-only, session IDs are backend-specific)
        if backend == "claude":
            for name, config in checkpoints.items():
                desc = config.get("description", "Saved checkpoint")
                options.append(("checkpoint", name, f"📍 {name}", desc))

        if len(options) == 1:
            # Only default, just start
            create_session(self.window, backend=backend)
            return

        # Show quick panel
        items = [[opt[2], opt[3]] for opt in options]

        def on_select(idx):
            if idx < 0:
                return
            opt_type, opt_name, _, _ = options[idx]
            if opt_type == "default":
                create_session(self.window, backend=backend)
            elif opt_type == "persona":
                self._show_persona_picker(opt_name, backend=backend)  # opt_name contains the URL
            elif opt_type == "profile":
                profile_config = profiles.get(opt_name, {})
                create_session(self.window, profile=profile_config, backend=backend)
            elif opt_type == "checkpoint":
                checkpoint = checkpoints.get(opt_name, {})
                session_id = checkpoint.get("session_id")
                if session_id:
                    create_session(self.window, resume_id=session_id, fork=True, backend=backend)
                else:
                    sublime.error_message(f"Checkpoint '{opt_name}' has no session_id")

        self.window.show_quick_panel(items, on_select)

    def _show_persona_picker(self, persona_url: str, backend: str = "claude") -> None:
        """Show list of personas to pick from."""
        from .. import persona_client
        import threading

        def fetch_and_show():
            personas = persona_client.list_personas(persona_url)
            if not personas:
                sublime.set_timeout(lambda: sublime.status_message("No personas available"), 0)
                return

            # Build options: unlocked first, then locked
            unlocked = [p for p in personas if not p.get("is_locked")]
            locked = [p for p in personas if p.get("is_locked")]

            options = []
            for p in unlocked:
                tags = ", ".join(p.get("tags", [])) if p.get("tags") else ""
                desc = p.get("notes", tags) or "No description"
                options.append((p["id"], f"👤 {p['alias']}", desc[:60]))

            for p in locked:
                locked_by = p.get("locked_by_session", "unknown")
                options.append((p["id"], f"🔒 {p['alias']}", f"Locked by {locked_by}"))

            def show_panel():
                items = [[opt[1], opt[2]] for opt in options]

                def on_select(idx):
                    if idx < 0:
                        return
                    persona_id = options[idx][0]
                    self._start_with_persona(persona_id, persona_url, backend=backend)

                self.window.show_quick_panel(items, on_select)

            sublime.set_timeout(show_panel, 0)

        threading.Thread(target=fetch_and_show, daemon=True).start()

    def _start_with_persona(self, persona_id: int, persona_url: str = None, backend: str = "claude") -> None:
        """Acquire persona and start session."""
        from .. import persona_client
        from ..settings import load_project_settings
        import threading
        import os

        if not persona_url:
            cwd = self.window.folders()[0] if self.window.folders() else None
            settings = load_project_settings(cwd)
            persona_url = settings.get("persona_url")

        if not persona_url:
            sublime.error_message("persona_url not configured in settings")
            return

        def acquire_and_start():
            # Generate session ID for locking
            import uuid
            session_id = f"sublime-{uuid.uuid4().hex[:8]}"

            result = persona_client.acquire_persona(session_id, persona_id=persona_id, base_url=persona_url)

            if "error" in result:
                sublime.set_timeout(
                    lambda: sublime.error_message(f"Failed to acquire persona: {result['error']}"),
                    0
                )
                return

            persona = result.get("persona", {})
            ability = result.get("ability", {})
            handoff_notes = result.get("handoff_notes")

            # Build profile config from persona (fallback to persona-level fields if ability empty)
            profile_config = {
                "model": ability.get("model") or persona.get("model") or "sonnet",
                "system_prompt": ability.get("system_prompt") or persona.get("system_prompt") or "",
                "persona_id": persona_id,
                "persona_session_id": session_id,
                "persona_url": persona_url,
                "description": f"Persona: {persona.get('alias', 'unknown')}"
            }

            def start():
                s = create_session(self.window, profile=profile_config, backend=backend)
                # Show handoff notes if present
                if handoff_notes:
                    s.output.text(f"\n*Handoff notes:* {handoff_notes}\n")
                sublime.status_message(f"Acquired persona: {persona.get('alias', 'unknown')}")

            sublime.set_timeout(start, 0)

        threading.Thread(target=acquire_and_start, daemon=True).start()


class CodexStartCommand(sublime_plugin.WindowCommand):
    """Start a new Codex session."""
    def run(self) -> None:
        import shutil
        if not shutil.which("codex"):
            sublime.error_message("Codex CLI not found. Install from: https://github.com/openai/codex")
            return
        create_session(self.window, backend="codex")


class CopilotStartCommand(sublime_plugin.WindowCommand):
    """Start a new GitHub Copilot session."""
    def run(self) -> None:
        # SDK check runs in bridge subprocess (Python 3.11+), not Sublime's 3.8
        # Just check if the bridge script exists
        copilot_bridge = os.path.join(os.path.dirname(__file__), "bridge", "copilot_main.py")
        if not os.path.exists(copilot_bridge):
            sublime.error_message("Copilot bridge not found")
            return
        create_session(self.window, backend="copilot")


class DeepSeekStartCommand(sublime_plugin.WindowCommand):
    """Start a new DeepSeek session (Anthropic-compatible endpoint).

    DeepSeek ships as a seeded custom_provider (see ClaudeCode.sublime-settings
    `custom_providers.deepseek`); this command is kept for muscle-memory / existing
    key bindings. Configure its key/base URL via 'Claude: Manage Anthropic Providers'.
    """
    def run(self) -> None:
        if not backends.is_available("deepseek"):
            sublime.error_message(
                "DeepSeek provider not configured. Open 'Claude: Manage Anthropic Providers' "
                "and set the deepseek entry's base_url + auth_token (or auth_env_var).")
            return
        create_session(self.window, backend="deepseek")


class PiStartCommand(sublime_plugin.WindowCommand):
    """Start a new Pi session."""
    def run(self) -> None:
        import shutil
        # Check bun global install first
        bun_pi = os.path.expanduser("~/.bun/install/global/node_modules/.bin/pi")
        if not os.path.isfile(bun_pi) and not shutil.which("pi"):
            sublime.error_message(
                "Pi CLI not found.\n\n"
                "Install: npm install -g @earendil-works/pi-coding-agent\n\n"
                "Then authenticate with: pi (and follow /login)"
            )
            return
        create_session(self.window, backend="pi")


class DsrStartCommand(sublime_plugin.WindowCommand):
    """Start a new DSR (dsr coding agent) session."""
    def run(self) -> None:
        import shutil
        if not os.environ.get("DSR_BIN") and not shutil.which("dsr"):
            sublime.error_message(
                "dsr CLI not found.\n\n"
                "Install dsr and put it in PATH, or set the DSR_BIN environment variable."
            )
            return
        create_session(self.window, backend="dsr")


class GrokStartCommand(sublime_plugin.WindowCommand):
    """Start a native Grok Build session via ACP (`grok agent stdio`)."""
    def run(self) -> None:
        import shutil
        if not os.environ.get("GROK_BIN") and not shutil.which("grok"):
            sublime.error_message(
                "grok CLI not found.\n\n"
                "Install Grok Build and put `grok` in PATH, or set GROK_BIN.\n"
                "Then run `grok login` once."
            )
            return
        create_session(self.window, backend="grok")


class ClaudeCodeQueryCommand(sublime_plugin.WindowCommand):
    """Open input for query (focuses output and enters input mode)."""
    def run(self) -> None:
        s = get_active_session(self.window) or create_session(self.window)
        s.output.show()
        s._enter_input_with_draft()


class ClaudeCodeRestartCommand(sublime_plugin.WindowCommand):
    """Restart session, keeping the output view."""
    def run(self) -> None:

        old_session = get_active_session(self.window)
        old_view = None
        backend = None
        profile = None

        if old_session:
            old_view = old_session.output.view
            backend = old_session.backend
            profile = old_session.profile
            old_session.stop()
            if old_view and old_view.id() in sublime._claude_sessions:
                del sublime._claude_sessions[old_view.id()]

        # Create new session
        if backend is None:
            backend = sublime.load_settings("ClaudeCode.sublime-settings").get("default_backend", "claude")
        new_session = Session(self.window, profile=profile, backend=backend)

        # Reuse existing view if available
        if old_view and old_view.is_valid():
            new_session.output.view = old_view
            new_session.output.clear()
            sublime._claude_sessions[old_view.id()] = new_session

        new_session.start()
        if new_session.output.view:
            if backend != "claude":
                spec = backends.get(backend)
                new_session.output.view.settings().set("claude_backend", backend)
                new_session.output.set_name(spec.label)
                if spec.theme:
                    new_session.output.view.settings().set("color_scheme", spec.theme)
            else:
                new_session.output.view.set_name("Claude")
            if new_session.output.view.id() not in sublime._claude_sessions:
                sublime._claude_sessions[new_session.output.view.id()] = new_session
        new_session.output.show()
        sublime.status_message("Session restarted")


class ClaudeCodeCopySessionIdCommand(sublime_plugin.WindowCommand):
    """Copy the Claude session ID of the active view to the clipboard."""
    def run(self) -> None:
        view = self.window.active_view()
        s = (get_session_for_view(view) if view else None) or get_active_session(self.window)
        sid = getattr(s, "session_id", None) if s else None
        if not sid:
            sublime.status_message("Claude: no session id for this view")
            return
        sublime.set_clipboard(sid)
        sublime.status_message(f"Claude: session id copied — {sid}")

    def is_enabled(self) -> bool:
        view = self.window.active_view()
        s = (get_session_for_view(view) if view else None) or get_active_session(self.window)
        return bool(getattr(s, "session_id", None)) if s else False


class ClaudeCodeQueuePromptCommand(sublime_plugin.WindowCommand):
    """Queue a prompt to be sent when current query finishes."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if not s:
            sublime.status_message("No active session")
            return
        s.show_queue_input()


class ClaudeCodeInterruptCommand(sublime_plugin.WindowCommand):
    def run(self) -> None:
        s = get_active_session(self.window)
        if not s:
            return
        # If working, always interrupt — don't just clear input
        if s.working:
            s.interrupt()
            return
        # If idle in input mode with text, clear the input
        if s.output.is_input_mode() and s.output.get_input_text().strip():
            view = s.output.view
            start = s.output._input_start
            view.run_command("claude_replace", {
                "start": start,
                "end": view.size(),
                "text": ""
            })
            view.sel().clear()
            view.sel().add(sublime.Region(start, start))
            return
        s.interrupt()


class ClaudeCloseSessionCommand(sublime_plugin.TextCommand):
    """Close Claude session view with confirmation."""
    def run(self, edit):
        view = self.view
        session = sublime._claude_sessions.get(view.id())
        if not session or not (session.initialized or session.is_sleeping):
            view.close()
            return
        # Use set_timeout so the dialog doesn't block the command dispatch loop.
        # Blocking mid-dispatch can cause the next Cmd+W to bypass our keybinding.
        def _ask():
            # Re-check session (may have closed in the meantime)
            s = sublime._claude_sessions.get(view.id())
            if not s or not (s.initialized or s.is_sleeping):
                view.close()
                return
            if sublime.ok_cancel_dialog("Close this Claude session?", "Close"):
                s.stop()
                if view.id() in sublime._claude_sessions:
                    del sublime._claude_sessions[view.id()]
                view.close()
        sublime.set_timeout(_ask, 0)


class ClaudeCodeRenameCommand(sublime_plugin.WindowCommand):
    """Rename the current session."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if not s:
            return
        current = s.name or ""
        self.window.show_input_panel(
            "Session name:",
            current,
            lambda name: self._done(name),
            None, None
        )

    def _done(self, name: str) -> None:
        if name.strip():
            s = get_active_session(self.window)
            if s:
                s._set_name(name.strip())


class ClaudeCodeToggleCommand(sublime_plugin.WindowCommand):
    def run(self) -> None:
        s = get_active_session(self.window)
        if s and s.output.view and s.output.view.is_valid():
            # View exists - toggle visibility
            group, _ = self.window.get_view_index(s.output.view)
            if group >= 0:
                # Visible - hide it
                self.window.focus_view(s.output.view)
                self.window.run_command("close_file")
            else:
                # Hidden/closed - show it
                s.output.show()
        elif s:
            # No view yet - show it
            s.output.show()


class ClaudeCodeStopCommand(sublime_plugin.WindowCommand):
    def run(self) -> None:

        s = get_active_session(self.window)
        if s and s.output.view:
            view_id = s.output.view.id()
            s.stop()
            if view_id in sublime._claude_sessions:
                del sublime._claude_sessions[view_id]


class ClaudeTerminalModeCommand(sublime_plugin.WindowCommand):
    """Switch active session to CLI terminal mode."""
    def run(self):
        session = get_active_session(self.window)
        if not session:
            return
        if session.terminal_mode:
            tv = session._find_terminal_view()
            if tv:
                self.window.focus_view(tv)
            return
        session.enter_terminal_mode()

    def is_enabled(self):
        session = get_active_session(self.window)
        return (session is not None
                and bool(session.session_id)
                and not session.working
                and session.backend != "copilot")


class ClaudeSleepSessionCommand(sublime_plugin.WindowCommand):
    """Put the active session to sleep."""
    def run(self):
        session = get_active_session(self.window)
        if session and not session.is_sleeping:
            session.sleep()

    def is_enabled(self):
        session = get_active_session(self.window)
        return session is not None and not session.is_sleeping


class ClaudeWakeSessionCommand(sublime_plugin.WindowCommand):
    """Wake a sleeping session."""
    def run(self):
        session = get_active_session(self.window)
        if session and session.is_sleeping:
            session.wake()

    def is_enabled(self):
        session = get_active_session(self.window)
        return session is not None and session.is_sleeping


class ClaudeToggleAutoSleepCommand(sublime_plugin.WindowCommand):
    """Toggle auto-sleep for the active session."""
    def run(self):
        session = get_active_session(self.window)
        if not session:
            return
        session.sleep_disabled = not session.sleep_disabled
        state = "disabled" if session.sleep_disabled else "enabled"
        sublime.status_message(f"Claude: auto-sleep {state} for this session")
        session.output.set_name(session.name or "Claude")

    def is_enabled(self):
        return get_active_session(self.window) is not None

    def is_checked(self):
        session = get_active_session(self.window)
        return bool(session and session.sleep_disabled)


class ClaudeCodeResumeCommand(sublime_plugin.WindowCommand):
    """Resume a previous session."""
    def run(self) -> None:
        cwd = self.window.folders()[0] if self.window.folders() else ""
        sessions = [s for s in load_saved_sessions() if s.get("project", "") == cwd]
        if not sessions:
            sublime.status_message("No saved sessions to resume")
            return

        starred = load_bookmarks(cwd or None)
        # Starred sessions first, then others (both groups keep recent-first order)
        sessions = sorted(sessions, key=lambda s: s.get("session_id") not in starred)

        # Build quick panel items
        items = []
        for s in sessions:
            sid = s.get("session_id", "")
            name = s.get("name") or "(unnamed)"
            backend = s.get("backend", "claude")
            star = "★ " if sid in starred else ""
            prefix = f"[{backend}] " if backend != "claude" else ""
            project = s.get("project", "")
            if project:
                project = "  " + project.split("/")[-1]
            cost = s.get("total_cost", 0)
            cost_str = f"  ${cost:.4f}" if cost else ""
            items.append([f"{star}{prefix}{name}", f"{project}{cost_str}"])

        def on_select(idx):
            if idx >= 0:
                session_id = sessions[idx].get("session_id")
                name = sessions[idx].get("name")
                backend = sessions[idx].get("backend", "claude")
                s = create_session(self.window, resume_id=session_id, backend=backend)
                if name:
                    s.name = name
                    s.output.show()
                    s.output.set_name(name)
                    s._update_status_bar()

        self.window.show_quick_panel(items, on_select)


class ClaudeCodeSwitchCommand(sublime_plugin.WindowCommand):
    """Switch between active sessions in this window."""
    def run(self, backend: str = None, transport: str = "bridge", model: str = None) -> None:
        import os
        import shutil
        from ..core import create_session

        if backend is None:
            backend = sublime.load_settings("ClaudeCode.sublime-settings").get("default_backend", "claude")

        # transport "terminal" = run the claude CLI in our embedded terminal view
        # instead of the SDK bridge; New Session commits there.
        if transport == "terminal":
            backend = "claude"
        is_term = transport == "terminal"
        # `model` is an accumulated option (set via the /model entries, like the
        # Switch-to backend/transport options) — committed by New Session.
        backend_prefix = ("⬛ " if is_term else "") + (f"[{backend}] " if backend != "claude" else "")
        # Backend availability flags — sourced from backends registry. Custom
        # Anthropic-compatible providers are included dynamically.
        available_backends = [name for name, spec in backends.all_backends().items()
                              if spec.available is None or spec.available()]

        project_path = self.window.folders()[0] if self.window.folders() else None
        starred = load_bookmarks(project_path)

        # Get all sessions in this window
        sessions_in_window = []
        for view_id, session in sublime._claude_sessions.items():
            if session.window == self.window:
                sessions_in_window.append((view_id, session))

        # Build quick panel items
        active_view_id = self.window.settings().get("claude_active_view")
        items = []
        actions = []  # ("new", None) | ("focus", session)

        # Add active session at top if exists
        active_session = None
        for view_id, s in sessions_in_window:
            if view_id == active_view_id:
                active_session = s
                break

        # Show "Active:" option only when not in a Claude output view (for quick jumping from file view).
        # A revealed PTY session's current view is a terminal (not claude_output) — treat it as one so
        # its session actions (incl. "Return to native view") still show.
        current_view = self.window.active_view()
        in_output_view = current_view and (current_view.settings().get("claude_output")
                                           or current_view.settings().get("pty_reveal_owner"))
        current_file = current_view.file_name() if current_view else None

        # Add "New Session with This File" option when in a non-session file
        if not in_output_view and current_file:
            filename = os.path.basename(current_file)
            items.append([f"📎 {backend_prefix}New with ctx:{filename}", "Create session with this file as context"])
            actions.append(("new_with_file", current_file))

        if active_session and not in_output_view:
            name = active_session.name or "(unnamed)"
            star = "★ " if active_session.session_id in starred else ""
            if active_session.is_sleeping:
                status = "sleeping"
                prefix = "⏸ "
            elif active_session.working:
                status = "working..."
                prefix = "Active: "
            else:
                status = "ready"
                prefix = "Active: "
            cost = f"${active_session.total_cost:.4f}" if active_session.total_cost > 0 else ""
            detail = f"{status}  {cost}  {active_session.query_count}q" if cost else f"{status}  {active_session.query_count}q"
            items.append([f"{prefix}{star}{name}", detail])
            actions.append(("focus", active_session))

        # Add other sessions (not the active one) — starred float to top
        other_in_window = [(v, s) for v, s in sessions_in_window
                           if v != active_view_id and s is not active_session]
        starred_sessions = [(v, s) for v, s in other_in_window if s.session_id in starred]
        plain_sessions = [(v, s) for v, s in other_in_window if s.session_id not in starred]
        for view_id, s in starred_sessions + plain_sessions:
            name = s.name or "(unnamed)"
            is_starred = s.session_id in starred
            if s.is_sleeping:
                marker = "⏸ " + ("★ " if is_starred else "")
                status = "sleeping"
            elif s.working:
                marker = "\u2022 " + ("★ " if is_starred else "")
                status = "working..."
            else:
                marker = "★ " if is_starred else "  "
                status = "ready"
            cost = f"${s.total_cost:.4f}" if s.total_cost > 0 else ""
            detail = f"{status}  {cost}  {s.query_count}q" if cost else f"{status}  {s.query_count}q"
            items.append([f"{marker}{name}", detail])
            actions.append(("focus", s))

        # Add session actions when in a session output view
        if in_output_view and active_session:
            if active_session.session_id:
                is_starred = active_session.session_id in starred
                star_label = "★ Unstar Session" if is_starred else "☆ Star Session"
                star_detail = "Remove from pinned sessions" if is_starred else "Pin to top of session list"
                items.append([star_label, star_detail])
                actions.append(("toggle_star", active_session))
            if not active_session.working and active_session.session_id:
                items.append(["↩ Undo Message", "Rewind session to previous turn"])
                actions.append(("undo_message", active_session))
            if active_session and not active_session.is_sleeping:
                items.append(["○ Sleep Session", "Put session to sleep, free resources"])
                actions.append(("sleep", active_session))
            items.append(["🔄 Restart Session", "Restart current session, keep output"])
            actions.append(("restart", active_session))

            # PTY-engine sessions can hot-swap between the native view and the
            # raw claude TUI (same live process, no restart).
            from .. import cc_pty_session
            if isinstance(active_session, cc_pty_session.PtyEngineSession):
                if active_session.terminal_revealed:
                    items.append(["⇄ Return to native view", "Hide the raw TUI, show the native transcript"])
                else:
                    items.append(["⇄ Reveal as terminal", "Show this session's raw claude TUI in a terminal"])
                actions.append(("toggle_reveal", active_session))

        # Add profiles and checkpoints
        from ..settings import load_profiles_and_checkpoints

        profiles_path = os.path.join(project_path, ".claude", "profiles.json") if project_path else None
        profiles, checkpoints = load_profiles_and_checkpoints(profiles_path)

        for name, config in profiles.items():
            desc = config.get("description", f"{config.get('model', 'default')} model")
            items.append([f"😶 {backend_prefix}{name}", desc])
            actions.append(("profile", config))

        if backend == "claude":
            for name, config in checkpoints.items():
                desc = config.get("description", "Saved checkpoint")
                items.append([f"📍 {backend_prefix}{name}", desc])
                actions.append(("checkpoint", config))

        # Add "From Persona" option
        sublime_settings = sublime.load_settings("ClaudeCode.sublime-settings")
        persona_url = sublime_settings.get("persona_url", "http://localhost:5002/personas")
        items.append(["👤 From Persona...", "Acquire a persona identity"])
        actions.append(("persona", persona_url))

        # Add "New Session" option — commits the accumulated backend/transport/model.
        _mlabel = f" [{model}]" if model else ""
        items.append([f"🆕 {backend_prefix}New Session{_mlabel}",
                      (f"Start fresh with {model}" if model else "Start fresh with default model")])
        actions.append(("new", None))

        # Model selection from settings + cached models
        all_models = sublime_settings.get("models", {})
        # Also read cached models from copilot SDK
        cached_models_file = os.path.expanduser("~/.claude/sublime_cached_models.json")
        if os.path.exists(cached_models_file):
            try:
                import json as _json
                with open(cached_models_file) as f:
                    cached = _json.load(f)
                for b, models in cached.items():
                    if b not in all_models:
                        all_models[b] = models
            except Exception:
                pass
        backend_models = all_models.get(backend, [])
        if not backend_models:  # fall back to the backend registry's defaults
            try:
                backend_models = backends.get(backend).default_models
            except Exception:
                backend_models = []
        for m in backend_models:
            if isinstance(m, str):
                model_id, model_name = m, m
            elif isinstance(m, (list, tuple)) and len(m) >= 2:
                model_id, model_name = m[0], m[1]
            else:
                continue
            # Accumulator (like Switch-to): selecting sets the model and re-renders;
            # the New Session entry commits it. Does NOT launch.
            sel = "● " if model == model_id else ""
            items.append([f"/model {sel}{backend_prefix}{model_name}",
                          f"Select {model_id} for the next session"])
            actions.append(("set_model", model_id))

        # Add "Fork Session" option when in a session window
        if in_output_view and active_session:
            items.append(["🍴 Fork Session", "Create new session with copy of history"])
            actions.append(("fork", active_session))

        # Transport switch: bridge ⇄ embedded terminal (claude CLI). Mirrors the
        # backend switcher — re-runs the modal in the chosen transport.
        if is_term:
            items.append(["⇄ Switch to bridge", "Back to SDK/bridge sessions"])
            actions.append(("switch_transport", "bridge"))
        else:
            items.append(["⇄ Switch to terminal", "Run Claude Code in the embedded terminal"])
            actions.append(("switch_transport", "terminal"))

        # Add "Switch Backend" options (bridge transport only — terminal is claude CLI).
        # Built-ins in a stable order first, then any custom providers not yet listed.
        # Include current backend with "(current)" marker so users can see what's active.
        if not is_term:
            ordered = []
            for name in ("claude", "codex", "copilot", "pi", "dsr", "grok", "grok_cc"):
                if name in available_backends and name not in ordered:
                    ordered.append(name)
            for name in available_backends:
                if name not in ordered:
                    ordered.append(name)
            for other in ordered:
                spec = backends.get(other)
                label = spec.label
                if other == backend:
                    label = f"{label} (current)"
                items.append([f"with {label}…", f"Show {other} options"])
                actions.append(("switch_backend", other))

        def on_select(idx):
            if idx >= 0:
                action, data = actions[idx]
                if action == "switch_backend":
                    # Re-open panel with new backend
                    sublime.set_timeout(lambda: self.run(backend=data), 0)
                    return
                if action == "switch_transport":
                    # Re-open in the chosen transport, keeping the model if claude.
                    keep = model if backend == "claude" else None
                    sublime.set_timeout(lambda: self.run(backend="claude", transport=data, model=keep), 0)
                    return
                if action == "set_model":
                    # Accumulate the model choice and re-render (like Switch-to).
                    sublime.set_timeout(lambda: self.run(backend=backend, transport=transport, model=data), 0)
                    return
                if action == "toggle_star" and data and data.session_id:
                    now_starred = toggle_bookmark(data.session_id, project_path)
                    msg = f"★ Starred: {data.name or data.session_id}" if now_starred else f"☆ Unstarred: {data.name or data.session_id}"
                    sublime.status_message(msg)
                    return
                if action == "undo_message" and data:
                    if getattr(data, "backend", "") == "grok":
                        # Async points fetch — never send_wait on the UI thread.
                        data.show_grok_undo_panel(self.window)
                        return
                    turns = data.get_turns_for_undo()
                    if not turns:
                        sublime.status_message("No undoable turns")
                        return
                    labels = [t[0] for t in turns]
                    def _on_undo(uidx, _turns=turns, _s=data):
                        if uidx >= 0:
                            _, rewind_id, draft_prompt = _turns[uidx]
                            _s._apply_undo(rewind_id, draft_prompt)
                    self.window.show_quick_panel(
                        labels, _on_undo, placeholder="Rewind to…")
                elif action == "restart" and data:
                    # Show profile picker for restart
                    self._show_restart_picker(data, profiles, checkpoints)
                elif action == "new_with_file" and data:
                    if is_term:
                        self.window.run_command("claude_code_terminal",
                                                {"draft": "@{}\n".format(data), "model": model or "default"})
                    else:
                        # Create new session with current file as context
                        s = create_session(self.window,
                                           profile=({"model": model} if model else None),
                                           backend=backend)
                        try:
                            with open(data, "r", encoding="utf-8") as f:
                                content = f.read()
                            s.add_context_file(data, content)
                        except Exception as e:
                            print(f"[Claude] Error adding file context: {e}")
                elif action == "new":
                    if is_term:
                        self.window.run_command("claude_code_terminal", {"model": model or "default"})
                    else:
                        create_session(self.window,
                                       profile=({"model": model} if model else None),
                                       backend=backend)
                elif action == "profile":
                    if is_term:
                        self.window.run_command("claude_code_terminal", {"model": data.get("model")})
                    else:
                        create_session(self.window, profile=data, backend=backend)
                elif action == "checkpoint":
                    session_id = data.get("session_id")
                    if session_id:
                        create_session(self.window, resume_id=session_id, fork=True, backend=backend)
                elif action == "fork" and data:
                    # Fork the current session
                    if data.session_id:
                        create_session(self.window, resume_id=data.session_id, fork=True, backend=data.backend)
                elif action == "persona" and data:
                    # Show persona picker
                    self._show_persona_picker(data, backend=backend)
                elif action == "sleep" and data:
                    data.sleep()
                elif action == "toggle_reveal" and data:
                    if data.terminal_revealed:
                        data.return_to_native()
                    else:
                        data.reveal_as_terminal()
                elif action == "focus" and data:
                    data.output.show()

        _ph = []
        if is_term:
            _ph.append("⬛ terminal")
        if model:
            _ph.append(f"model: {model}")
        self.window.show_quick_panel(
            items, on_select, placeholder=(" · ".join(_ph) if _ph else None))

    def _show_persona_picker(self, persona_url: str, backend: str = "claude") -> None:
        """Show list of personas to pick from."""
        from .. import persona_client
        from ..core import create_session
        import threading

        def fetch_and_show():
            personas = persona_client.list_personas(persona_url)
            if not personas:
                sublime.set_timeout(lambda: sublime.status_message("No personas available"), 0)
                return

            # Build options: unlocked first, then locked
            unlocked = [p for p in personas if not p.get("is_locked")]
            locked = [p for p in personas if p.get("is_locked")]

            options = []
            for p in unlocked:
                tags = ", ".join(p.get("tags", [])) if p.get("tags") else ""
                desc = p.get("notes", tags) or "No description"
                options.append((p["id"], f"👤 {p['alias']}", desc[:60]))

            for p in locked:
                locked_by = p.get("locked_by_session", "unknown")
                options.append((p["id"], f"🔒 {p['alias']}", f"Locked by {locked_by}"))

            def show_panel():
                items = [[opt[1], opt[2]] for opt in options]

                def on_select(idx):
                    if idx < 0:
                        return
                    persona_id = options[idx][0]
                    self._start_with_persona(persona_id, persona_url, backend=backend)

                self.window.show_quick_panel(items, on_select)

            sublime.set_timeout(show_panel, 0)

        threading.Thread(target=fetch_and_show, daemon=True).start()

    def _start_with_persona(self, persona_id: int, persona_url: str, backend: str = "claude") -> None:
        """Acquire persona and start session."""
        from .. import persona_client
        from ..core import create_session
        import threading
        import uuid

        def acquire_and_start():
            session_id = f"sublime-{uuid.uuid4().hex[:8]}"

            result = persona_client.acquire_persona(session_id, persona_id=persona_id, base_url=persona_url)

            if "error" in result:
                sublime.set_timeout(
                    lambda: sublime.error_message(f"Failed to acquire persona: {result['error']}"),
                    0
                )
                return

            persona = result.get("persona", {})
            ability = result.get("ability", {})
            handoff_notes = result.get("handoff_notes")

            profile_config = {
                "model": ability.get("model") or persona.get("model") or "sonnet",
                "system_prompt": ability.get("system_prompt") or persona.get("system_prompt") or "",
                "persona_id": persona_id,
                "persona_session_id": session_id,
                "persona_url": persona_url,
                "description": f"Persona: {persona.get('alias', 'unknown')}"
            }

            def start():
                s = create_session(self.window, profile=profile_config, backend=backend)
                if handoff_notes:
                    s.output.text(f"\n*Handoff notes:* {handoff_notes}\n")
                sublime.status_message(f"Acquired persona: {persona.get('alias', 'unknown')}")

            sublime.set_timeout(start, 0)

        threading.Thread(target=acquire_and_start, daemon=True).start()

    def _show_restart_picker(self, session, profiles, checkpoints):
        """Show profile/checkpoint picker for restart."""
        from ..core import create_session

        items = []
        actions = []

        # Default restart
        items.append(["🆕 Fresh Start", "Restart with default settings"])
        actions.append(("default", None))

        # Profiles
        for name, config in profiles.items():
            desc = config.get("description", f"{config.get('model', 'default')} model")
            items.append([f"📋 {name}", desc])
            actions.append(("profile", config))

        # Checkpoints
        for name, config in checkpoints.items():
            desc = config.get("description", "Saved checkpoint")
            items.append([f"📍 {name}", desc])
            actions.append(("checkpoint", config))

        def on_select(idx):
            if idx < 0:
                return

            action, data = actions[idx]
            old_view = session.output.view

            # Stop old session
            session.stop()
            if old_view and old_view.id() in sublime._claude_sessions:
                del sublime._claude_sessions[old_view.id()]

            # Create new session with selected config
            if action == "checkpoint":
                session_id = data.get("session_id")
                new_session = Session(self.window, resume_id=session_id, fork=True, backend=session.backend)
            elif action == "profile":
                new_session = Session(self.window, profile=data, backend=session.backend)
            else:
                new_session = Session(self.window, backend=session.backend)

            # Reuse existing view
            if old_view and old_view.is_valid():
                new_session.output.view = old_view
                new_session.output.clear()
                sublime._claude_sessions[old_view.id()] = new_session

            new_session.start()
            if new_session.output.view:
                new_session.output.view.set_name("Claude")
                if new_session.output.view.id() not in sublime._claude_sessions:
                    sublime._claude_sessions[new_session.output.view.id()] = new_session
            new_session.output.show()

        self.window.show_quick_panel(items, on_select)


class ClaudeCodeForkCommand(sublime_plugin.WindowCommand):
    """Fork the current active session."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if not s or not s.session_id:
            sublime.status_message("No active session to fork")
            return

        # Create forked session
        forked = create_session(self.window, resume_id=s.session_id, fork=True, backend=s.backend)
        forked_name = f"{s.name or 'session'} (fork)"
        forked.name = forked_name
        forked.output.set_name(forked_name)
        sublime.status_message(f"Forked session: {forked_name}")


class ClaudeCodeForkFromCommand(sublime_plugin.WindowCommand):
    """Fork from a session selected from list."""
    def run(self) -> None:

        # Combine active sessions and saved sessions
        items = []
        sources = []

        # Active sessions in this window
        for view_id, session in sublime._claude_sessions.items():
            if session.window == self.window and session.session_id:
                name = session.name or "(unnamed)"
                cost = f"${session.total_cost:.4f}" if session.total_cost > 0 else ""
                items.append([f"● {name}", f"active  {cost}  {session.query_count}q"])
                sources.append(("active", view_id, session.session_id, name, session.backend))

        # Saved sessions
        saved = load_saved_sessions()
        for s in saved:
            session_id = s.get("session_id")
            name = s.get("name") or "(unnamed)"
            if any(src[2] == session_id for src in sources):
                continue
            project = s.get("project", "")
            if project:
                project = project.split("/")[-1]
            cost = s.get("total_cost", 0)
            cost_str = f"${cost:.4f}" if cost else ""
            items.append([name, f"saved  {project}  {cost_str}"])
            sources.append(("saved", None, session_id, name, s.get("backend", "claude")))

        if not items:
            sublime.status_message("No sessions to fork from")
            return

        def on_select(idx):
            if idx >= 0:
                source_type, view_id, session_id, name, src_backend = sources[idx]
                forked = create_session(self.window, resume_id=session_id, fork=True, backend=src_backend)
                forked_name = f"{name} (fork)"
                forked.name = forked_name
                forked.output.set_name(forked_name)
                sublime.status_message(f"Forked session: {forked_name}")

        self.window.show_quick_panel(items, on_select)


