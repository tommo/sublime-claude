"""Backend registry and per-backend configuration.

Centralizes everything that varies between Claude / Codex / Copilot / custom
Anthropic-compatible providers:
- Display label and tab abbreviation
- Bridge subprocess script
- Default model fallback
- Static env var additions
- Theme path
- Model list shown in the picker
- Availability check (e.g. codex CLI installed, API key present)

Built-in backends live in BACKENDS. User-defined Anthropic-compatible providers
(base URL + auth + model aliases, like ccm's deepseek/glm/kimi/qwen/openrouter)
live in settings under `custom_providers` and are merged in by all_backends().
Adding a built-in = one entry in BACKENDS; adding a custom provider = one entry
in settings.custom_providers (or via the Manage Providers UI).
"""
import os
import shutil
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import grok_backend


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
    pinned: bool = True
    """If True the provider is eligible for the quick panels (Start Custom
    Provider / Set Default). Built-ins default True; custom providers default
    False (opt-in) so the seeds don't bloat the picker — pin the ones you use."""
    effort: Optional[str] = None
    """Per-provider reasoning effort override (low/medium/high/max). None → fall
    back to the global `effort` setting. Only meaningful for Anthropic-effort
    backends (claude + custom Anthropic-compatible providers)."""


def _valid_auth_token(token: str) -> bool:
    """Return True for non-empty tokens that do not look like config templates."""
    if not token:
        return False
    low = str(token).strip().lower()
    if not low:
        return False
    if low == "from_env_var" or low.startswith("sk-your-"):
        return False
    if "your" in low and "api" in low and "key" in low:
        return False
    return True


def resolve_auth_token(cfg: dict, name: str = None, settings: dict = None) -> str:
    """Resolve the effective auth token for a custom-provider cfg.

    Single source of truth shared by dynamic_env, the availability check, the
    Manage-Providers test action, and the generate-models fetcher. Order:
    inline auth_token → named auth_env_var → (deepseek legacy setting).
    Returns '' when no usable token is found.
    """
    cfg = cfg or {}
    token = (cfg.get("auth_token") or "").strip()
    if not _valid_auth_token(token):
        token = ""
    auth_env_var = (cfg.get("auth_env_var") or "").strip()
    if not token and auth_env_var:
        env_token = os.environ.get(auth_env_var, "")
        if _valid_auth_token(env_token):
            token = env_token
    if not token and name == "deepseek":
        # Legacy: old configs stored the key under top-level deepseek_api_key
        # instead of custom_providers.deepseek.auth_env_var.
        legacy = ""
        if isinstance(settings, dict):
            legacy = (settings.get("deepseek_api_key") or "").strip()
        else:
            try:
                import sublime
                legacy = (sublime.load_settings("ClaudeCode.sublime-settings")
                          .get("deepseek_api_key", "") or "").strip()
            except Exception:
                legacy = ""
        if _valid_auth_token(legacy):
            token = legacy
    return token


def _custom_anthropic_dynamic_env(settings: dict, name: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Generic dynamic-env builder for a user-defined Anthropic-compatible provider.

    Mirrors what ccm does for deepseek/glm/kimi/qwen/openrouter: point the Claude
    bridge at a third-party Anthropic-compat endpoint by overriding
    ANTHROPIC_BASE_URL + an auth var, and mapping the opus/sonnet/haiku aliases
    to the provider's models.

    Returns (overwrite_env, default_env). The endpoint and auth MUST point at
    the provider, so they're in overwrite. Model aliases and the nonessential-
    traffic flags are defaults so users can still override per-session.

    Auth footgun guard: a provider authenticates via exactly one of
    ANTHROPIC_AUTH_TOKEN (default, ccm-style) or ANTHROPIC_API_KEY. The *other*
    var is forcibly cleared in overwrite — if it leaked from the parent process
    (e.g. a developer who exports their Anthropic key in shell rc), the SDK
    would prefer ANTHROPIC_API_KEY over ANTHROPIC_AUTH_TOKEN and send the wrong
    creds to the provider, which rejects with 401.
    """
    providers = settings.get("custom_providers", {}) or {}
    cfg = providers.get(name, {}) or {}
    base_url = (cfg.get("base_url") or "").strip()
    auth_env_var = (cfg.get("auth_env_var") or "").strip()
    auth_token = resolve_auth_token(cfg, name, settings)
    auth_via_key = bool(cfg.get("auth_via_api_key", False))

    overwrite: Dict[str, str] = {}
    if base_url:
        overwrite["ANTHROPIC_BASE_URL"] = base_url
    if auth_via_key:
        if auth_token:
            overwrite["ANTHROPIC_API_KEY"] = auth_token
        overwrite["ANTHROPIC_AUTH_TOKEN"] = ""  # clear the sibling so it can't win
    else:
        if auth_token:
            overwrite["ANTHROPIC_AUTH_TOKEN"] = auth_token
        overwrite["ANTHROPIC_API_KEY"] = ""    # clear the sibling so it can't win

    if not base_url or not auth_token:
        src = f"settings.custom_providers.{name}.auth_token" if not auth_env_var else f"env ${auth_env_var}"
        print(f"[Claude] WARNING: custom provider '{name}' has no base_url or auth "
              f"({src}). Requests will likely fail with 401.")

    opus = (cfg.get("opus_model") or "").strip()
    sonnet = (cfg.get("sonnet_model") or "").strip()
    haiku = (cfg.get("haiku_model") or "").strip()
    subagent = (cfg.get("subagent_model") or "").strip()

    # Alias mappings must point at this provider, but do not force
    # ANTHROPIC_MODEL. The bridge passes --model opus/sonnet/haiku; Claude Code
    # then resolves that alias through ANTHROPIC_DEFAULT_*_MODEL. Forcing
    # ANTHROPIC_MODEL here changes that path and can pin every request to the
    # provider's heavy model. Still clear leaked single-model vars so a parent
    # shell's prior ccm selection cannot override the alias mapping.
    if opus:
        overwrite["ANTHROPIC_DEFAULT_OPUS_MODEL"] = opus
    if sonnet:
        overwrite["ANTHROPIC_DEFAULT_SONNET_MODEL"] = sonnet
    if haiku:
        overwrite["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = haiku
    overwrite["ANTHROPIC_MODEL"] = ""
    overwrite["ANTHROPIC_SMALL_FAST_MODEL"] = ""
    if subagent:
        overwrite["CLAUDE_CODE_SUBAGENT_MODEL"] = subagent
    else:
        overwrite["CLAUDE_CODE_SUBAGENT_MODEL"] = ""

    defaults: Dict[str, str] = {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    }
    # User-supplied extra env (e.g. provider-specific headers). Defaults so a
    # real env var still wins; use overwrite only for vars that must point at
    # the provider.
    extra = cfg.get("extra_env") or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            defaults[str(k)] = str(v)
    return overwrite, defaults


def _custom_provider_spec(name: str, cfg: dict) -> "BackendSpec":
    """Build a BackendSpec for a user-defined Anthropic-compatible provider."""
    label = (cfg.get("label") or name).strip() or name
    abbrev = (cfg.get("abbrev") or name[:2].upper()).strip() or name[:2].upper()
    opus = (cfg.get("opus_model") or "").strip()
    sonnet = (cfg.get("sonnet_model") or "").strip()
    haiku = (cfg.get("haiku_model") or "").strip()
    # Model picker: show the alias → real-model mapping (like the old deepseek
    # picker did). Drop empty aliases.
    default_models: List[Tuple[str, str]] = []
    if opus:
        default_models.append(("opus", f"Opus → {opus}"))
    if sonnet:
        default_models.append(("sonnet", f"Sonnet → {sonnet}"))
    if haiku:
        default_models.append(("haiku", f"Haiku → {haiku}"))
    if not default_models:
        # Fall back to plain aliases so the picker is never empty.
        default_models = [("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku")]

    def _available() -> bool:
        # A provider is usable if its base_url is set AND a token resolves
        # (inline, via its named env var, or the deepseek legacy setting).
        if not (cfg.get("base_url") or "").strip():
            return False
        return bool(resolve_auth_token(cfg, name))

    # Closure captures `name`; the builder reads the live settings dict at call
    # time so edits to custom_providers take effect on the next session start.
    def _dyn(settings: dict):
        return _custom_anthropic_dynamic_env(settings, name)

    return BackendSpec(
        name=name,
        label=label,
        abbrev=abbrev,
        bridge_script="main.py",  # shares the Claude bridge, different endpoint
        fallback_model="opus",
        default_models=default_models,
        dynamic_env=_dyn,
        available=_available,
        pinned=bool(cfg.get("pinned", False)),  # opt-in for the quick panels
        effort=((cfg.get("effort") or "").strip() or None),  # per-provider override
    )


def _load_custom_providers() -> Dict[str, "BackendSpec"]:
    """Read custom_providers from settings → {name: BackendSpec}.

    Done fresh on every call so the UI and session start always see the latest
    config (Sublime's settings cache is the source of truth; we never hold a
    stale copy).
    """
    try:
        import sublime
        s = sublime.load_settings("ClaudeCode.sublime-settings")
        providers = s.get("custom_providers", {}) or {}
    except Exception:
        return {}
    out: Dict[str, "BackendSpec"] = {}
    if not isinstance(providers, dict):
        return out
    for name, cfg in providers.items():
        if not isinstance(cfg, dict):
            continue
        try:
            out[str(name)] = _custom_provider_spec(str(name), cfg)
        except Exception as e:
            print(f"[Claude] skipping malformed custom provider '{name}': {e}")
    return out


def _codex_available() -> bool:
    return bool(shutil.which("codex"))


def _copilot_available() -> bool:
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.exists(os.path.join(plugin_dir, "bridge", "copilot_main.py"))


def _dsr_available() -> bool:
    """Check if dsr CLI is installed (DSR_BIN env var or in PATH)."""
    return bool(os.environ.get("DSR_BIN") or shutil.which("dsr"))


def _grok_available() -> bool:
    """Native Grok Build ACP: `grok` CLI on PATH (or GROK_BIN)."""
    return bool(os.environ.get("GROK_BIN") or shutil.which("grok"))


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
    # Native Grok Build via ACP (`grok agent stdio`).
    "grok": BackendSpec(
        name="grok",
        label="Grok",
        abbrev="GR",
        bridge_script="grok_main.py",
        fallback_model="grok-4.5",
        default_models=[
            ("grok-4.5", "Grok 4.5"),
            ("grok-composer-2.5-fast", "Composer 2.5"),
        ],
        available=_grok_available,
        pinned=True,
    ),
    # xAI via Anthropic-compat proxy + Claude Code bridge (legacy path).
    "grok_cc": BackendSpec(
        name="grok_cc",
        label="Grok (Claude Code)",
        abbrev="GCC",
        bridge_script="main.py",
        fallback_model="grok-4.5",
        default_models=grok_backend.GROK_MODELS,
        dynamic_env=grok_backend.grok_dynamic_env,
        available=grok_backend.grok_available,
        pinned=True,
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
    # Note: DeepSeek is no longer a hardcoded built-in — it ships as a seeded
    # entry under settings.custom_providers (see ClaudeCode.sublime-settings).
    # Any Anthropic-compatible provider is added the same way.
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


def all_backends() -> Dict[str, "BackendSpec"]:
    """All usable backends: built-ins (BACKENDS) + user custom_providers.

    Custom providers override built-ins on name collision (rare; lets a user
    replace a built-in's config). Fresh-merged on every call so UI and session
    start always see the latest settings.
    """
    merged: Dict[str, "BackendSpec"] = dict(BACKENDS)
    try:
        merged.update(_load_custom_providers())
    except Exception as e:
        print(f"[Claude] failed to load custom providers: {e}")
    return merged


def get(name: str) -> "BackendSpec":
    """Look up backend spec; falls back to claude if unknown."""
    return all_backends().get(name, BACKENDS["claude"])


def is_available(name: str) -> bool:
    """Check whether a backend can currently be used (defaults to True if no check)."""
    spec = all_backends().get(name)
    if spec is None:
        return False
    return spec.available is None or spec.available()


def default_models_dict() -> Dict[str, List[List[str]]]:
    """Backwards-compat shape for legacy DEFAULT_MODELS consumers (list-of-lists)."""
    return {name: [list(m) for m in spec.default_models] for name, spec in all_backends().items()}
