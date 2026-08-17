"""Host-owned turn state.

working/busy is true only when a known closer exists:
  live        — query() sent session/prompt; closer is the prompt RPC
  compacting  — /compact RPC returned; closer is compact-done text
  rewinding   — undo restart; closer is start() finishing

Inbound leftovers (text, thinking, synth Bash, task_notification) never
enter a busy kind. That was ◎ ⚙ task completed: working=True, no RPC, no end.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

Kind = Literal["idle", "live", "compacting", "interrupting", "rewinding"]
Inbound = Literal["drop", "paint", "paint_bg"]
Notify = Literal["hold", "surface", "query"]

# Agents that inject/auto-continue on bg complete. Host query() doubles the turn.
_SELF_WAKE_BACKENDS = frozenset({"kimi", "grok"})


@dataclass
class TurnState:
    kind: Kind = "idle"
    gen: int = 0
    awaiting_rpc: bool = False

    @property
    def busy(self) -> bool:
        # interrupting stays busy until cancel ACK — otherwise Esc + bg
        # task looks idle while the agent is still streaming.
        return self.kind in ("live", "compacting", "rewinding", "interrupting")

    @property
    def working(self) -> bool:
        return self.busy

    def begin_query(self) -> int:
        self.kind = "live"
        self.gen += 1
        self.awaiting_rpc = True
        return self.gen

    def begin_rewind(self) -> int:
        self.kind = "rewinding"
        self.gen += 1
        self.awaiting_rpc = False
        return self.gen

    def enter_compacting(self) -> None:
        if self.kind == "live":
            self.kind = "compacting"
            self.awaiting_rpc = False

    def finish_compact(self) -> bool:
        if self.kind != "compacting":
            return False
        self.kind = "idle"
        self.awaiting_rpc = False
        return True

    def end_live(self, gen: Optional[int] = None) -> bool:
        if gen is not None and gen != self.gen:
            return False
        if self.kind == "compacting":
            return False
        if self.kind in ("live", "rewinding"):
            self.kind = "idle"
            self.awaiting_rpc = False
            return True
        return False

    def begin_interrupt(self) -> bool:
        if self.kind == "idle":
            return False
        self.kind = "interrupting"
        self.awaiting_rpc = False
        return True

    def settle_interrupt(self) -> None:
        if self.kind == "interrupting":
            self.kind = "idle"
            self.awaiting_rpc = False

    def resume_stream(self) -> None:
        """Agent kept outputting after cancel ACK. Own busy until a closer."""
        if self.kind in ("live", "compacting", "rewinding"):
            return
        self.kind = "live"
        self.awaiting_rpc = False

    def inbound_action(self, event: str) -> Inbound:
        """What leftover/stream events may do. Never begins a turn."""
        if event == "thinking":
            return "drop"
        if self.kind == "interrupting":
            return "paint"
        if self.busy:
            return "paint"
        if event in ("synth_bash", "tool_use_bg"):
            return "paint_bg"
        return "paint"

    def notify_action(self, backend: str) -> Notify:
        if self.busy or self.kind == "interrupting":
            return "hold"
        if (backend or "") in _SELF_WAKE_BACKENDS:
            return "surface"
        return "query"

    def should_queue_prompt(self) -> bool:
        return self.busy or self.kind == "interrupting"
