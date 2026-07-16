"""Quick Agent host — multi-slot (≤3) transient agents in one sheet.

Each slot owns a short-lived Session (own bridge). One host view per window
with a phantom tab bar; only the active slot paints the buffer. Soft-hide
keeps bridges warm. Agents call quick_done to stop their own slot.
"""
from __future__ import annotations

import re
import sublime
import sublime_plugin
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple, Any

from . import backends
from .session import Session

MAX_QUICK_SLOTS = 3
QUICK_COLOR_SCHEME = "Packages/ClaudeCode/ClaudeOutput-quick.hidden-tmTheme"
TAB_BAR_KEY = "claude_quick_tabs"

# window.id() → QuickHost
_hosts: Dict[int, "QuickHost"] = {}

# Pure helpers (unit-tested without a live bridge)
def normalize_done_status(status: Optional[str]) -> str:
    s = (status or "completed").strip().lower()
    if s in ("blocked", "error", "failed", "fail"):
        return "blocked"
    return "completed"


def can_add_slot(n_slots: int, cap: int = MAX_QUICK_SLOTS) -> bool:
    return n_slots < cap


def default_system_prompt() -> str:
    return (
        "You are a Quick Agent for short trivia and focused tasks. "
        "Answer concisely. Prefer a few sentences unless the user asks for depth. "
        "When the user's request is fully handled, call the MCP tool "
        "quick_done (via use_tool / sublime tools) with status='completed' and a "
        "one-line message summary. If you cannot finish, call quick_done with "
        "status='blocked' and a short reason. Do not leave the session open waiting."
    )


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
    cfg = cfg if cfg is not None else load_config()
    model = (cfg.get("model") or "haiku").strip()
    if model == "flash":
        return "haiku"
    return model


def stop_session_bridge(session: Optional[Session]) -> bool:
    """Stop a session's bridge process. Returns True if a client was stopped."""
    if not session:
        return False
    stopped = False
    try:
        if session.client:
            session.client.stop()
            stopped = True
    except Exception:
        pass
    session.client = None
    session.initialized = False
    session.working = False
    return stopped


@dataclass
class QuickSlot:
    slot_id: str
    session: Session
    name: str
    content: str = ""
    scroll_pos: Tuple[float, float] = (0.0, 0.0)
    draft: str = ""
    status: str = "live"  # live | completed | blocked
    status_message: str = ""


class QuickHost:
    """One host view + up to MAX_QUICK_SLOTS agent slots per window."""

    def __init__(self, window: sublime.Window):
        self.window = window
        self.view: Optional[sublime.View] = None
        self.slots: Dict[str, QuickSlot] = {}
        self.active_id: Optional[str] = None
        self._counter = 0
        self._tab_phantom_set = None

    # ── registry helpers ──────────────────────────────────────────────

    @property
    def active_slot(self) -> Optional[QuickSlot]:
        if not self.active_id:
            return None
        return self.slots.get(self.active_id)

    @property
    def active_session(self) -> Optional[Session]:
        sl = self.active_slot
        return sl.session if sl else None

    def slot_for_session(self, session: Session) -> Optional[QuickSlot]:
        for sl in self.slots.values():
            if sl.session is session:
                return sl
        return None

    # ── show / hide host view ─────────────────────────────────────────

    def ensure_view(self, focus: bool = True) -> Optional[sublime.View]:
        if self.view and self.view.is_valid():
            if focus:
                self.window.focus_view(self.view)
                _apply_quick_layout(self.window, self.view)
            return self.view
        self.window.settings().set("claude_creating_session", True)
        try:
            v = self.window.new_file()
            v.set_scratch(True)
            v.set_read_only(True)
            v.settings().set("claude_output", True)
            v.settings().set("claude_quick", True)
            v.settings().set("claude_quick_host", True)
            v.settings().set("auto_indent", False)
            v.settings().set("color_scheme", QUICK_COLOR_SCHEME)
            try:
                v.assign_syntax("Packages/ClaudeCode/ClaudeOutput.sublime-syntax")
            except Exception:
                pass
            v.set_name("⚡ Quick")
            self.view = v
            _apply_quick_layout(self.window, v)
            if focus:
                self.window.focus_view(v)
            return v
        finally:
            self.window.settings().erase("claude_creating_session")

    def is_focused(self) -> bool:
        if not self.view or not self.view.is_valid():
            return False
        av = self.window.active_view()
        return bool(av and av.id() == self.view.id())

    def soft_hide(self) -> bool:
        if not self.view or not self.view.is_valid():
            return False
        self._save_active_surface()
        _save_quick_layout(self.window, self.view)
        # Detach all sessions from view registry
        for sl in self.slots.values():
            s = sl.session
            if s.output and s.output.view:
                try:
                    if hasattr(s, "reset_phantoms_for_new_view"):
                        s.reset_phantoms_for_new_view()
                except Exception:
                    pass
                s.output.view = None
                s.output._input_mode = False
        try:
            if hasattr(sublime, "_claude_sessions"):
                sublime._claude_sessions.pop(self.view.id(), None)
        except Exception:
            pass
        self.view.settings().set("claude_quick_soft_close", True)
        try:
            self.view.close()
        except Exception:
            pass
        self.view = None
        self._tab_phantom_set = None
        if not _restore_return_view(self.window):
            for v in self.window.views():
                if not v.settings().get("claude_quick"):
                    self.window.focus_view(v)
                    break
        sublime.status_message("Quick Agent hidden (⌘⇧\\ to show)")
        return True

    def show(self, source: sublime.View = None) -> Optional[Session]:
        """Show host; create first slot if empty. Returns active session."""
        _remember_return_view(self.window)
        self.ensure_view(focus=True)
        if not self.slots:
            self.add_slot(source=source)
        else:
            # Rebind active to view and restore buffer
            self._activate(self.active_id or next(iter(self.slots)),
                           source=source, force_restore=True)
        return self.active_session

    # ── slots ─────────────────────────────────────────────────────────

    def add_slot(self, source: sublime.View = None, name: str = None) -> Optional[QuickSlot]:
        if not can_add_slot(len(self.slots)):
            sublime.status_message(
                f"Quick Agent: max {MAX_QUICK_SLOTS} slots — close one first")
            sublime.error_message(
                f"Quick Agent allows at most {MAX_QUICK_SLOTS} concurrent slots.\n"
                f"Close a tab (×) before opening another.")
            return None
        self.ensure_view(focus=True)
        if self.active_id and self.active_id in self.slots:
            self._save_active_surface()
            # Detach previous active from painting
            prev = self.slots[self.active_id].session
            if prev.output:
                if prev.output.is_input_mode():
                    try:
                        prev.draft_prompt = prev.output.get_input_text()
                        prev.output.exit_input_mode(keep_text=False)
                    except Exception:
                        pass
                prev.output.view = None
                prev.output._input_mode = False
                if hasattr(prev, "reset_phantoms_for_new_view"):
                    prev.reset_phantoms_for_new_view()

        self._counter += 1
        sid = f"q{self._counter}"
        label = name or f"Q{self._counter}"
        s = self._spawn_session(label, slot_id=sid)
        _attach_focused_doc_context(s, source or _source_view_for_context(self.window))
        slot = QuickSlot(slot_id=sid, session=s, name=label)
        self.slots[sid] = slot
        s._quick_slot_id = sid  # identity for quick_done (multi-slot host)
        self.active_id = sid
        self._bind_session_to_host(s, clear_buffer=True)
        s.start()
        self._schedule_input(s)
        self._render_tab_bar()
        return slot

    def _spawn_session(self, label: str, slot_id: str = None) -> Session:
        cfg = load_config()
        backend = (cfg.get("backend") or "deepseek").strip() or "deepseek"
        model = resolve_model_id(cfg)
        effort = (cfg.get("effort") or "low").strip() or "low"
        system = (cfg.get("system_prompt") or default_system_prompt()).strip()
        # Always append self-stop hint if not already present
        if "quick_done" not in system:
            system = system.rstrip() + "\n\n" + (
                "When the request is fully handled, call MCP tool quick_done "
                "with status='completed' and a one-line message. "
                "If blocked, status='blocked' with reason."
            )
        profile = {"model": model, "effort": effort, "system_prompt": system}
        s = Session(self.window, profile=profile, backend=backend)
        s.quick_mode = True
        s.sleep_disabled = True
        s.name = f"⚡ {label} · {config_label(cfg)}"
        s._quick_slot_id = slot_id
        return s

    def _bind_session_to_host(self, session: Session, clear_buffer: bool = False,
                              restore_content: str = None) -> None:
        view = self.ensure_view(focus=True)
        if not view:
            return
        if hasattr(session, "reset_phantoms_for_new_view"):
            session.reset_phantoms_for_new_view()
        session.output.view = view
        session.output._panel_name = None
        session.output._input_mode = False
        view.settings().set("claude_backend", session.backend)
        view.settings().set("claude_quick", True)
        view.settings().set("claude_quick_host", True)
        view.settings().set("color_scheme", QUICK_COLOR_SCHEME)
        session.output.set_name(session.name or "⚡ Quick")
        if clear_buffer or restore_content is not None:
            view.set_read_only(False)
            view.run_command("select_all")
            view.run_command("right_delete")
            if restore_content:
                view.run_command("append", {"characters": restore_content})
            view.set_read_only(True)
        if hasattr(sublime, "_claude_sessions"):
            sublime._claude_sessions[view.id()] = session
        self.window.settings().set("claude_active_view", view.id())

    def _schedule_input(self, session: Session) -> None:
        def _go(tries=0):
            if self.active_session is not session:
                return
            sl = self.slot_for_session(session)
            if sl and sl.status in ("completed", "blocked"):
                self._show_done_chrome(sl)
                self._render_tab_bar()
                return
            if session.initialized and not session.working:
                session._input_mode_entered = False
                if not session.output.is_input_mode():
                    session._enter_input_with_draft()
                try:
                    if session.context and session.context.items:
                        session.output.set_pending_context(list(session.context.items))
                except Exception:
                    pass
                try:
                    session._update_queue_phantom()
                except Exception:
                    pass
                self._render_tab_bar()
                return
            if tries < 80:
                sublime.set_timeout(lambda: _go(tries + 1), 100)
        sublime.set_timeout(lambda: _go(), 50)

    def _show_done_chrome(self, slot: QuickSlot) -> None:
        """Append a done line if not already present; no bridge."""
        s = slot.session
        if not s.output or not s.output.view:
            return
        glyph = "✓" if slot.status == "completed" else "⚠"
        line = f"\n  {glyph} quick_done · {slot.status}"
        if slot.status_message:
            line += f" · {slot.status_message}"
        line += "\n"
        try:
            # Avoid duplicating if already at end
            content = s.output.view.substr(sublime.Region(0, s.output.view.size()))
            if "quick_done ·" in content[-200:]:
                return
            s.output.view.set_read_only(False)
            s.output.view.run_command("append", {"characters": line})
            s.output.view.set_read_only(True)
        except Exception:
            pass

    def switch_to(self, slot_id: str) -> None:
        if slot_id not in self.slots or slot_id == self.active_id:
            if slot_id == self.active_id:
                self.ensure_view(focus=True)
            return
        self._activate(slot_id)

    def _activate(self, slot_id: str, source: sublime.View = None,
                  force_restore: bool = False) -> None:
        if slot_id not in self.slots:
            return
        if self.active_id and self.active_id in self.slots and self.active_id != slot_id:
            self._save_active_surface()
            prev = self.slots[self.active_id].session
            if prev.output:
                if prev.output.is_input_mode():
                    try:
                        prev.draft_prompt = prev.output.get_input_text()
                        self.slots[self.active_id].draft = prev.draft_prompt
                        prev.output.exit_input_mode(keep_text=False)
                    except Exception:
                        pass
                prev.output.view = None
                prev.output._input_mode = False
                if hasattr(prev, "reset_phantoms_for_new_view"):
                    prev.reset_phantoms_for_new_view()
        self.active_id = slot_id
        slot = self.slots[slot_id]
        s = slot.session
        s.draft_prompt = slot.draft or s.draft_prompt or ""
        self._bind_session_to_host(
            s, clear_buffer=True, restore_content=slot.content or "")
        # Restore scroll
        def _scroll():
            if self.view and self.view.is_valid():
                try:
                    self.view.set_viewport_position(slot.scroll_pos, False)
                except Exception:
                    pass
        sublime.set_timeout(_scroll, 10)
        if source:
            _attach_focused_doc_context(s, source)
        if slot.status in ("completed", "blocked"):
            self._show_done_chrome(slot)
            self._render_tab_bar()
            return
        if s.client and s.client.is_alive() and s.initialized:
            s._input_mode_entered = False
            s._enter_input_with_draft()
            try:
                s._update_queue_phantom()
            except Exception:
                pass
        elif not s.client or not s.client.is_alive():
            # Dead bridge — restart if not done
            if slot.status == "live":
                self._restart_slot_bridge(slot)
        self._render_tab_bar()

    def _restart_slot_bridge(self, slot: QuickSlot) -> None:
        name = slot.name
        ns = self._spawn_session(name, slot_id=slot.slot_id)
        ns._quick_slot_id = slot.slot_id
        slot.session = ns
        self._bind_session_to_host(ns, clear_buffer=False, restore_content=slot.content)
        ns.start()
        self._schedule_input(ns)

    def _save_active_surface(self) -> None:
        if not self.active_id or self.active_id not in self.slots:
            return
        if not self.view or not self.view.is_valid():
            return
        slot = self.slots[self.active_id]
        s = slot.session
        try:
            if s.output and s.output.is_input_mode():
                slot.draft = s.output.get_input_text()
                s.draft_prompt = slot.draft
                # Save buffer without peeling input carefully: full substr OK
                # (input re-added on restore via enter_input)
                peel = getattr(s.output, "_input_area_start", None)
                if peel is not None and peel >= 0:
                    slot.content = self.view.substr(sublime.Region(0, peel))
                else:
                    slot.content = self.view.substr(sublime.Region(0, self.view.size()))
            else:
                slot.content = self.view.substr(sublime.Region(0, self.view.size()))
            vp = self.view.viewport_position()
            if vp and len(vp) >= 2:
                slot.scroll_pos = (float(vp[0]), float(vp[1]))
        except Exception as e:
            print(f"[Claude] quick save surface: {e}")

    def close_slot(self, slot_id: str = None) -> None:
        slot_id = slot_id or self.active_id
        if not slot_id or slot_id not in self.slots:
            return
        slot = self.slots[slot_id]
        stop_session_bridge(slot.session)
        try:
            if slot.session.output:
                slot.session.output.view = None
        except Exception:
            pass
        del self.slots[slot_id]
        if not self.slots:
            # Last slot — soft-hide host
            self.active_id = None
            self.soft_hide()
            return
        if self.active_id == slot_id:
            self.active_id = None
            nxt = next(iter(self.slots))
            self._activate(nxt)
        else:
            self._render_tab_bar()

    def complete_slot(
        self,
        session: Session,
        status: str = "completed",
        message: str = "",
    ) -> dict:
        """Self-stop: mark slot done/blocked and stop its bridge. Testable."""
        status = normalize_done_status(status)
        message = (message or "").strip()
        slot = self.slot_for_session(session)
        if not slot:
            # Not multi-host? Fall back to single-session stop
            if getattr(session, "quick_mode", False):
                stop_session_bridge(session)
                try:
                    if session.output and session.output.view:
                        glyph = "✓" if status == "completed" else "⚠"
                        session.output.view.set_read_only(False)
                        session.output.view.run_command(
                            "append",
                            {"characters": f"\n  {glyph} quick_done · {status}"
                             + (f" · {message}" if message else "") + "\n"},
                        )
                        session.output.view.set_read_only(True)
                except Exception:
                    pass
                return {"ok": True, "status": status, "message": message, "slot": None}
            return {"ok": False, "error": "not a quick session"}

        slot.status = status
        slot.status_message = message
        stop_session_bridge(session)
        session.working = False
        # Paint status if this slot is active
        if self.active_id == slot.slot_id and self.view and self.view.is_valid():
            if session.output:
                session.output.view = self.view
            self._show_done_chrome(slot)
            try:
                if session.output and session.output.is_input_mode():
                    session.output.exit_input_mode(keep_text=False)
            except Exception:
                pass
        self._render_tab_bar()
        sublime.status_message(f"Quick · {slot.name}: {status}")
        return {
            "ok": True,
            "status": status,
            "message": message,
            "slot": slot.slot_id,
            "bridge_stopped": True,
        }

    # ── tab bar ───────────────────────────────────────────────────────

    def _render_tab_bar(self) -> None:
        if not self.view or not self.view.is_valid():
            return
        import html as _html
        chips = []
        for sid, sl in self.slots.items():
            label = _html.escape(sl.name)
            busy = sl.session.working if sl.session else False
            if sl.status == "completed":
                mark = "✓ "
            elif sl.status == "blocked":
                mark = "⚠ "
            elif busy:
                mark = "◉ "
            else:
                mark = ""
            if sid == self.active_id:
                style = (
                    "color:var(--foreground);"
                    "background-color:color(var(--foreground) alpha(0.18));"
                    "padding:2px 8px;margin-right:4px;text-decoration:none;"
                    "font-weight:bold;"
                )
            else:
                style = (
                    "color:color(var(--foreground) alpha(0.55));"
                    "padding:2px 8px;margin-right:4px;text-decoration:none;"
                )
            chips.append(
                f'<a href="tab:{sid}" style="{style}">{mark}{label}</a>'
                f'<a href="close:{sid}" style="color:var(--redish);'
                f'text-decoration:none;margin-right:8px;" title="close">×</a>'
            )
        # + new if under cap
        if can_add_slot(len(self.slots)):
            chips.append(
                '<a href="new" style="color:var(--bluish);padding:2px 8px;'
                'text-decoration:none;font-weight:bold;" title="new slot">+</a>'
            )
        else:
            chips.append(
                '<span style="color:color(var(--foreground) alpha(0.35));'
                'padding:2px 8px;">(max 3)</span>'
            )
        html = (
            '<body id="claude-quick-tabs" style="margin:0;padding:4px 0;">'
            '<div style="padding:2px 4px;">'
            f'{"".join(chips)}'
            '</div></body>'
        )
        try:
            if self._tab_phantom_set is None:
                self._tab_phantom_set = sublime.PhantomSet(self.view, TAB_BAR_KEY)
            self._tab_phantom_set.update([sublime.Phantom(
                sublime.Region(0, 0),
                html,
                sublime.LAYOUT_BLOCK,
                on_navigate=self._on_tab_navigate,
            )])
        except Exception as e:
            print(f"[Claude] quick tab bar: {e}")

    def _on_tab_navigate(self, href: str) -> None:
        if href == "new":
            self.add_slot(source=_source_view_for_context(self.window))
            return
        if href.startswith("tab:"):
            self.switch_to(href.split(":", 1)[1])
            return
        if href.startswith("close:"):
            self.close_slot(href.split(":", 1)[1])
            return


# ── window registry ───────────────────────────────────────────────────

def get_host(window: sublime.Window) -> Optional[QuickHost]:
    if not window:
        return None
    return _hosts.get(window.id())


def ensure_host(window: sublime.Window) -> QuickHost:
    h = _hosts.get(window.id())
    if not h:
        h = QuickHost(window)
        _hosts[window.id()] = h
    return h


def get_quick_session(window: sublime.Window) -> Optional[Session]:
    h = get_host(window)
    if not h:
        return None
    s = h.active_session
    if s and (not s.client or not s.client.is_alive()) and h.active_slot and h.active_slot.status == "live":
        # bridge dead — still return for UI
        return s
    return s


def is_quick_view_focused(window: sublime.Window) -> bool:
    h = get_host(window)
    return bool(h and h.is_focused())


def hide_quick_view(window: sublime.Window) -> bool:
    h = get_host(window)
    if not h:
        return False
    return h.soft_hide()


def stop_quick_session(window: sublime.Window, close_view: bool = True) -> None:
    """Stop all slots and tear down host (Stop command)."""
    h = _hosts.pop(window.id(), None)
    if not h:
        return
    for sl in list(h.slots.values()):
        stop_session_bridge(sl.session)
    h.slots.clear()
    h.active_id = None
    if close_view and h.view and h.view.is_valid():
        try:
            h.view.settings().set("claude_quick_soft_close", True)
            if hasattr(sublime, "_claude_sessions"):
                sublime._claude_sessions.pop(h.view.id(), None)
            h.view.close()
        except Exception:
            pass
    h.view = None
    sublime.status_message("Quick Agent stopped")


def ensure_quick_session(window: sublime.Window, force_new: bool = False) -> Session:
    """Show host / active slot. force_new → new slot (if under cap)."""
    source = _source_view_for_context(window)
    h = ensure_host(window)
    if force_new:
        sl = h.add_slot(source=source)
        if not sl:
            # Cap hit — return active if any
            return h.active_session or _empty_fail_session(window)
        return sl.session
    s = h.show(source=source)
    return s or _empty_fail_session(window)


def _empty_fail_session(window) -> Session:
    # Should not be used; callers check None. Provide a dead session for type compat.
    s = Session(window, backend="deepseek")
    s.quick_mode = True
    return s


def resolve_quick_session_for_tool(view_id: int = None) -> tuple:
    """Resolve (host, session) for MCP quick_done.

    Multi-slot hosts share one view_id for all slots. Prefer:
      1) window setting claude_executing_quick_slot (set when that slot queries)
      2) the sole working slot on the host for that view
      3) active slot
    """
    host = None
    if view_id is not None:
        for h in _hosts.values():
            try:
                if h.view and h.view.is_valid() and h.view.id() == view_id:
                    host = h
                    break
            except Exception:
                continue
            for sl in h.slots.values():
                try:
                    ov = sl.session.output.view if sl.session and sl.session.output else None
                    if ov and ov.is_valid() and ov.id() == view_id:
                        host = h
                        break
                except Exception:
                    continue
            if host:
                break

    if host is None:
        try:
            w = sublime.active_window()
            host = get_host(w) if w else None
        except Exception:
            host = None

    if not host:
        # Fallback: session registered on view (single-slot legacy)
        session = None
        if view_id is not None and hasattr(sublime, "_claude_sessions"):
            session = sublime._claude_sessions.get(view_id)
        if session and getattr(session, "quick_mode", False):
            return None, session
        return None, None

    session = None
    # 1) Explicit executing slot (set in Session.query for quick_mode)
    try:
        esid = host.window.settings().get("claude_executing_quick_slot") if host.window else None
    except Exception:
        esid = None
    if esid and esid in host.slots:
        session = host.slots[esid].session

    # 2) Sole working slot (tool call almost always from the busy agent)
    if session is None:
        working = [
            sl for sl in host.slots.values()
            if sl.session and sl.session.working and sl.status == "live"
        ]
        if len(working) == 1:
            session = working[0].session
        elif len(working) > 1 and host.active_session and host.active_session.working:
            session = host.active_session

    # 3) Active
    if session is None:
        session = host.active_session

    if session and not getattr(session, "quick_mode", False):
        return host, None
    return host, session


def complete_quick_from_tool(
    status: str = "completed",
    message: str = "",
    view_id: int = None,
) -> dict:
    """Entry for MCP quick_done — resolve calling slot and complete it."""
    host, session = resolve_quick_session_for_tool(view_id=view_id)
    if not session or not getattr(session, "quick_mode", False):
        return {"ok": False, "error": "quick_done only works inside a Quick Agent slot"}
    if host:
        return host.complete_slot(session, status=status, message=message)
    # Session without host registry entry (should be rare)
    stop_session_bridge(session)
    return {
        "ok": True,
        "status": normalize_done_status(status),
        "message": (message or "").strip(),
        "slot": getattr(session, "_quick_slot_id", None),
        "bridge_stopped": True,
    }


# ── context / layout (shared) ─────────────────────────────────────────

def _source_view_for_context(window: sublime.Window) -> Optional[sublime.View]:
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
        session.context._add_path_ref(path)
        new_keys.add(path)
        new_keys.add(path.split("/")[-1] if "/" in path else path)
    else:
        session._quick_auto_context_keys = set()
        return
    session._quick_auto_context_keys = new_keys
    try:
        if session.output and session.output.is_input_mode():
            session.output.set_pending_context(list(session.context.items))
    except Exception:
        pass


def _remember_return_view(window: sublime.Window) -> None:
    if not window:
        return
    av = window.active_view()
    if not av or not av.is_valid():
        return
    if av.settings().get("claude_quick") or av.settings().get("claude_output"):
        return
    window.settings().set("claude_quick_return_view", av.id())


def _restore_return_view(window: sublime.Window) -> bool:
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
    try:
        n_in = len(window.views_in_group(group))
    except Exception:
        n_in = 0
    index = max(0, min(index, n_in))
    try:
        window.set_view_index(view, group, index)
    except Exception as e:
        print(f"[Claude] quick layout: {e}")


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


# ── Commands ──────────────────────────────────────────────────────────


class ClaudeQuickAgentCommand(sublime_plugin.WindowCommand):
    def run(self, prompt: str = None, config: bool = False, new_slot: bool = False) -> None:
        if config:
            self.window.run_command("claude_quick_agent_config")
            return
        if not prompt and is_quick_view_focused(self.window):
            hide_quick_view(self.window)
            return
        s = ensure_quick_session(self.window, force_new=bool(new_slot))
        if not s:
            return
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


class ClaudeQuickAgentNewSlotCommand(sublime_plugin.WindowCommand):
    def run(self) -> None:
        ensure_quick_session(self.window, force_new=True)


class ClaudeQuickAgentConfigCommand(sublime_plugin.WindowCommand):
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
            cfg.setdefault("system_prompt", default_system_prompt())
            save_config(cfg)
            stop_quick_session(self.window, close_view=True)
            sublime.status_message(f"Quick Agent → {config_label(cfg)}")
            ensure_quick_session(self.window, force_new=False)

        self.window.show_quick_panel(items, on_effort)


class ClaudeQuickAgentStopCommand(sublime_plugin.WindowCommand):
    def run(self) -> None:
        stop_quick_session(self.window, close_view=True)

    def is_enabled(self) -> bool:
        return get_host(self.window) is not None
