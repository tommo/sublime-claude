"""Quick Agent — a real session view with a pinned fast model config.

Same UX as any Claude session (inline ◎ input, streaming, tools, permissions).
Difference is only the model/backend profile from settings `quick_agent`
(e.g. deepseek flash). One reusable Quick session per window.
"""
from __future__ import annotations

import sublime
import sublime_plugin
from typing import Dict, Optional, List, Tuple

from . import backends
from .session import Session

# window.id() → Session
_quick_sessions: Dict[int, Session] = {}

# Warm amber palette — distinct from default Claude / codex / copilot sheets
QUICK_COLOR_SCHEME = "Packages/ClaudeCode/ClaudeOutput-quick.hidden-tmTheme"


def load_config() -> dict:
    s = sublime.load_settings("ClaudeCode.sublime-settings")
    cfg = s.get("quick_agent") or {}
    return cfg if isinstance(cfg, dict) else {}


def save_config(cfg: dict) -> None:
    s = sublime.load_settings("ClaudeCode.sublime-settings")
    s.set("quick_agent", cfg)
    sublime.save_settings("ClaudeCode.sublime-settings")


def config_label(cfg: dict = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    backend = cfg.get("backend") or "deepseek"
    model = cfg.get("model") or "haiku"
    spec = backends.get(backend)
    label = (spec.label if spec else backend) or backend
    display_model = model
    if model in ("haiku", "flash") and spec:
        for mid, mlabel in (spec.default_models or []):
            if mid == "haiku" or mid == model:
                display_model = mlabel.split("→")[-1].strip() if "→" in mlabel else mlabel
                break
        if model == "flash":
            display_model = display_model or "flash"
    effort = cfg.get("effort") or ""
    bits = [label, str(display_model)]
    if effort:
        bits.append(str(effort))
    return " · ".join(bits)


def resolve_model_id(cfg: dict = None) -> str:
    """Map quick_agent model aliases (flash → haiku) for the bridge."""
    cfg = cfg if cfg is not None else load_config()
    model = (cfg.get("model") or "haiku").strip()
    if model == "flash":
        return "haiku"
    return model


# Per-window layout / return-focus live on window.settings():
#   claude_quick_layout: {"group": int, "index": int}
#   claude_quick_return_view: view id to restore on hide


def get_quick_session(window: sublime.Window) -> Optional[Session]:
    """Return the window's Quick Agent if its bridge is still alive.

    The sheet may be soft-hidden (view closed, bridge kept).
    """
    if not window:
        return None
    s = _quick_sessions.get(window.id())
    if not s:
        return None
    if s.client and s.client.is_alive():
        return s
    _quick_sessions.pop(window.id(), None)
    try:
        if s.output and s.output.view and hasattr(sublime, "_claude_sessions"):
            sublime._claude_sessions.pop(s.output.view.id(), None)
    except Exception:
        pass
    return None


def is_quick_view_focused(window: sublime.Window) -> bool:
    s = get_quick_session(window)
    if not s or not s.output or not s.output.view or not s.output.view.is_valid():
        return False
    active = window.active_view()
    return bool(active and active.id() == s.output.view.id())


def _source_view_for_context(window: sublime.Window) -> Optional[sublime.View]:
    """Document to attach as context — active non-Quick view, else return view."""
    if not window:
        return None
    av = window.active_view()
    if (av and av.is_valid()
            and not av.settings().get("claude_output")
            and not av.settings().get("claude_quick")):
        return av
    vid = window.settings().get("claude_quick_return_view")
    if vid is None:
        return None
    for v in window.views():
        if v.id() == vid and v.is_valid() and not v.settings().get("claude_quick"):
            return v
    return None


def _attach_focused_doc_context(session: Session, source: sublime.View = None) -> None:
    """Add the focused document path (or selection) as pending context.

    Replaces the previous auto-attached open context so reopen stays current.
    Selection present → selection chip with :L… range + text.
    No selection → path only (agent can read if needed; no whole-file slurp).
    """
    if not session:
        return
    view = source
    if not view or not view.is_valid():
        view = _source_view_for_context(session.window) if session.window else None
    if not view or not view.is_valid():
        return
    if view.settings().get("claude_output") or view.settings().get("claude_quick"):
        return

    from .context_manager import format_line_range

    # Drop prior auto-attach from last open so context stays one focused doc
    prev = getattr(session, "_quick_auto_context_keys", None) or set()
    if prev and session.context:
        session.context.items = [
            it for it in session.context.items
            if getattr(it, "name", None) not in prev
            and f"{it.path}:{it.line_range}" not in prev
            and it.path not in prev
        ]

    new_keys = set()
    path = view.file_name() or ""
    sels = [r for r in view.sel() if not r.empty()]

    if sels:
        for r in sels:
            content = view.substr(r)
            if not content.strip():
                continue
            r0 = view.rowcol(r.begin())[0] + 1
            r1 = view.rowcol(r.end())[0] + 1
            lr = format_line_range(r0, r1)
            label = f"{path or 'untitled'}:{lr}"
            session.add_context_selection(label, content)
            base = (path.split("/")[-1] if path else "untitled")
            new_keys.add(f"{base}:{lr}")
            new_keys.add(label)
            if path:
                new_keys.add(path)
    elif path:
        # Path ref only — no full file body (add_path would slurp text files)
        session.context._add_path_ref(path)
        new_keys.add(path)
        new_keys.add(path.split("/")[-1] if "/" in path else path)
    else:
        # Untitled, no selection — nothing useful to attach
        session._quick_auto_context_keys = set()
        return

    session._quick_auto_context_keys = new_keys
    # Refresh 📎 chips if already in input mode
    try:
        if session.output and session.output.is_input_mode():
            session.output.set_pending_context(list(session.context.items))
    except Exception:
        pass


def _remember_return_view(window: sublime.Window) -> None:
    """Remember the focused document so hide can restore it."""
    if not window:
        return
    av = window.active_view()
    if not av or not av.is_valid():
        return
    # Don't overwrite with the Quick sheet itself
    if av.settings().get("claude_quick") or av.settings().get("claude_output"):
        return
    window.settings().set("claude_quick_return_view", av.id())


def _restore_return_view(window: sublime.Window) -> bool:
    """Focus the document that was active when Quick Agent was shown."""
    if not window:
        return False
    vid = window.settings().get("claude_quick_return_view")
    if vid is None:
        return False
    for v in window.views():
        if v.id() == vid and v.is_valid():
            window.focus_view(v)
            return True
    return False


def _save_quick_layout(window: sublime.Window, view: sublime.View) -> None:
    """Remember group/index of the Quick sheet for this window."""
    if not window or not view or not view.is_valid():
        return
    try:
        group, index = window.get_view_index(view)
    except Exception:
        return
    if group is None or group < 0:
        return
    window.settings().set("claude_quick_layout", {
        "group": int(group),
        "index": int(index) if index is not None and index >= 0 else 0,
    })


def _apply_quick_layout(window: sublime.Window, view: sublime.View) -> None:
    """Place the Quick sheet at the last remembered position in this window."""
    if not window or not view or not view.is_valid():
        return
    layout = window.settings().get("claude_quick_layout")
    if not isinstance(layout, dict):
        return
    try:
        group = int(layout.get("group", 0))
        index = int(layout.get("index", 0))
    except (TypeError, ValueError):
        return
    n_groups = window.num_groups()
    if n_groups <= 0:
        return
    if group < 0 or group >= n_groups:
        group = window.active_group()
    # Clamp index into [0, len(views_in_group)] (len == append at end)
    try:
        n_in = len(window.views_in_group(group))
    except Exception:
        n_in = 0
    # view may already be in that group counting toward n_in
    index = max(0, min(index, n_in))
    try:
        window.set_view_index(view, group, index)
    except Exception as e:
        print(f"[Claude] quick layout: {e}")


def hide_quick_view(window: sublime.Window) -> bool:
    """Soft-hide the Quick sheet: close the tab, keep the bridge + transcript.

    Restores focus to the document that was active when Quick was shown.
    """
    s = get_quick_session(window)
    if not s or not s.output:
        return False
    view = s.output.view
    if not view or not view.is_valid():
        return False

    # Remember where this sheet lived for the next show
    _save_quick_layout(window, view)

    try:
        if s.output.is_input_mode():
            s.draft_prompt = s.output.get_input_text()
            s.output.exit_input_mode(keep_text=False)
        s._quick_hidden_buffer = view.substr(sublime.Region(0, view.size()))
    except Exception:
        s._quick_hidden_buffer = None

    view.settings().set("claude_quick_soft_close", True)
    try:
        if hasattr(sublime, "_claude_sessions"):
            sublime._claude_sessions.pop(view.id(), None)
    except Exception:
        pass

    # Detach PhantomSets from the dying view
    if hasattr(s, "reset_phantoms_for_new_view"):
        s.reset_phantoms_for_new_view()

    s.output.view = None
    s.output._input_mode = False
    s._input_mode_entered = False
    try:
        view.close()
    except Exception:
        pass

    # Prefer the remembered document; fall back to any non-quick view
    if not _restore_return_view(window):
        for v in window.views():
            if not v.settings().get("claude_quick"):
                window.focus_view(v)
                break

    sublime.status_message("Quick Agent hidden (⌘⇧\\ to show)")
    return True


def stop_quick_session(window: sublime.Window, close_view: bool = True) -> None:
    s = _quick_sessions.pop(window.id(), None)
    if not s:
        return
    try:
        if s.client:
            s.client.stop()
    except Exception:
        pass
    s.client = None
    s.initialized = False
    view = s.output.view if s.output else None
    try:
        if view and hasattr(sublime, "_claude_sessions"):
            sublime._claude_sessions.pop(view.id(), None)
    except Exception:
        pass
    if close_view and view and view.is_valid():
        try:
            view.settings().set("claude_quick_soft_close", True)
            view.close()
        except Exception:
            pass
    sublime.status_message("Quick Agent stopped")


def _focus_input(session: Session) -> None:
    """Force inline ◎ input mode (never show_input_panel)."""
    if not session or not session.output:
        return
    if session.working:
        return
    if session.output.view and session.output.view.is_valid():
        session.output.show(focus=True)
        _apply_quick_layout(session.window, session.output.view)
    session._input_mode_entered = False
    if session.output.is_input_mode():
        try:
            v = session.output.view
            if v and v.is_valid():
                end = v.size()
                v.sel().clear()
                v.sel().add(sublime.Region(end, end))
                v.show(end)
                window = session.window
                if window:
                    window.focus_view(v)
        except Exception:
            pass
        return
    session._enter_input_with_draft()


def _bind_quick_view(session: Session, *, restore_buffer: bool = False) -> Optional[sublime.View]:
    """Attach a sheet for session: create if needed, restore layout, register."""
    window = session.window
    if not window:
        return None
    window.settings().set("claude_creating_session", True)
    try:
        recreated = False
        if not session.output.view or not session.output.view.is_valid():
            session.output.view = None
            session.output._panel_name = None
            session.output._input_mode = False
            # Old PhantomSets point at the closed view — drop them
            if hasattr(session, "reset_phantoms_for_new_view"):
                session.reset_phantoms_for_new_view()
            session.output.show(focus=True)
            recreated = True
        view = session.output.view
        if not view:
            return None
        view.settings().set("claude_backend", session.backend)
        view.settings().set("claude_quick", True)
        view.settings().set("color_scheme", QUICK_COLOR_SCHEME)
        session.output.set_name(session.name or "⚡ Quick")
        if restore_buffer:
            buf = getattr(session, "_quick_hidden_buffer", None) or ""
            if buf:
                view.set_read_only(False)
                view.run_command("append", {"characters": buf})
                view.set_read_only(True)
            session._quick_hidden_buffer = None
        if hasattr(sublime, "_claude_sessions"):
            sublime._claude_sessions[view.id()] = session
        window.settings().set("claude_active_view", view.id())
        _apply_quick_layout(window, view)
        window.focus_view(view)
        if recreated:
            # Permission / wakeup / media phantoms need a live view + input mode
            session._quick_view_recreated = True
        return view
    finally:
        window.settings().erase("claude_creating_session")


def _reshow_hidden_session(session: Session, source: sublime.View = None) -> None:
    """Re-create the sheet for a soft-hidden Quick session (bridge still up)."""
    _remember_return_view(session.window)
    _attach_focused_doc_context(session, source)
    _bind_quick_view(session, restore_buffer=True)
    if not session.working:
        _focus_input(session)
        # enter_input_mode pins the bypass/permission hint phantom; force a
        # second pass after the new view settles (PhantomSet bind race).
        def _repin():
            if not session.output or not session.output.view:
                return
            if not session.output.is_input_mode():
                session._input_mode_entered = False
                session._enter_input_with_draft()
            try:
                session._update_permission_banner(show=True)
                session._update_wakeup_banner(show=True)
            except Exception:
                pass
            try:
                session.output._refresh_media_phantoms()
            except Exception:
                pass
            # Context chips after input mode is up
            try:
                if session.context and session.context.items:
                    session.output.set_pending_context(list(session.context.items))
            except Exception:
                pass
        sublime.set_timeout(_repin, 30)


def ensure_quick_session(window: sublime.Window, force_new: bool = False) -> Session:
    """Create or focus Quick Agent as a normal session sheet + inline input."""
    # Capture focused doc *before* we steal focus for the Quick sheet
    source = _source_view_for_context(window)

    if not force_new:
        existing = get_quick_session(window)
        if existing and existing.client and existing.client.is_alive() and existing.initialized:
            if existing.output and existing.output.view and existing.output.view.is_valid():
                _remember_return_view(window)
                _attach_focused_doc_context(existing, source)
                _focus_input(existing)
            else:
                _reshow_hidden_session(existing, source=source)
            return existing
        if existing:
            stop_quick_session(window, close_view=True)

    cfg = load_config()
    backend = (cfg.get("backend") or "deepseek").strip() or "deepseek"
    model = resolve_model_id(cfg)
    effort = (cfg.get("effort") or "low").strip() or "low"
    system = (cfg.get("system_prompt") or (
        "You are a Quick Agent for short trivia and focused tasks. "
        "Answer concisely. Prefer a few sentences unless the user asks for depth."
    )).strip()

    profile = {
        "model": model,
        "effort": effort,
        "system_prompt": system,
    }

    # Remember where the user was before we steal focus
    _remember_return_view(window)

    s = Session(window, profile=profile, backend=backend)
    s.quick_mode = True
    s.sleep_disabled = True
    s.name = f"⚡ Quick · {config_label(cfg)}"

    # Attach focused file / selection as 📎 context for the first turn
    _attach_focused_doc_context(s, source)

    # Full session sheet — same OutputView + enter_input_mode path as New Session
    _bind_quick_view(s, restore_buffer=False)
    _quick_sessions[window.id()] = s
    s.start()  # _on_init → _enter_input_with_draft → ◎ input mode

    # Belt-and-suspenders: if init wins the race after this returns, re-focus ◎
    def _ensure_input(tries=0):
        if get_quick_session(window) is not s:
            return
        if s.initialized and not s.working:
            _focus_input(s)
            try:
                if s.context and s.context.items:
                    s.output.set_pending_context(list(s.context.items))
            except Exception:
                pass
            return
        if tries < 50:
            sublime.set_timeout(lambda: _ensure_input(tries + 1), 100)

    sublime.set_timeout(lambda: _ensure_input(), 50)
    return s


def list_backend_choices() -> List[Tuple[str, str, str]]:
    out = []
    for name, spec in backends.all_backends().items():
        if spec.available is not None and not spec.available():
            continue
        title = f"{spec.label or name}"
        models = ", ".join(m[0] for m in (spec.default_models or [])[:3]) or spec.fallback_model
        detail = f"{name} · {models}"
        out.append((name, title, detail))
    out.sort(key=lambda x: (0 if x[0] == "deepseek" else 1, x[1].lower()))
    return out


def list_model_choices(backend: str) -> List[Tuple[str, str]]:
    spec = backends.get(backend)
    items: List[Tuple[str, str]] = []
    seen = set()
    if spec and spec.default_models:
        for mid, label in spec.default_models:
            items.append((mid, label))
            seen.add(mid)
            if mid == "haiku" and "flash" not in seen:
                items.insert(0, ("flash", f"Flash (→ haiku / {label})"))
                seen.add("flash")
    if not items:
        items = [
            ("haiku", "Haiku / flash"),
            ("sonnet", "Sonnet"),
            ("opus", "Opus"),
        ]
    return items


# ── Commands ────────────────────────────────────────────────────────────────


class ClaudeQuickAgentCommand(sublime_plugin.WindowCommand):
    """Toggle Quick Agent sheet (⌘⇧\\): show / focus, or hide when focused."""

    def run(self, prompt: str = None, config: bool = False) -> None:
        if config:
            self.window.run_command("claude_quick_agent_config")
            return
        # Same shortcut again while focused → soft-hide (bridge stays warm)
        if not prompt and is_quick_view_focused(self.window):
            hide_quick_view(self.window)
            return
        s = ensure_quick_session(self.window)
        # No show_input_panel — type in the sheet's ◎ region like any session.
        if prompt and str(prompt).strip():
            def _when_ready(tries=0):
                if s.initialized and not s.working:
                    s.query(str(prompt).strip())
                    return
                if tries > 80:
                    return
                sublime.set_timeout(lambda: _when_ready(tries + 1), 100)
            if s.initialized and not s.working:
                s.query(str(prompt).strip())
            else:
                _when_ready()


class ClaudeQuickAgentConfigCommand(sublime_plugin.WindowCommand):
    """Pick backend + model for the Quick Agent session."""

    def run(self) -> None:
        cfg = load_config()
        current = config_label(cfg)
        backends_list = list_backend_choices()
        if not backends_list:
            sublime.error_message("No backends available for Quick Agent")
            return

        items = [[f"⚡ {t}", d] for _, t, d in backends_list]
        items.insert(0, [f"Current: {current}", "Keep backend, change model…"])
        choices = [("__keep__", None, None)] + backends_list

        def on_backend(idx: int):
            if idx < 0:
                return
            if idx == 0:
                backend = cfg.get("backend") or "deepseek"
            else:
                backend = choices[idx][0]
            self._pick_model(backend, cfg)

        self.window.show_quick_panel(items, on_backend)

    def _pick_model(self, backend: str, prev_cfg: dict) -> None:
        models = list_model_choices(backend)
        cur_model = prev_cfg.get("model") or "haiku"
        items = []
        for mid, label in models:
            mark = " ✓" if mid == cur_model else ""
            items.append([f"{label}{mark}", mid])

        def on_model(idx: int):
            if idx < 0:
                return
            self._pick_effort(backend, models[idx][0], prev_cfg)

        self.window.show_quick_panel(items, on_model)

    def _pick_effort(self, backend: str, model: str, prev_cfg: dict) -> None:
        efforts = ["low", "medium", "high", "max"]
        cur = (prev_cfg.get("effort") or "low").strip() or "low"
        items = [[f"{e}{' ✓' if e == cur else ''}", "reasoning effort"] for e in efforts]

        def on_effort(idx: int):
            if idx < 0:
                return
            effort = efforts[idx]
            cfg = dict(prev_cfg) if isinstance(prev_cfg, dict) else {}
            cfg["backend"] = backend
            cfg["model"] = model
            cfg["effort"] = effort
            cfg.setdefault("permission_mode", "bypassPermissions")
            # Inherit global tool list when not set — full session capability.
            cfg.setdefault(
                "system_prompt",
                "You are a Quick Agent for short trivia and focused tasks. "
                "Answer concisely. Prefer a few sentences unless the user asks for depth.",
            )
            save_config(cfg)
            stop_quick_session(self.window, close_view=True)
            sublime.status_message(f"Quick Agent → {config_label(cfg)}")
            ensure_quick_session(self.window, force_new=True)

        self.window.show_quick_panel(items, on_effort)


class ClaudeQuickAgentStopCommand(sublime_plugin.WindowCommand):
    """Stop the Quick Agent and close its view."""

    def run(self) -> None:
        stop_quick_session(self.window, close_view=True)

    def is_enabled(self) -> bool:
        return get_quick_session(self.window) is not None
