"""Backend registry and per-backend configuration.

Centralizes everything that varies between Claude / Codex / Copilot / DeepSeek:
- Display label and tab abbreviation
- Bridge subprocess script
- Default model fallback
- Static env var additions (e.g. DeepSeek's Anthropic-compat endpoint)
- Theme path
- Model list shown in the picker
- Availability check (e.g. codex CLI installed, deepseek API key present)

Adding a 5th backend = one entry in BACKENDS plus optional `available()`.
"""
import os
import shutil
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


def _pi_available() -> bool:
    """Check if pi CLI is installed."""
    # Check bun global install first
    bun_pi = os.path.expanduser("~/.bun/install/global/node_modules/.bin/pi")
    if os.path.isfile(bun_pi):
        return True
    return bool(shutil.which("pi"))


@dataclass
class BackendSpec:
    name: str                                    # "claude" | "codex" | "copilot" | "deepseek"
    label: str                                   # "Claude", "Codex", ...
    abbrev: str                                  # "" / "CX" / "CP" / "DS" — shown in tab title
    bridge_script: str                           # filename under bridge/
    fallback_model: str                          # used when settings has no default for this backend
    default_models: List[Tuple[str, str]]        # [(id, label), ...] for picker
    theme: str = ""                              # color_scheme path; empty = default
    static_env: Dict[str, str] = field(default_factory=dict)  # always added when backend starts
    dynamic_env: Optional[Callable[[dict], Tuple[Dict[str, str], Dict[str, str]]]] = None
    """Build env from runtime settings dict. Returns (overwrite, defaults) tuple.
    Called once per session start. Overwrite entries always replace; defaults use setdefault."""
    available: Optional[Callable[[], bool]] = None
    """Returns True if this backend can be used (CLI installed, API key set, etc)."""


def _deepseek_dynamic_env(settings: dict) -> Tuple[Dict[str, str], Dict[str, str]]:
    """DeepSeek uses the Claude bridge with Anthropic-compat endpoint + API key.

    Returns (overwrite_env, default_env). The endpoint and auth token MUST point
    to DeepSeek for this backend to work, so they're in overwrite. Model aliases
    and disable-nonessential are defaults so users can override in settings.

    Also forcibly clears ANTHROPIC_API_KEY in overwrite — if it leaked from the
    parent process (e.g. a developer who exports their Anthropic key in shell rc),
    the SDK would prefer it over ANTHROPIC_AUTH_TOKEN and send Anthropic creds to
    api.deepseek.com, which DeepSeek would reject with 401.
    """
    api_key = settings.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
    overwrite = {
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        # Empty string explicitly unsets it for the child process
        "ANTHROPIC_API_KEY": "",
    }
    if api_key:
        overwrite["ANTHROPIC_AUTH_TOKEN"] = api_key
    else:
        print("[Claude] WARNING: deepseek backend has no API key set "
              "(settings.deepseek_api_key or DEEPSEEK_API_KEY env var). "
              "Requests will likely fail with 401.")
    defaults = {
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro[1m]",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    }
    return overwrite, defaults


def _deepseek_available() -> bool:
    import sublime
    s = sublime.load_settings("ClaudeCode.sublime-settings")
    return bool(s.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY"))


def _codex_available() -> bool:
    return bool(shutil.which("codex"))


def _copilot_available() -> bool:
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.exists(os.path.join(plugin_dir, "bridge", "copilot_main.py"))


def _dsr_available() -> bool:
    """Check if dsr CLI is installed (DSR_BIN env var or in PATH)."""
    return bool(os.environ.get("DSR_BIN") or shutil.which("dsr"))


BACKENDS: Dict[str, BackendSpec] = {
    "pi": BackendSpec(
        name="pi",
        label="Pi",
        abbrev="Pi",
        bridge_script="pi_main.py",
        fallback_model="claude-sonnet-4-6",
        default_models=[
            ("claude-fable-5", "Claude Fable 5"),
            ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
            ("claude-opus-4-8", "Claude Opus 4.8"),
            ("claude-haiku-4-5", "Claude Haiku 4.5"),
            ("gpt-5.5", "GPT-5.5"),
        ],
        available=_pi_available,
    ),
    "claude": BackendSpec(
        name="claude",
        label="Claude",
        abbrev="",
        bridge_script="main.py",
        fallback_model="opus",
        default_models=[
            ("claude-fable-5", "Fable 5"),
            ("opus", "Opus 4.8"),
            ("opus@400k", "Opus 4.8 (400K context)"),
            ("claude-opus-4-8[1m]", "Opus 4.8 (1M context)"),
            ("claude-opus-4-8[1m]@400k", "Opus 4.8 (400K context)"),
            ("sonnet", "Sonnet 4.6"),
            ("haiku", "Haiku 4.5"),
            ("claude-opus-4-7", "Opus 4.7"),
            ("claude-opus-4-6", "Opus 4.6"),
            ("claude-sonnet-4-5", "Sonnet 4.5"),
        ],
    ),
    "codex": BackendSpec(
        name="codex",
        label="Codex",
        abbrev="CX",
        bridge_script="codex_main.py",
        fallback_model="gpt-5.5",
        theme="Packages/ClaudeCode/ClaudeOutput-codex.hidden-tmTheme",
        default_models=[
            ("gpt-5.5", "GPT-5.5"),
            ("gpt-5.4", "GPT-5.4"),
            ("gpt-5.4-mini", "GPT-5.4 Mini"),
            ("gpt-5.3-codex", "GPT-5.3 Codex"),
            ("o3", "O3"),
        ],
        available=_codex_available,
    ),
    "copilot": BackendSpec(
        name="copilot",
        label="Copilot",
        abbrev="CP",
        bridge_script="copilot_main.py",
        fallback_model="claude-sonnet-4-6",
        theme="Packages/ClaudeCode/ClaudeOutput-copilot.hidden-tmTheme",
        default_models=[
            ("claude-sonnet-4-6", "Sonnet 4.6"),
            ("claude-opus-4-8", "Opus 4.8"),
            ("gpt-5.5", "GPT-5.5"),
            ("gpt-5.4", "GPT-5.4"),
            ("gpt-5.3-codex", "GPT-5.3 Codex"),
            ("gpt-5-mini", "GPT-5 Mini (free)"),
        ],
        available=_copilot_available,
    ),
    "deepseek": BackendSpec(
        name="deepseek",
        label="DeepSeek",
        abbrev="DS",
        bridge_script="main.py",  # Same Claude bridge, different endpoint
        fallback_model="opus",
        default_models=[
            ("opus", "Opus → V4 Pro (1M)"),
            ("sonnet", "Sonnet → V4 Pro"),
            ("haiku", "Haiku → V4 Flash"),
        ],
        dynamic_env=_deepseek_dynamic_env,
        available=_deepseek_available,
    ),
    "dsr": BackendSpec(
        name="dsr",
        label="DSR",
        abbrev="DSR",
        bridge_script="dsr_main.py",
        fallback_model="deepseek-v4-pro",
        default_models=[
            ("pro", "V4 Pro (default)"),
            ("flash", "V4 Flash"),
            ("deepseek-v4-pro", "deepseek-v4-pro"),
            ("deepseek-v4-flash", "deepseek-v4-flash"),
        ],
        available=_dsr_available,
    ),
}


def get(name: str) -> BackendSpec:
    """Look up backend spec; falls back to claude if unknown."""
    return BACKENDS.get(name, BACKENDS["claude"])


def is_available(name: str) -> bool:
    """Check whether a backend can currently be used (defaults to True if no check)."""
    spec = BACKENDS.get(name)
    if spec is None:
        return False
    return spec.available is None or spec.available()


def default_models_dict() -> Dict[str, List[List[str]]]:
    """Backwards-compat shape for legacy DEFAULT_MODELS consumers (list-of-lists)."""
    return {name: [list(m) for m in spec.default_models] for name, spec in BACKENDS.items()}
