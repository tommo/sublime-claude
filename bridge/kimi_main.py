"""Kimi Code ACP bridge — thin adapter over AcpBridge.

Spawns `kimi acp` (or `$KIMI_BIN acp`) over stdio. Auth via terminal-auth
login when advertised; otherwise relies on existing kimi-code credentials.
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional

_BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_BRIDGE_DIR)
sys.path.insert(0, _BRIDGE_DIR)
sys.path.insert(0, _PLUGIN_DIR)

from acp_base import AcpBridge, run_bridge  # noqa: E402

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
    DEFAULT_MODEL = "kimi-code/kimi-for-coding"
    LOG_PATH = os.path.join(
        os.environ.get("TMPDIR")
        or os.environ.get("TEMP")
        or os.environ.get("TMP")
        or "/tmp",
        "kimi_bridge.log",
    )

    PERM_TO_MODE = {
        "default": "default",
        "acceptEdits": "acceptEdits",
        "auto": "default",
        "bypassPermissions": "bypassPermissions",
        "plan": "plan",
        "dontAsk": "default",
    }
    MODE_TO_PERM = {
        "default": "default",
        "plan": "plan",
        "bypassPermissions": "bypassPermissions",
        "acceptEdits": "acceptEdits",
        "agent": "acceptEdits",
        "ask": "default",
    }
    MODEL_ALIASES = {
        "default": "kimi-code/kimi-for-coding",
        "kimi": "kimi-code/kimi-for-coding",
        "coding": "kimi-code/kimi-for-coding",
        "kimi-for-coding": "kimi-code/kimi-for-coding",
        "highspeed": "kimi-code/kimi-for-coding-highspeed",
        "kimi-code/kimi-for-coding": "kimi-code/kimi-for-coding",
        "kimi-code/kimi-for-coding-highspeed": "kimi-code/kimi-for-coding-highspeed",
    }
    # Prefer ToolKind from ACP; map common titles / ids to Claude formatters.
    TOOL_TO_CANONICAL = {
        "read": "Read",
        "Read": "Read",
        "read_file": "Read",
        "ReadFile": "Read",
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
        "fetch": "WebFetch",
        "WebFetch": "WebFetch",
        "think": "Think",
        "todo": "TodoWrite",
        "TodoWrite": "TodoWrite",
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


def main() -> None:
    run_bridge(KimiBridge())


if __name__ == "__main__":
    main()
