"""Idle-triggered prompt loop for sessions.

A LoopController is owned by one Session. When active, it re-fires its prompt
each time the session goes idle, optionally enforcing a minimum gap since the
last fire. Loops never interrupt an in-flight query — they only fire when the
session has just transitioned to idle (or, on `start()`, if already idle).

Lifecycle hooks:
    - Session.__init__              → LoopController(session)
    - User input "loop:..."         → loop.start(prompt, interval_sec)
    - "/loop:cancel" or session end → loop.stop()
    - Session goes idle (in _on_done) → loop.on_idle()
"""
import time
from typing import Optional, TYPE_CHECKING

import sublime

from .constants import LOOP_PREFIX

if TYPE_CHECKING:
    from .session import Session


def fmt_duration(sec: int) -> str:
    """Human-readable seconds (e.g. 30s, 5m, 2h)."""
    if sec >= 3600 and sec % 3600 == 0:
        return f"{sec // 3600}h"
    if sec >= 60 and sec % 60 == 0:
        return f"{sec // 60}m"
    return f"{sec}s"


class LoopController:
    """Owns the active loop state for one session.

    State (`self.active`) is `None` when no loop is running; otherwise a dict
    with prompt, min_interval_sec, last_fire, token. `token` is bumped each
    start/stop so deferred timers from a stale loop are no-ops.
    """

    def __init__(self, session: "Session"):
        self.session = session
        self.active: Optional[dict] = None
        self._token: int = 0  # increments to invalidate stale timers

    # ── Status / introspection ──────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self.active is not None

    def status_label(self) -> Optional[str]:
        """Returns "loop" or "loop ≥5m" for status bar; None when not active."""
        if not self.active:
            return None
        min_iv = self.active.get("min_interval_sec", 0)
        return f"loop ≥{fmt_duration(min_iv)}" if min_iv else "loop"

    # ── Public API ──────────────────────────────────────────────────────

    def start(self, prompt: str, interval_sec: Optional[int] = None) -> None:
        """Start an idle-triggered loop. `interval_sec` is the MINIMUM gap between fires."""
        if not prompt:
            self._log("start aborted: empty prompt")
            return
        if self.active:
            self._log(f"replacing existing loop (was: {self.active['prompt'][:40]!r})")
        self.stop(silent=True)
        min_interval = interval_sec or 0  # 0 = fire immediately on idle
        self._token += 1
        self.active = {
            "prompt": prompt,
            "min_interval_sec": min_interval,
            "token": self._token,
            "last_fire": 0.0,
        }
        gap = fmt_duration(min_interval) if min_interval else "no min"
        self._log(f"started: min_interval={gap}, token={self._token}, prompt={prompt[:80]!r}")
        short = prompt[:80] + "…" if len(prompt) > 80 else prompt
        self.session.output.text(f"\n{LOOP_PREFIX}loop started (min gap: {gap}): {short}\n")
        self.session._update_status_bar()
        # Trigger immediately if currently idle
        if not self.session.working:
            self.on_idle()

    def stop(self, silent: bool = False) -> None:
        """Cancel the active loop. silent=True for cleanup paths (sleep/stop)."""
        if not self.active:
            return
        self._log(f"stopped (token={self.active['token']}, silent={silent})")
        self._token += 1  # invalidate any pending deferred fire
        self.active = None
        if not silent:
            self.session.output.text(f"\n{LOOP_PREFIX}loop cancelled\n")
        self.session._update_status_bar()
        # Re-enter input mode so user can type again (only if idle and not silent cleanup)
        if not silent:
            self.session.resume_input_mode()

    def on_idle(self) -> None:
        """Hook called when session transitions to idle. Fires the prompt if due."""
        if not self.active:
            return
        if self.session.working:
            self._log("on_idle skipped: session working")
            return
        loop = self.active
        token = loop["token"]
        elapsed = time.time() - loop["last_fire"]
        remaining = loop["min_interval_sec"] - elapsed
        if remaining > 0:
            wait_ms = int(remaining * 1000) + 100  # tiny buffer
            self._log(f"deferred {fmt_duration(int(remaining))} (min interval not elapsed)")
            sublime.set_timeout(lambda: self._deferred_fire(token), wait_ms)
            return
        self._fire(token)

    # ── Internal ────────────────────────────────────────────────────────

    def _deferred_fire(self, token: int) -> None:
        """Resume after a deferred-wait timeout. Re-validate state before firing."""
        if not self.active or self.active.get("token") != token:
            self._log(f"deferred fire skipped: stale token={token}")
            return
        if self.session.working:
            self._log("deferred fire: session became busy, will retry on next idle")
            return
        self._fire(token)

    def _fire(self, token: int) -> None:
        """Send the prompt as a new query."""
        if not self.active or self.active.get("token") != token:
            return
        if self.session.working:
            self._log("fire aborted: session working")
            return
        loop = self.active
        prompt = loop["prompt"]
        loop["last_fire"] = time.time()
        self._log(f"fire (token={token}, backend={self.session.backend}, prompt={prompt[:60]!r})")
        try:
            if self.session.client and self.session.initialized:
                self.session.query(prompt, display_prompt=f"{LOOP_PREFIX}{prompt[:80]}")
            else:
                self._log(f"fire skipped: client={bool(self.session.client)}, initialized={self.session.initialized}")
        except Exception as e:
            self._log(f"fire error: {e}")
        # No re-schedule here — the next fire is driven by the next idle transition
        # (Session._on_done calls loop.on_idle()).

    def _log(self, msg: str) -> None:
        sid = (self.session.session_id or "?")[:8]
        print(f"[Claude loop {sid}] {msg}")
