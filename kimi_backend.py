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

# Models advertised in the picker; agent may remap via session/set_model.
# Wire ids match `kimi acp` session/new configOptions (kimi-code/…).
KIMI_MODELS: List[Tuple[str, str]] = [
    ("kimi-code/kimi-for-coding", "K2.7 Coding (default)"),
    ("kimi-code/kimi-for-coding-highspeed", "K2.7 Coding Highspeed"),
    ("kimi-for-coding", "Kimi for Coding (alias)"),
]

MODEL_ALIASES = {
    "default": "kimi-code/kimi-for-coding",
    "kimi": "kimi-code/kimi-for-coding",
    "coding": "kimi-code/kimi-for-coding",
    "kimi-for-coding": "kimi-code/kimi-for-coding",
    "highspeed": "kimi-code/kimi-for-coding-highspeed",
    "k2.5": "kimi-code/kimi-for-coding",
    "k2": "kimi-code/kimi-for-coding",
}


def resolve_kimi_bin() -> str:
    """Resolve kimi executable: KIMI_BIN → PATH → ~/.kimi-code/bin/kimi."""
    env = (os.environ.get("KIMI_BIN") or "").strip()
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    if env:
        # May be a name on PATH
        found = shutil.which(env)
        if found:
            return found
    which = shutil.which("kimi")
    if which:
        return which
    if os.path.isfile(_DEFAULT_HOME_BIN) and os.access(_DEFAULT_HOME_BIN, os.X_OK):
        return _DEFAULT_HOME_BIN
    return env or "kimi"


def kimi_available() -> bool:
    """True when a usable kimi binary is found (not merely the string 'kimi')."""
    path = resolve_kimi_bin()
    if not path or path == "kimi":
        return bool(shutil.which("kimi"))
    return os.path.isfile(path) and os.access(path, os.X_OK)


def agent_argv(model: Optional[str] = None) -> List[str]:
    """Argv for ACP stdio agent. Model is applied post-init via session/set_model.

    Always ends with the `acp` subcommand (not Claude SDK / main.py).
    """
    return [resolve_kimi_bin(), "acp"]


def normalize_model(model: Optional[str], default: str = "kimi-code/kimi-for-coding") -> str:
    if not model:
        return default
    key = model.strip()
    return MODEL_ALIASES.get(key, MODEL_ALIASES.get(key.lower(), key))
