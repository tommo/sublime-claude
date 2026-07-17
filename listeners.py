"""Claude Code event listeners for Sublime Text."""
import os
import platform
import re
import urllib.parse

import sublime
import sublime_plugin

from .core import get_session_for_view, get_active_session, in_startup_quiet
from .session import Session
from .context_parser import ContextParser, ContextMenuItem, ContextMenuHandler


def _normalize_session_name(name: str) -> str:
    """Collapse whitespace/newlines for stable title ↔ saved-name matching."""
    if not name:
        return ""
    return " ".join(str(name).replace("\r", "\n").split())


def _session_names_match(saved_name: str, tab_name: str) -> bool:
    """True if tab title (possibly truncated) refers to the saved session name."""
    a = _normalize_session_name(saved_name)
    b = _normalize_session_name(tab_name)
    if not a or not b:
        return False
    if a == b:
        return True
    # Tab titles truncate to ~24 chars (plus backend prefix already stripped)
    if len(b) >= 8 and a.startswith(b):
        return True
    if len(a) >= 8 and b.startswith(a):
        return True
    # Multiline prompts used as names: match first line only
    a0 = str(saved_name or "").split("\n", 1)[0].strip()
    b0 = str(tab_name or "").split("\n", 1)[0].strip()
    a0n, b0n = _normalize_session_name(a0), _normalize_session_name(b0)
    if a0n and b0n and len(min(a0n, b0n, key=len)) >= 8:
        if a0n == b0n or a0n.startswith(b0n) or b0n.startswith(a0n):
            return True
    return False


def _find_saved_session_for_view(view, saved_sessions: list):
    """Resolve a .sessions.json entry (or synthetic) for a restored Claude view.

    Prefer view-persisted claude_session_id (survives ST restart). Fall back to
    name / first-prompt matching. Returns dict with at least session_id, or None.
    """
    if not view:
        return None
    settings = view.settings()
    view_sid = (settings.get("claude_session_id") or "").strip()
    view_backend = (settings.get("claude_backend") or "").strip() or None

    if view_sid:
        for saved in saved_sessions:
            if saved.get("session_id") == view_sid:
                return saved
        # Id stamped on view but pruned from the 200-entry list — still resume
        return {
            "session_id": view_sid,
            "backend": view_backend or "claude",
            "name": None,
        }

    # Strip status icons / GM> / truncation ellipsis from tab title
    name = view.name() or ""
    if name.endswith("…") or name.endswith("..."):
        name = name[:-1] if name.endswith("…") else name[:-3]
    try:
        from .output import strip_title_decoration
        name = strip_title_decoration(name)
    except Exception:
        pass
    name = name.strip()

    if name and name != "Claude":
        for saved in saved_sessions:
            if not saved.get("session_id"):
                continue
            # Prefer same backend when the view still has it (avoid claude↔glm mixups)
            if view_backend and saved.get("backend") and saved.get("backend") != view_backend:
                continue
            if _session_names_match(saved.get("name") or "", name):
                return saved
        # Second pass without backend filter (backend setting may be missing)
        if view_backend:
            for saved in saved_sessions:
                if not saved.get("session_id"):
                    continue
                if _session_names_match(saved.get("name") or "", name):
                    return saved

    # Buffer first ◎ prompt line
    try:
        content = view.substr(sublime.Region(0, min(800, view.size())))
        m = re.search(r"◎ (.+?) ▶", content)
        if m:
            first_prompt = m.group(1).strip()
            fp_norm = _normalize_session_name(first_prompt)
            for saved in saved_sessions:
                if not saved.get("session_id") or not saved.get("query_count", 0):
                    continue
                if view_backend and saved.get("backend") and saved.get("backend") != view_backend:
                    continue
                sp = saved.get("first_prompt") or ""
                sn = saved.get("name") or ""
                if sp and _normalize_session_name(sp) == fp_norm:
                    return saved
                if sn and _session_names_match(sn, first_prompt):
                    return saved
    except Exception:
        pass

    return None


def settle_active_claude_view(window: sublime.Window) -> None:
    """Ensure the window's active Claude sheet is restored (sleep or live)."""
    if not window:
        return
    view = window.active_view()
    if not view or not view.settings().get("claude_output"):
        return
    s = get_session_for_view(view)
    if not s:
        ClaudeOutputEventListener(view)._restore_session(window, paint=True)
        s = get_session_for_view(view)
    if not s:
        return
    s._update_status_bar()
    s.output.set_name(s.display_name)
    if s.is_sleeping:
        s._apply_sleep_ui(touch_buffer=True)
    elif s.initialized and not s.working and not s.output.is_input_mode():
        s._enter_input_with_draft()


def settle_startup_claude_views() -> None:
    """After quiet period: one-shot restore of every Claude sheet as sleeping.

    Composer tail is already stripped in core._startup_strip_composers (t=0).
    This only registers Session + sleep chrome — never briefly enter_input.
    """
    from .output_view import OutputView
    for w in sublime.windows():
        active = w.active_view()
        active_id = active.id() if active else None
        for view in w.views():
            if not view.settings().get("claude_output"):
                continue
            if w.settings().get("claude_creating_session"):
                continue
            if view.settings().get("claude_quick"):
                continue
            # Guarantee no ◎ flash if a late buffer restore re-added it
            view.settings().set("claude_input_mode", False)
            OutputView.strip_composer_tail(view)
            is_focused = view.id() == active_id
            if get_session_for_view(view):
                s = get_session_for_view(view)
                if s and s.is_sleeping:
                    s._apply_sleep_ui(touch_buffer=is_focused)
                continue
            ClaudeOutputEventListener(view)._restore_session(
                w, paint=is_focused)

        settle_active_claude_view(w)


_last_copy_meta = None  # {file, regions: [(row_start, row_end), ...], text}


def _attach_order_bookmarks(view: sublime.View) -> None:
    """Add order-region/phantom markers for any pending orders matching this view's file.

    Idempotent (each call erases-then-adds), but guarded by a per-view sentinel so
    we only walk the order table once per view per plugin lifetime."""
    if view.settings().get("claude_orders_attached"):
        return
    path = view.file_name()
    if not path:
        return
    window = view.window()
    if not window:
        return
    from .order_table import get_table, _add_order_region
    table = get_table(window)
    if not table:
        return
    for order in table.list("pending"):
        if order.get("file_path") != path:
            continue
        _add_order_region(
            view, order["id"],
            order.get("row", 0), order.get("col", 0),
            order.get("selection_length"), order.get("prompt"),
        )
    view.settings().set("claude_orders_attached", True)

class ClaudeCodeEventListener(sublime_plugin.EventListener):
    def on_post_text_command(self, view, command_name, args):
        global _last_copy_meta
        if command_name not in ("copy", "cut"):
            return
        print(f"[Claude] on_post_text_command: {command_name}")
        path = view.file_name()
        if not path or view.is_scratch() or view.settings().get("claude_output"):
            return
        sel = view.sel()
        if not sel:
            print("[Claude] copy tracking skipped: no sel")
            return
        regions = []
        for r in sel:
            row_start = view.rowcol(r.begin())[0] + 1
            row_end = view.rowcol(r.end())[0] + 1
            regions.append((row_start, row_end))
        _last_copy_meta = {
            "file": path,
            "regions": regions,
            "text": sublime.get_clipboard(),
        }
        print(f"[Claude] copy tracked: {path} regions={regions}")

    def on_window_command(self, window: sublime.Window, command: str, args: dict):
        if command == "close_window":
            # Stop all sessions in this window
            to_remove = []
            for view_id, session in sublime._claude_sessions.items():
                if session.window == window:
                    session.stop()
                    to_remove.append(view_id)
            for view_id in to_remove:
                del sublime._claude_sessions[view_id]

        # Intercept close for claude output views
        if command in ("close", "close_file", "close_by_index"):
            view = window.active_view()
            if command == "close_by_index" and args:
                group = args.get("group", 0)
                index = args.get("index", 0)
                views = window.views_in_group(group)
                if index < len(views):
                    view = views[index]
            if view and view.settings().get("claude_output"):
                # Quick Agent soft-hide closes the sheet without killing the bridge
                if view.settings().get("claude_quick_soft_close"):
                    return None
                session = sublime._claude_sessions.get(view.id())
                if session and (session.initialized or session.is_sleeping):
                    def _ask():
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
                    return ("noop",)


    def on_activated(self, view: sublime.View) -> None:
        """Handle view activated - check if it's for context adding from goto."""
        import time
        window = view.window()
        if not window:
            return

        # Skip Claude output views
        if view.settings().get("claude_output"):
            return

        # Lazily attach order bookmarks (only walks table once per view, no-op if already attached).
        _attach_order_bookmarks(view)

        # Check if we have a pending context session
        session_view_id = window.settings().get("claude_pending_context_session")
        if not session_view_id:
            return

        # Check timestamp - only process if at least 300ms have passed
        pending_time = window.settings().get("claude_pending_context_time", 0)
        if time.time() - pending_time < 0.3:
            return

        # Clear the pending state
        window.settings().erase("claude_pending_context_session")
        window.settings().erase("claude_pending_context_time")

        # Get the session
        session = sublime._claude_sessions.get(session_view_id)
        if not session:
            return

        # Add the file as context
        path = view.file_name()
        if path:
            content = view.substr(sublime.Region(0, view.size()))
            session.add_context_file(path, content)
            sublime.status_message(f"Added context: {path.split('/')[-1]}")

            # Focus back to Claude output (user just added context from a file)
            def refocus():
                session.output.show(focus=True)
                if not session.output.is_input_mode():
                    session.output.enter_input_mode()
                if session.draft_prompt and session.output.is_input_mode():
                    if hasattr(session.output, "set_composer_text"):
                        session.output.set_composer_text(session.draft_prompt)
                    else:
                        session.output.view.run_command("append", {
                            "characters": session.draft_prompt,
                        })
                if session.output.is_input_mode():
                    session.output.focus_composer(
                        force_show=True, steal_focus=True)

            sublime.set_timeout(refocus, 100)

    def on_load(self, view: sublime.View) -> None:
        # Lazily attach order bookmarks for this file (replaces upfront sync_orders scan).
        _attach_order_bookmarks(view)

    def on_post_save(self, view: sublime.View) -> None:
        fname = view.file_name() or ""
        if os.path.basename(fname) == "ClaudeOutput.sublime-settings":
            for session in getattr(sublime, "_claude_sessions", {}).values():
                if session.output:
                    session.output._apply_output_settings()

    def on_pre_close(self, view: sublime.View) -> None:
        # Closing a workflow detail view → drop the session's refs and refocus
        # the parent session view it was opened from.
        task_id = view.settings().get("claude_workflow_view")
        if not task_id:
            return
        parent_id = view.settings().get("claude_workflow_parent")
        win = view.window()
        sess = sublime._claude_sessions.get(parent_id)
        if sess:
            if hasattr(sess, "_workflow_views"):
                sess._workflow_views.pop(task_id, None)
            if hasattr(sess, "_workflow_view_ps"):
                sess._workflow_view_ps.pop(task_id, None)
        if parent_id is not None and win:
            def refocus():
                pv = next((v for v in win.views() if v.id() == parent_id), None)
                if pv:
                    win.focus_view(pv)
            sublime.set_timeout(refocus, 0)

    def on_close(self, view: sublime.View) -> None:
        sublime.load_settings("ClaudeOutput.sublime-settings").clear_on_change(
            f"claude_output_{view.id()}"
        )
        # Soft-hide Quick Agent: sheet goes away, bridge stays in quick_agent registry
        if view.settings().get("claude_quick_soft_close"):
            sublime._claude_sessions.pop(view.id(), None)
            return
        # Clean up session when output view is closed
        if view.id() in sublime._claude_sessions:
            sublime._claude_sessions[view.id()].stop()
            del sublime._claude_sessions[view.id()]

        # Check if closed view was a terminal mode view
        tag = view.settings().get("terminus_view.tag") or ""
        if tag.startswith("claude-terminal-"):
            for vid, session in sublime._claude_sessions.items():
                if session.terminal_mode and session._terminal_tag == tag:
                    session._on_terminal_exit()
                    break


class ClaudeOutputEventListener(sublime_plugin.ViewEventListener):
    """Handle keys in the Claude output view."""

    @classmethod
    def is_applicable(cls, settings):
        return settings.get("claude_output", False)

    def on_activated(self):
        """Update status bar and title when this output view becomes active."""
        window = self.view.window()
        if not window:
            return

        # Package reload / session restore: ST can fire on_activated for *every*
        # claude_output ViewEventListener sheet. Do nothing during the startup
        # quiet window — no set_name, sel, sleep UI, or registry writes.
        if in_startup_quiet():
            return

        # ST can still fire on_activated for non-focused sheets. Stay quiet
        # unless this view is the window's real active_view — except we still
        # allow quiet orphan registration is handled only for real active below.
        active = window.active_view()
        is_real_active = bool(active and active.id() == self.view.id())
        if not is_real_active:
            return

        # Mark this as the "active" session for the window only when truly focused
        old_active = window.settings().get("claude_active_view")
        window.settings().set("claude_active_view", self.view.id())

        # Orphan after restart: one path — restore as sleeping (or start if new)
        s = get_session_for_view(self.view)
        if not s:
            if window.settings().get("claude_creating_session"):
                return
            self._restore_session(window, paint=True)
            s = get_session_for_view(self.view)
            if not s:
                return

        # Update this session's status and title
        s._update_status_bar()
        s.output.set_name(s.display_name)

        if s.is_sleeping:
            # Already restored as sleep; ensure full chrome on real focus
            s._apply_sleep_ui(touch_buffer=True)
        elif s.initialized and not s.output.is_input_mode():
            s._enter_input_with_draft()
        elif s.output.is_input_mode():
            input_start = s.output._input_start
            sel = self.view.sel()
            if len(sel) == 0:
                self.view.sel().clear()
                self.view.sel().add(sublime.Region(input_start, input_start))

        # Remove active marker from previous active session
        if old_active and old_active != self.view.id() and old_active in sublime._claude_sessions:
            old_session = sublime._claude_sessions[old_active]
            try:
                old_session.output.set_name(old_session.display_name)
            except Exception:
                pass

    def _restore_session(self, window, paint: bool = True) -> None:
        """Single path: orphan view → Session already in sleeping state.

        paint=True (focused sheet): full sleep chrome (overlay, buffer cleanup).
        paint=False (background tab): title + settings only — no focus thrash.

        Resume identity: claude_session_id on the view (preferred), else
        .sessions.json match by name/prompt. Backend comes from the saved
        entry when the view setting is missing (custom providers e.g. glm).
        """
        # Quick Agent sheets are owned by quick_agent.py
        if self.view and self.view.settings().get("claude_quick"):
            return
        from .session import Session, load_saved_sessions

        view = self.view
        if view.id() in sublime._claude_sessions:
            return
        if view.settings().get("claude_reconnecting"):
            return
        view.settings().set("claude_reconnecting", True)

        try:
            saved_sessions = load_saved_sessions()
            matched = _find_saved_session_for_view(view, saved_sessions)

            resume_id = (matched or {}).get("session_id") if matched else None
            session_name = (matched or {}).get("name") if matched else None
            resume_session_at = (matched or {}).get("resume_session_at") if matched else None

            # Backend: view stamp → saved entry → claude. Critical for glm/kimi/…
            saved_backend = (
                view.settings().get("claude_backend")
                or (matched or {}).get("backend")
                or "claude"
            )
            if not session_name:
                # Best-effort display name from tab
                raw = view.name() or ""
                if raw.endswith("…"):
                    raw = raw[:-1]
                try:
                    from .output import strip_title_decoration
                    session_name = strip_title_decoration(raw) or None
                except Exception:
                    session_name = raw or None

            session = Session(window, resume_id=resume_id, backend=saved_backend)
            session.name = session_name
            session.output.view = view
            session.draft_prompt = ""
            # BEFORE any UI: forbid ◎. Never add-then-strip.
            session._composer_allowed = False
            view.settings().set("claude_sleeping", True)
            view.settings().set("claude_input_mode", False)
            view.settings().set("claude_backend", saved_backend)
            if resume_session_at:
                session._pending_resume_at = resume_session_at

            # session_id set immediately so is_sleeping is True (no client yet)
            if resume_id:
                session.session_id = resume_id
                try:
                    session._persist_view_identity()
                except Exception:
                    pass

            sublime._claude_sessions[view.id()] = session

            # Theme (no focus side effects)
            if view.settings().get("claude_quick"):
                view.settings().set(
                    "color_scheme",
                    "Packages/ClaudeCode/ClaudeOutput-quick.hidden-tmTheme",
                )
            else:
                backend = saved_backend
                if backend and backend != "claude":
                    try:
                        from . import backends as _backends
                        spec = _backends.get(backend)
                        if getattr(spec, "theme", None):
                            view.settings().set("color_scheme", spec.theme)
                    except Exception:
                        backend_themes = {
                            "codex": "Packages/ClaudeCode/ClaudeOutput-codex.hidden-tmTheme",
                            "copilot": "Packages/ClaudeCode/ClaudeOutput-copilot.hidden-tmTheme",
                            "pi": "Packages/ClaudeCode/ClaudeOutput.hidden-tmTheme",
                        }
                        theme = backend_themes.get(backend)
                        if theme:
                            view.settings().set("color_scheme", theme)

            # Soft in-memory reset only (no reenter). Strip ST-restored ◎ if any.
            session.output.reset_active_states(soft=True)
            session._strip_sticky_composer()

            if resume_id:
                session._apply_sleep_ui(touch_buffer=paint)
            else:
                session.output.set_name(session.display_name)
                if paint:
                    session._show_overlay_phantom(
                        "\u23f8 No session to resume \u2014 Restart Session or close",
                        color="var(--yellowish)",
                        strong=True,
                    )
        finally:
            view.settings().erase("claude_reconnecting")

    # Back-compat alias
    def _reconnect_orphaned_view(self, window, quiet: bool = False):
        self._restore_session(window, paint=not quiet)

    # Buffer-mutating commands that must stay inside the ◎ input region.
    # Everything else (Cmd+D find_under_expand, multi-cursor, find, nav) is
    # allowed over history — the view is read-mostly; we only block edits.
    _INPUT_EDIT_COMMANDS = frozenset({
        "insert", "insert_snippet", "insert_best_completion",
        "left_delete", "right_delete", "delete_word", "delete_to_mark",
        "cut", "paste", "paste_and_indent", "paste_from_history",
        "swap_line_up", "swap_line_down", "join_lines", "duplicate_line",
        "permute_lines", "permute_selection", "sort_lines", "wrap_lines",
        "indent", "unindent", "reindent", "complete_under",
        "commit_completion", "auto_complete", "replace_completion_with_next_completion",
        "yank", "run_macro_file", "run_macro",
        "upper_case", "lower_case", "title_case", "swap_case",
    })

    def _park_caret_in_draft(self, session) -> int:
        """Move caret to end of ◎ draft. No scroll. Returns draft-end point."""
        view = self.view
        view.set_read_only(False)
        end = view.size()
        view.sel().clear()
        view.sel().add(sublime.Region(end, end))
        return end

    def _focus_draft_bottom(self, session) -> None:
        """Park caret in draft and scroll so ◎ + bottom hline are in view.

        Used when typing/pasting was redirected from history — ST's default
        show(caret) only frames the caret row and leaves the hline off-screen.
        Question free-text reuses _input_mode: park only, no sticky pad.
        """
        try:
            session.output.focus_composer(force_show=True)
        except Exception:
            self._park_caret_in_draft(session)

    def _sel_outside_draft(self, session) -> bool:
        """True if any selection touches conversation history (before ◎)."""
        if not session.output.is_input_mode():
            return False
        input_start = session.output._input_start
        for region in self.view.sel():
            if region.begin() < input_start or region.end() < input_start:
                return True
        return False

    def on_text_command(self, command_name, args):
        """Intercept text commands to restrict edits in input mode."""
        s = get_session_for_view(self.view)
        if not s:
            return None

        # Paste always routes through claude_paste_image — even when not yet
        # in input mode (otherwise ST paste hits a read-only history region
        # and image clipboards are dropped).
        if command_name in ("paste", "paste_and_indent"):
            if s.output.is_input_mode() and self._sel_outside_draft(s):
                # Redirect paste into draft and reveal ◎ + bottom pad
                self._focus_draft_bottom(s)
            return ("claude_paste_image", {})

        if not s.output.is_input_mode():
            return None

        # Click on/below ◎ (pad hairline or near EOF) → focus composer.
        if command_name == "drag_select":
            args = args or {}
            sublime.set_timeout(
                lambda a=dict(args), sess=s: self._focus_composer_if_click_below(sess, a),
                0,
            )
            return None

        # Plugin / navigation / selection / find — never block (Cmd+D etc.)
        if command_name.startswith("claude_"):
            return None
        if command_name not in self._INPUT_EDIT_COMMANDS:
            return None

        # Edit outside ◎ draft → rewrite into draft (never leave text in history)
        if not self._sel_outside_draft(s):
            return None

        _delete_cmds = (
            "left_delete", "right_delete", "delete_word", "delete_to_mark",
            "cut",
        )
        # Moving sel alone is not enough — ST still applies insert at the old
        # region. Re-dispatch after parking caret; scroll to true bottom so the
        # hline pad is visible (show(caret) alone only frames the caret row).
        if command_name == "insert":
            self._focus_draft_bottom(s)
            chars = (args or {}).get("characters", "")
            return ("insert", {"characters": chars})
        if command_name == "insert_snippet":
            self._focus_draft_bottom(s)
            return ("insert_snippet", args or {})
        if command_name in _delete_cmds:
            # Block delete in history; park caret only — no viewport thrash
            self._park_caret_in_draft(s)
            return ("noop", {})

        self._park_caret_in_draft(s)
        return ("noop", {})

    def _focus_composer_if_click_below(self, session, args: dict) -> None:
        """If mouse click is on/below the ◎ line, put caret in the draft."""
        try:
            if not session or not session.output or not session.output.is_input_mode():
                return
            view = self.view
            if not view or not view.is_valid():
                return
            input_start = session.output._input_start
            event = (args or {}).get("event") or {}
            click_y = None
            if "y" in event:
                try:
                    layout = view.window_to_layout((event.get("x", 0), event["y"]))
                    click_y = float(layout[1])
                except Exception:
                    click_y = None
            if click_y is not None:
                try:
                    _lx, input_y = view.text_to_layout(input_start)
                    if click_y + 1.0 >= float(input_y):
                        session.output.focus_composer(
                            force_show=True, steal_focus=True)
                        return
                except Exception:
                    pass
            # Fallback: caret landed in draft / EOF after click
            sel = view.sel()
            if sel and sel[0].begin() >= input_start:
                session.output.focus_composer(
                    force_show=True, steal_focus=True)
        except Exception:
            pass

    def on_selection_modified(self):
        """Dynamically toggle read_only based on cursor position to protect conversation history."""
        s = get_session_for_view(self.view)
        if not s:
            return

        # Only manage read_only state when in input mode
        if not s.output.is_input_mode():
            return

        # CRITICAL BUG FIX: Sublime Text requires at least one region in sel()
        # for mouse clicks to work. If sel is completely empty, restore a cursor.
        sel = self.view.sel()
        if len(sel) == 0:
            cursor_pos = self.view.size() if self.view.size() > 0 else 0
            self.view.sel().add(sublime.Region(cursor_pos, cursor_pos))
            return

        # Check if ALL cursors/selections are in the input region
        input_start = s.output._input_start
        all_in_input_region = True

        for region in sel:
            # If any part of any selection is before input_start, not safe to edit
            if region.begin() < input_start:
                all_in_input_region = False
                break

        # Keep view editable - we handle protection via on_modified (Terminus approach)
        # This allows typing anywhere, then we redirect it to input area
        if self.view.is_read_only():
            self.view.set_read_only(False)

    def on_query_context(self, key, operator, operand, match_all):
        """Provide context for key bindings."""
        if key == "claude_outside_input_area":
            s = get_session_for_view(self.view)
            if not s or not s.output.is_input_mode():
                return False

            input_start = s.output._input_start
            sel = self.view.sel()
            for region in sel:
                if region.begin() < input_start:
                    return True
            return False

        if key == "claude_submit_with_modifier":
            val = bool(sublime.load_settings("ClaudeCode.sublime-settings")
                       .get("submit_with_modifier", False))
            if operator == sublime.OP_EQUAL:
                return val == bool(operand)
            if operator == sublime.OP_NOT_EQUAL:
                return val != bool(operand)
            return None

        return None

    _in_soft_undo = False

    def on_modified(self):
        """Track modifications and redirect typing from history to input area."""
        if self._in_soft_undo:
            return

        s = get_session_for_view(self.view)
        if not s:
            return

        # Check what command just ran (Terminus trick)
        command, args, _ = self.view.command_history(0)

        # Don't redirect during input mode setup
        if not s.output.is_input_mode():
            return

        # Safety net: if an insert still landed in history (on_text_command miss),
        # pull it into the draft and scroll to true bottom (◎ + hline).
        if command == "insert" and args and "characters" in args and len(self.view.sel()) == 1:
            chars = args["characters"]
            input_start = s.output._input_start
            current_cursor = self.view.sel()[0].end()
            insert_pos = max(current_cursor - len(chars), 0)

            if insert_pos < input_start:
                self._in_soft_undo = True
                try:
                    self.view.run_command("soft_undo")
                finally:
                    self._in_soft_undo = False
                self._focus_draft_bottom(s)
                # Avoid re-entering on_modified recursion on the re-insert
                self._in_soft_undo = True
                try:
                    self.view.run_command("insert", {"characters": chars})
                finally:
                    self._in_soft_undo = False
                s.draft_prompt = s.output.get_input_text()
                return

        # Soft-undo other edits that landed in history
        elif command and not command.startswith("claude"):
            if command in self._INPUT_EDIT_COMMANDS:
                input_start = s.output._input_start
                if any(r.begin() < input_start for r in self.view.sel()):
                    self._in_soft_undo = True
                    try:
                        self.view.run_command("soft_undo")
                    finally:
                        self._in_soft_undo = False
                    # Undo-only path: park without scroll thrash (deletes etc.)
                    self._park_caret_in_draft(s)
                    s.draft_prompt = s.output.get_input_text()
                    return

        # Don't capture an AskUserQuestion free-text answer as the prompt draft
        # (question input reuses _input_mode) — it would reappear next prompt.
        if getattr(s.output, "_question_input_mode", False):
            return

        # Save draft. Whitespace-only (legacy spare ``\\n``) → empty so we never
        # rehydrate blank rows under ◎. Do NOT re-pin pad every keystroke.
        input_text = s.output.get_input_text()
        s.draft_prompt = "" if not input_text.strip() else input_text

        # Check for @ trigger at cursor
        sel = self.view.sel()
        if sel and len(sel) == 1:
            cursor = sel[0].end()
            content = self.view.substr(sublime.Region(0, self.view.size()))
            trigger = ContextParser.check_trigger(content, cursor)
            if trigger:
                self._show_context_popup(s, cursor)

    def _show_context_popup(self, session: Session, cursor: int) -> None:
        """Show @ context autocomplete via quick panel."""
        window = self.view.window()
        if not window:
            return

        # Remove the @ character first
        self.view.run_command("claude_replace", {
            "start": cursor - 1,
            "end": cursor,
            "text": ""
        })

        # Build list of open files
        import os
        open_files = []
        for v in window.views():
            if v.file_name() and not v.settings().get("claude_output"):
                name = os.path.basename(v.file_name())
                path = v.file_name()
                open_files.append((name, path))

        # Use context parser to build menu
        has_pending = bool(session.pending_context)
        pending_count = len(session.pending_context) if has_pending else 0
        menu_items = ContextParser.build_menu(open_files, has_pending, pending_count)

        # Create handler for menu selection
        def on_browse():
            self._show_file_picker(session)

        def on_clear():
            session.clear_context()
            sublime.status_message("Context cleared")

        def on_add_file(path, _content):
            # Find the view for this path and read content
            for v in window.views():
                if v.file_name() == path:
                    content = v.substr(sublime.Region(0, v.size()))
                    session.add_context_file(path, content)
                    break

        handler = ContextMenuHandler(on_browse, on_clear, on_add_file)

        def on_select(idx):
            handler.handle_selection(menu_items, idx)

        window.show_quick_panel(
            ContextParser.format_menu_items(menu_items),
            on_select,
            placeholder="Add context..."
        )

    def _handle_context_choice(self, session: Session, cursor: int, choice: str) -> None:
        """Handle context menu selection."""
        window = self.view.window()
        if not window:
            return

        active_view = None
        for v in window.views():
            if not v.settings().get("claude_output") and v.file_name():
                active_view = v
                break

        if choice == "file":
            if active_view and active_view.file_name():
                content = active_view.substr(sublime.Region(0, active_view.size()))
                session.add_context_file(active_view.file_name(), content)
        elif choice == "selection":
            if active_view:
                sel = active_view.sel()
                if sel and not sel[0].empty():
                    content = active_view.substr(sel[0])
                    path = active_view.file_name() or "untitled"
                    from .context_manager import format_line_range
                    r0 = active_view.rowcol(sel[0].begin())[0] + 1
                    r1 = active_view.rowcol(sel[0].end())[0] + 1
                    label = f"{path}:{format_line_range(r0, r1)}"
                    session.add_context_selection(label, content)
        elif choice == "open":
            for v in window.views():
                if v.file_name() and not v.settings().get("claude_output"):
                    content = v.substr(sublime.Region(0, v.size()))
                    session.add_context_file(v.file_name(), content)
        elif choice == "folder":
            if active_view and active_view.file_name():
                import os
                folder = os.path.dirname(active_view.file_name())
                session.add_context_folder(folder)
        elif choice == "browse":
            self._show_file_picker(session)
        elif choice == "clear":
            session.clear_context()
            sublime.status_message("Context cleared")

    def _show_file_picker(self, session: Session) -> None:
        """Show Ctrl+P file picker for context."""
        import time
        window = self.view.window()
        if not window:
            return

        # Store session and timestamp for the callback
        window.settings().set("claude_pending_context_session", session.output.view.id())
        window.settings().set("claude_pending_context_time", time.time())

        # Show the goto file overlay (Ctrl+P)
        window.run_command("show_overlay", {"overlay": "goto", "show_files": True})
