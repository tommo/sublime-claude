"""Kimi Code ACP helpers — pure Python (no Sublime).

Native path: `kimi acp` (Agent Client Protocol over stdio).
Distinct from custom_providers Moonshot/Kimi Anthropic-compat base_url entries.
"""
from __future__ import annotations

import os
import shutil
from typing import List, Optional, Tuple

# Default install location when not on PATH (kimi-code layout).
_DEFAULT_HOME_BIN = os.path.expanduser("~/.kimi-code/bin/kimi")

# Wire ids from `kimi acp` session/new configOptions / ~/.kimi-code/config.toml
# (do not invent model ids — only what the CLI advertises).
KIMI_MODELS: List[Tuple[str, str]] = [
    ("kimi-code/k3", "K3"),
    ("kimi-code/kimi-for-coding", "K2.7 Coding"),
    ("kimi-code/kimi-for-coding-highspeed", "K2.7 Coding Highspeed"),
]

# Short aliases → real wire ids only
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


def resolve_kimi_bin() -> str:
    """Resolve kimi executable: KIMI_BIN → ~/.kimi-code/bin → PATH.

    Prefer the default install path before PATH: Sublime often has a sparse
    PATH that omits ~/.kimi-code/bin even when the CLI is installed.
    """
    env = (os.environ.get("KIMI_BIN") or "").strip()
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    if env:
        found = shutil.which(env)
        if found:
            return found
    if os.path.isfile(_DEFAULT_HOME_BIN) and os.access(_DEFAULT_HOME_BIN, os.X_OK):
        return _DEFAULT_HOME_BIN
    which = shutil.which("kimi")
    if which:
        return which
    return env or "kimi"


def kimi_available() -> bool:
    """True when a usable kimi binary is found (not merely the string 'kimi')."""
    path = resolve_kimi_bin()
    if path and path != "kimi" and os.path.isfile(path) and os.access(path, os.X_OK):
        return True
    return bool(shutil.which("kimi"))


def agent_argv(model: Optional[str] = None) -> List[str]:
    """Argv for ACP stdio agent. Model is applied post-init via session/set_model.

    Always ends with the `acp` subcommand (not Claude SDK / main.py).
    """
    return [resolve_kimi_bin(), "acp"]


def normalize_model(model: Optional[str], default: str = "kimi-code/k3") -> str:
    if not model:
        return default
    key = model.strip()
    return MODEL_ALIASES.get(key, MODEL_ALIASES.get(key.lower(), key))
