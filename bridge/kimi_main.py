"""Kimi Code ACP bridge — thin adapter over AcpBridge.

Spawns `kimi acp` (or `$KIMI_BIN acp`) over stdio. Auth via terminal-auth
login when advertised; otherwise relies on existing kimi-code credentials.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Dict, List, Optional

_BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_BRIDGE_DIR)
sys.path.insert(0, _BRIDGE_DIR)
sys.path.insert(0, _PLUGIN_DIR)

from acp_base import AcpBridge, run_bridge  # noqa: E402
from rpc_helpers import send_notification  # noqa: E402

try:
    from kimi_backend import (  # noqa: E402
        agent_argv as _kimi_agent_argv,
        normalize_model as _kimi_normalize_model,
        resolve_kimi_bin,
    )
except ImportError:
    # Fallback if package layout differs in bridge-only spawn
    import shutil

    def resolve_kimi_bin() -> str:
        return (
            (os.environ.get("KIMI_BIN") or "").strip()
            or shutil.which("kimi")
            or os.path.expanduser("~/.kimi-code/bin/kimi")
            or "kimi"
        )

    def _kimi_agent_argv(model=None):
        return [resolve_kimi_bin(), "acp"]

    def _kimi_normalize_model(model, default="kimi-for-coding"):
        return (model or default).strip() or default


class KimiBridge(AcpBridge):
    BACKEND_NAME = "kimi"
    DEFAULT_MODEL = "kimi-code/k3"  # matches kimi default_model / ACP currentValue
    LOG_PATH = os.path.join(
        os.environ.get("TMPDIR")
        or os.environ.get("TEMP")
        or os.environ.get("TMP")
        or "/tmp",
        "kimi_bridge.log",
    )

    # Modes: open-source kimi-cli only advertises ``default``
    # (server.py set_session_mode asserts mode_id == "default").
    # Kimi Code may also advertise plan|auto|yolo — apply_mode resolves
    # against session availableModes and never sends acceptEdits.
    PERM_TO_MODE = {
        "default": "default",
        "acceptEdits": "yolo",   # Code: yolo; OSS falls back to default
        "auto": "auto",
        "bypassPermissions": "auto",
        "plan": "plan",
        "dontAsk": "auto",
    }
    MODE_TO_PERM = {
        "default": "default",
        "plan": "plan",
        "auto": "bypassPermissions",
        "yolo": "acceptEdits",
        "agent": "acceptEdits",
        "ask": "default",
    }
    # Only real wire ids from kimi config + short aliases
    MODEL_ALIASES = {
        "default": "kimi-code/k3",
        "k3": "kimi-code/k3",
        "kimi-code/k3": "kimi-code/k3",
        "k2.7": "kimi-code/kimi-for-coding",
        "kimi-for-coding": "kimi-code/kimi-for-coding",
        "kimi-code/kimi-for-coding": "kimi-code/kimi-for-coding",
        "highspeed": "kimi-code/kimi-for-coding-highspeed",
        "kimi-for-coding-highspeed": "kimi-code/kimi-for-coding-highspeed",
        "kimi-code/kimi-for-coding-highspeed": "kimi-code/kimi-for-coding-highspeed",
    }
    # Prefer ToolKind from ACP; map common titles / ids to Claude formatters.
    # Kimi often sends PascalCase titles (TodoList) or kind=other without meta.
    TOOL_TO_CANONICAL = {
        "read": "Read",
        "Read": "Read",
        "read_file": "Read",
        "ReadFile": "Read",
        "ReadMediaFile": "Read",
        "edit": "Edit",
        "Edit": "Edit",
        "search_replace": "Edit",
        "write": "Write",
        "Write": "Write",
        "execute": "Bash",
        "Bash": "Bash",
        "shell": "Bash",
        "Shell": "Bash",
        "run": "Bash",
        "search": "Grep",
        "Grep": "Grep",
        "grep": "Grep",
        "glob": "Glob",
        "Glob": "Glob",
        "list_dir": "Glob",
        "ListDir": "Glob",
        "fetch": "WebFetch",
        "WebFetch": "WebFetch",
        "think": "Think",
        "todo": "TodoWrite",
        "TodoWrite": "TodoWrite",
        "TodoList": "TodoWrite",
        "TodoRead": "TodoWrite",
        # Subagents — format like Claude Task (description + type)
        "Agent": "Task",
        "AgentSwarm": "Task",
        "agent": "Task",
        "spawn_subagent": "Task",
        "Task": "Task",
        "TaskOutput": "TaskGet",
        "TaskGet": "TaskGet",
        "AskUserQuestion": "AskUserQuestion",
        "ask_user_question": "AskUserQuestion",
        "EnterPlanMode": "EnterPlanMode",
        "ExitPlanMode": "ExitPlanMode",
        "update_goal": "update_goal",
        "goal_verdict": "goal_verdict",
    }

    def agent_argv(self) -> List[str]:
        # Always ACP stdio — never Claude SDK main.py
        return list(_kimi_agent_argv(self.model))

    def normalize_model(self, model: Optional[str]) -> str:
        return _kimi_normalize_model(model, default=self.DEFAULT_MODEL)

    def spawn_env(self) -> Optional[Dict[str, str]]:
        env = dict(os.environ)
        # Ensure default install dir is findable for child tools
        home_bin = os.path.expanduser("~/.kimi-code/bin")
        path = env.get("PATH") or ""
        if os.path.isdir(home_bin) and home_bin not in path.split(os.pathsep):
            env["PATH"] = home_bin + os.pathsep + path
        return env

    async def after_agent_initialize(self, init_result: dict) -> None:
        methods = init_result.get("authMethods") or self._auth_methods or []
        method_ids = [m.get("id") for m in methods if isinstance(m, dict)]
        preferred = None
        meta = init_result.get("_meta") or {}
        preferred = meta.get("defaultAuthMethodId")
        if preferred not in method_ids:
            # Prefer non-interactive / already-logged-in methods first
            for cand in ("cached_token", "login", "terminal"):
                if cand in method_ids:
                    preferred = cand
                    break
            if preferred not in method_ids:
                preferred = method_ids[0] if method_ids else None
        if not preferred:
            self.log("no auth methods; relying on existing kimi-code credentials")
            return
        # terminal-auth login is interactive — skip auto-auth for that; user
        # must have run `kimi login` already (doctor-clean).
        method = next(
            (m for m in methods if isinstance(m, dict) and m.get("id") == preferred),
            None,
        )
        mtype = (method or {}).get("type") or ""
        if mtype == "terminal" or preferred == "login":
            self.log(
                f"auth method {preferred!r} is terminal/login — "
                "skipping auto-authenticate (use `kimi login` if unauthenticated)"
            )
            return
        try:
            await self._send_acp("authenticate", {"methodId": preferred})
            self.log(f"authenticated via {preferred}")
        except Exception as e:
            self.log(f"authenticate({preferred}) failed: {e}")

    async def handle_interrupt(self, req_id: Optional[int],
                                params: dict) -> None:
        """Cancel turn; second Esc hard-kills hung Agent subagents.

        Kimi Code Agent tools can ignore session/cancel for 30–60+ min while
        the parent session/prompt never returns — UI stays busy forever.
        First interrupt: cancel + force local prompt settle.
        Second interrupt within 15s: SIGKILL the kimi-code process (session
        must be reopened — better than a stuck hour-long spinner).
        """
        now = time.time()
        last = float(getattr(self, "_kimi_last_interrupt_at", 0) or 0)
        count = int(getattr(self, "_kimi_interrupt_streak", 0) or 0)
        if now - last < 15.0:
            count += 1
        else:
            count = 1
        self._kimi_last_interrupt_at = now
        self._kimi_interrupt_streak = count

        # Longer wait than default — subagents need cancel to propagate
        fut = self._prompt_fut
        active = fut is not None and not fut.done()
        has_query = self._query_req_id is not None
        if not active and not has_query and count < 2:
            self.file_log("interrupt: idle (no in-flight prompt)")
            send_result(req_id, {"status": "interrupted"})
            return

        for tid in list(self._terminals):
            try:
                await self._terminal_close(tid)
            except Exception:
                pass

        await self._cancel_agent_turn(
            reason="interrupt", wait_s=3.0, settle_s=0.4, force_local=True)

        for pid, pfut in list(self.pending_permissions.items()):
            if pfut and not pfut.done():
                pfut.set_result({"kind": "denied-interactively-by-user"})
            self.pending_permissions.pop(pid, None)
        for qid, qfut in list(self.pending_questions.items()):
            if qfut and not qfut.done():
                qfut.set_result(None)
            self.pending_questions.pop(qid, None)
        for pid, pfut in list(self.pending_plan_approvals.items()):
            if pfut and not pfut.done():
                pfut.set_result(None)
            self.pending_plan_approvals.pop(pid, None)

        # Hard kill if still hung after cancel (Agent subagent ignores cancel)
        still_busy = (
            (self._prompt_fut is not None and not self._prompt_fut.done())
            or count >= 2
        )
        if still_busy and count >= 2:
            await self._kimi_hard_kill_agent(reason="double_interrupt")
            self._kimi_interrupt_streak = 0

        send_result(req_id, {"status": "interrupted"})

    async def _kimi_hard_kill_agent(self, *, reason: str = "") -> None:
        """SIGKILL kimi-code ACP process so a stuck Agent cannot hold the UI."""
        proc = getattr(self, "proc", None)
        if proc is None:
            self.file_log(f"kimi hard kill ({reason}): no proc")
            return
        pid = getattr(proc, "pid", None)
        self.file_log(f"kimi hard kill ({reason}): pid={pid}")
        try:
            if proc.returncode is None:
                proc.kill()
        except Exception as e:
            self.file_log(f"kimi hard kill failed: {e}")
        # Settle local waiters
        fut = self._prompt_fut
        if fut is not None and not fut.done():
            fut.set_result({"stopReason": "cancelled"})
        self._prompt_cancelled = True
        self._cancel_in_flight = True
        send_notification("message", {
            "type": "system",
            "subtype": "init",
            "data": {
                "message": (
                    "Kimi agent process killed (stuck turn). "
                    "Restart this session to continue."
                ),
            },
        })


def main() -> None:
    run_bridge(KimiBridge())


if __name__ == "__main__":
    main()
