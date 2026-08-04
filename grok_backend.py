"""Grok backend glue for the sublime-claude plugin.

Registers the `grok` backend: spawns and lifecycle-manages a bundled
grok_proxy (Anthropic<->xAI) subprocess, then points the Claude Code bridge at
it via ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN. The proxy survives bridge
sleep/restart because it is spawned with start_new_session (not tied to the
JsonRpcClient).

This module has NO top-level sublime/sublime_plugin import so the manager logic
is unit-testable. Settings are read lazily via _get_setting (sublime in-process,
else GROK_* env vars for tests).

backends.py imports this and constructs the BackendSpec; see GROK_MODELS,
grok_dynamic_env, grok_available, GrokProxyManager.
"""
import http.client
import json
import os
import signal
import subprocess
import time

# --- model catalog ----------------------------------------------------------
# Curated picker only (current defaults). Old grok-4 / 4.3 / 4-fast stay in
# ALIASES for set_model if already saved, but are not listed.
GROK_MODELS = [
    ("grok-4.5", "Grok 4.5"),
    ("grok-composer-2.5-fast", "Composer 2.5"),
    ("deepseek-v4-pro", "DeepSeek V4 Pro"),
    ("deepseek-v4-flash", "DeepSeek V4 Flash"),
]

# Never show these in the picker (legacy / noise from ACP availableModels).
_GROK_PICKER_HIDDEN = frozenset({
    "grok-4", "grok-4.0", "grok-4.1", "grok-4.2", "grok-4.3",
    "grok-4-fast", "grok-4-fast-non-reasoning", "grok-4-fast-reasoning",
    "grok-3", "grok-3-mini", "grok-2", "grok-2-mini",
    "grok-beta", "grok-vision-beta",
})

# Short aliases → wire modelId for spawn / set_model
GROK_MODEL_ALIASES = {
    "grok-4.5": "grok-4.5",
    "grok-4-fast": "grok-composer-2.5-fast",  # legacy → current fast
    "grok-composer-2.5-fast": "grok-composer-2.5-fast",
    "composer": "grok-composer-2.5-fast",
    "composer-2.5": "grok-composer-2.5-fast",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek-pro": "deepseek-v4-pro",
    "deepseek-flash": "deepseek-v4-flash",
    "ds-pro": "deepseek-v4-pro",
    "ds-flash": "deepseek-v4-flash",
    "ds": "deepseek-v4-pro",
}

DEFAULT_PORT = 8787
DEFAULT_DATA_DIR = os.path.expanduser("~/.claude/grok_proxy")
DEFAULT_BASE_URL = "https://api.x.ai/v1"


def _is_deepseek_model(model_id: str) -> bool:
    m = (model_id or "").strip().lower()
    if not m:
        return False
    return (
        m.startswith("deepseek")
        or "deepseek" in m
        or m.startswith("ds-")
        or m in ("ds", "ds-pro", "ds-flash", "deepseek-pro", "deepseek-flash")
    )


def model_supports_reasoning_effort(model_id: str) -> bool:
    """DeepSeek BYOK via Grok is chat_completions — no reasoningEffort."""
    if not (model_id or "").strip():
        return True
    return not _is_deepseek_model(model_id)


def model_supports_vision(model_id: str) -> bool:
    """DeepSeek via Grok has no image/vision — read_image would error the turn."""
    if not (model_id or "").strip():
        return True  # unknown → keep Grok default vision
    return not _is_deepseek_model(model_id)


def normalize_grok_model(model_id: str, default: str = "grok-4.5") -> str:
    if not model_id:
        return default
    key = model_id.strip()
    return GROK_MODEL_ALIASES.get(
        key, GROK_MODEL_ALIASES.get(key.lower(), key))


def _grok_config_paths():
    home = os.environ.get("GROK_HOME") or os.path.expanduser("~/.grok")
    return [
        os.path.join(home, "config.toml"),
        os.path.expanduser("~/.grok/config.toml"),
    ]


def load_grok_config_models():
    """Parse [model.<id>] blocks from ~/.grok/config.toml → [(id, label)].

    Lightweight TOML subset — only what we need for the picker. Grok Build
    already uses these for BYOK DeepSeek etc.
    """
    out = []
    seen = set()
    path = next((p for p in _grok_config_paths() if os.path.isfile(p)), None)
    if not path:
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return out

    # [model.foo] or [model."foo-bar"]
    import re
    section_re = re.compile(
        r'^\[model\.(?:"([^"]+)"|([A-Za-z0-9_.\-]+))\]\s*$', re.M)
    name_re = re.compile(r'^name\s*=\s*"([^"]*)"', re.M)
    model_re = re.compile(r'^model\s*=\s*"([^"]*)"', re.M)

    parts = section_re.split(text)
    # split yields: pre, g1, g2, body, g1, g2, body, ...
    i = 1
    while i + 2 < len(parts):
        mid = (parts[i] or parts[i + 1] or "").strip()
        body = parts[i + 2] if i + 2 < len(parts) else ""
        i += 3
        if not mid or mid in seen:
            continue
        # stop body at next top-level [section]
        cut = re.search(r'^\[', body, re.M)
        if cut:
            body = body[: cut.start()]
        display = None
        m = name_re.search(body)
        if m:
            display = m.group(1).strip()
        wire = mid
        m2 = model_re.search(body)
        if m2 and m2.group(1).strip():
            wire = m2.group(1).strip()
        label = display or wire
        # Prefer section key for picker id when it matches Grok's modelId
        # (session/set_model uses modelId from catalog = section/custom id).
        picker_id = mid
        seen.add(mid)
        seen.add(wire)
        out.append((picker_id, label))
    return out


def _picker_hide(mid: str) -> bool:
    """True if model should not appear in the UI list."""
    m = (mid or "").strip()
    if not m:
        return True
    if m in _GROK_PICKER_HIDDEN:
        return True
    # Hide other legacy SpaceXAI catalog noise; keep curated + BYOK + config.
    low = m.lower()
    if low.startswith("grok-") and m not in {x[0] for x in GROK_MODELS}:
        # Allow unknown current grok-* only if not matching old major lines
        if any(low.startswith(p) for p in (
            "grok-2", "grok-3", "grok-4", "grok-beta", "grok-vision",
        )) and not low.startswith("grok-4.5"):
            return True
    return False


def grok_picker_models(extra=None):
    """Short picker: curated + config.toml BYOK + filtered ACP extras.

    extra: ACP availableModels — only non-hidden, non-legacy ids are kept
    (e.g. deepseek already in config, or new models not in HIDDEN).
    """
    merged = []
    seen = set()
    curated = {x[0] for x in GROK_MODELS}

    def add(mid, label=None, *, force=False):
        mid = (mid or "").strip()
        if not mid or mid in seen:
            return
        if not force and _picker_hide(mid) and mid not in curated:
            return
        seen.add(mid)
        merged.append((mid, (label or mid).strip() or mid))

    for mid, label in GROK_MODELS:
        add(mid, label, force=True)
    for mid, label in load_grok_config_models():
        if not _picker_hide(mid):
            add(mid, label, force=True)
    if extra:
        for item in extra:
            if isinstance(item, dict):
                mid = item.get("modelId") or item.get("model_id") or item.get("id")
                label = item.get("name") or item.get("label") or mid
                add(mid, label)
            elif isinstance(item, (list, tuple)) and item:
                add(item[0], item[1] if len(item) > 1 else item[0])
            elif isinstance(item, str):
                add(item, item)
    return merged


# --- settings (sublime-first, env fallback) ---------------------------------
def _plugin_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _get_setting(key, default=None):
    try:
        import sublime
        v = sublime.load_settings("ClaudeCode.sublime-settings").get(key, default)
        return v if v not in (None, "") else default
    except Exception:
        # env fallback for tests / non-Sublime contexts: key uppercased
        # (e.g. grok_proxy_port -> GROK_PROXY_PORT, python_path -> PYTHON_PATH)
        return os.environ.get(key.upper().replace(".", "_"), default)


def proxy_port():
    p = _get_setting("grok_proxy_port", DEFAULT_PORT)
    try:
        return int(p)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def auth_token():
    # A token the proxy requires clients to present. If unset, the manager
    # generates a random one on first use and stashes it in the data dir so it
    # is stable across proxy restarts.
    tok = _get_setting("grok_proxy_auth_token")
    if tok:
        return tok
    return _persistent_token()


def python_path():
    return _get_setting("python_path", "python3") or "python3"


def base_url():
    return _get_setting("grok_proxy_base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL


def data_dir():
    return _get_setting("grok_proxy_data_dir", DEFAULT_DATA_DIR) or DEFAULT_DATA_DIR


def _persistent_token():
    import secrets
    d = data_dir()
    try:
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError:
        pass
    path = os.path.join(d, "proxy_token")
    try:
        with open(path, "r") as f:
            tok = f.read().strip()
            if tok:
                return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        with open(path, "w") as f:
            f.write(tok)
        os.chmod(path, 0o600)
    except OSError:
        pass
    return tok


def credential_exists(dir=None):
    """True if an xAI OAuth credential file is present in the data dir."""
    d = dir or data_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return False
    return any(n.startswith("xai-") and n.endswith(".json") for n in names)


# --- reuse the official `grok` CLI's auth (same OAuth client_id) -------------
# ~/.grok/auth.json is keyed by "https://auth.x.ai::<client_id>" and holds the
# access token (key), refresh_token, expires_at, issuer and email. Because we
# use the identical public Grok CLI client_id, these tokens are fully reusable
# — no separate login needed if the user already ran `grok` / Grok Build.
GROK_CLI_AUTH_PATH = os.path.expanduser("~/.grok/auth.json")
GROK_CLI_TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"


def grok_cli_auth_exists(path=GROK_CLI_AUTH_PATH):
    return os.path.isfile(path)


def _read_grok_cli_auth(path=GROK_CLI_AUTH_PATH):
    """Parse ~/.grok/auth.json -> inner credential dict, or None."""
    try:
        with open(path, "r") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    # keyed by "<issuer>::<client_id>"; take the first matching our client id,
    # else the first entry.
    inner = None
    for k, v in doc.items():
        if isinstance(v, dict) and v.get("oidc_client_id") == _CLIENT_ID:
            inner = v
            break
    if inner is None:
        for v in doc.values():
            if isinstance(v, dict) and v.get("key"):
                inner = v
                break
    if not inner or not inner.get("key"):
        return None
    return inner


def import_grok_cli_auth(path=GROK_CLI_AUTH_PATH, dir=None):
    """Import the official grok CLI's auth into our token store. Returns True on success."""
    from datetime import datetime, timezone
    try:
        from .grok_proxy import tokens, oauth  # package context (Sublime)
    except ImportError:
        from grok_proxy import tokens, oauth    # standalone (tests / direct run)
    inner = _read_grok_cli_auth(path)
    if not inner:
        return False
    access = inner.get("key", "")
    refresh = inner.get("refresh_token", "")
    if not access and not refresh:
        return False
    d = dir or data_dir()
    try:
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError:
        pass
    store = tokens.TokenStore(tokens.credential_file_name(
        d, email=inner.get("email"), sub=inner.get("user_id")))
    # Compute our `expired` (refresh lead) from the CLI's absolute expires_at.
    expired_epoch = None
    exp_str = inner.get("expires_at")
    if exp_str:
        try:
            dt = datetime.fromisoformat(str(exp_str).replace("Z", "+00:00"))
            expired_epoch = dt.timestamp() - oauth.REFRESH_LEAD_S
        except ValueError:
            expired_epoch = None
    store._data = {
        "type": "xai",
        "access_token": access,
        "refresh_token": refresh,
        "id_token": "",
        "token_type": "Bearer",
        "expires_in": 3600,
        "expired": tokens._utc_rfc3339(expired_epoch) if expired_epoch else tokens._utc_rfc3339(),
        "last_refresh": tokens._utc_rfc3339(),
        "email": inner.get("email", ""),
        "sub": inner.get("user_id", ""),
        "base_url": oauth.DEFAULT_BASE_URL,
        "redirect_uri": "",
        "token_endpoint": inner.get("token_endpoint") or GROK_CLI_TOKEN_ENDPOINT,
        "auth_kind": "oauth",
    }
    store._save()
    return True


# client_id mirrored here (not imported at module top to keep stdlib-only load)
_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"


# --- proxy lifecycle --------------------------------------------------------
class GrokProxyManager:
    """Spawn / health-check / stop the bundled grok_proxy subprocess.

    One instance per call; the proxy process itself is a singleton on disk
    (PID file + port). Survives bridge sleep because it is started detached.
    """

    def __init__(self, port=None, token=None, py=None, data=None, base=None):
        self.port = port if port is not None else proxy_port()
        self.token = token if token is not None else auth_token()
        self.py = py or python_path()
        self.data = data if data is not None else data_dir()
        self.base = base or base_url()
        self._proc = None

    # paths
    def pid_file(self):
        return os.path.join(self.data, "proxy.pid")

    def log_file(self):
        return os.path.join(self.data, "proxy.log")

    def base_url(self):
        return "http://127.0.0.1:%d" % self.port

    # health
    def is_healthy(self, timeout=0.6):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
            conn.request("GET", "/healthz")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            return resp.status == 200
        except Exception:
            return False

    def _running_pid(self):
        try:
            with open(self.pid_file(), "r") as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _spawn(self):
        cmd = [self.py, "-m", "grok_proxy",
               "--port", str(self.port),
               "--auth-token", self.token,
               "--data-dir", self.data,
               "--base-url", self.base]
        try:
            os.makedirs(self.data, exist_ok=True)
        except OSError:
            pass
        log = open(self.log_file(), "ab")
        kwargs = dict(
            cwd=_plugin_dir(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
        )
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS on Windows
        try:
            self._proc = subprocess.Popen(cmd, **kwargs)
        finally:
            # close our handle; the child inherited its own dup
            try:
                log.close()
            except Exception:
                pass
        try:
            with open(self.pid_file(), "w") as f:
                f.write(str(self._proc.pid))
        except OSError:
            pass

    def ensure_running(self, wait=5.0, spawn=True):
        """Return True if the proxy is (or comes up) healthy within `wait` seconds.

        If no credential is present, returns False without spawning and prints a
        login hint (the caller surfaces the failure in the session).
        """
        if self.is_healthy():
            return True
        if not credential_exists(self.data):
            # Auto-import the official grok CLI's auth if present (same client_id).
            if grok_cli_auth_exists() and import_grok_cli_auth(dir=self.data):
                print("[grok] imported login from ~/.grok/auth.json")
            elif not grok_cli_auth_exists():
                print("[grok] not logged in. Run the 'Claude: Grok Login' command "
                      "(or log in with the `grok` CLI), then restart the session.")
                return False
        # reap a stale PID if the recorded process is gone
        pid = self._running_pid()
        if pid and not self._alive(pid):
            try:
                os.remove(self.pid_file())
            except OSError:
                pass
        if spawn:
            self._spawn()
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.is_healthy(timeout=0.4):
                return True
            if self._proc is not None and self._proc.poll() is not None:
                print("[grok] proxy exited during startup; see %s" % self.log_file())
                return False
            time.sleep(0.15)
        return self.is_healthy()

    @staticmethod
    def _alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def stop(self):
        pid = self._running_pid()
        if not pid:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        # wait briefly, then SIGKILL if needed
        for _ in range(20):
            if not self._alive(pid):
                break
            time.sleep(0.1)
        if self._alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.remove(self.pid_file())
        except OSError:
            pass
        return True


# --- BackendSpec hooks ------------------------------------------------------
def grok_available():
    """Selectable in the picker iff an OAuth credential exists (ours or the grok CLI's)."""
    return credential_exists() or grok_cli_auth_exists()


def grok_dynamic_env(settings_dict):
    """Return (overwrite, defaults) env for the Claude bridge, spawning the proxy.

    Mirrors backends._custom_anthropic_dynamic_env's auth-footgun guard: the
    sibling auth var is cleared so a leaked parent-shell key can't win.
    """
    mgr = GrokProxyManager()
    running = mgr.ensure_running(wait=4.0)
    if not running:
        # Don't silently point the bridge at a dead proxy. Log prominently; the
        # session will still start (so the user sees a connection error in the
        # Claude Code output rather than a silent no-op) but the cause is clear.
        print("[grok] WARNING: grok_proxy is not reachable on %s. "
              "If you haven't logged in, run the 'Claude: Grok Login' command. "
              "Otherwise check %s" % (mgr.base_url(), mgr.log_file()))

    overwrite = {
        "ANTHROPIC_BASE_URL": mgr.base_url(),
        "ANTHROPIC_AUTH_TOKEN": mgr.token,
        "ANTHROPIC_API_KEY": "",  # clear sibling (SDK prefers API_KEY over AUTH_TOKEN)
        # Map the Claude aliases to current Grok models. opus/sonnet -> grok-4.5;
        # haiku (small/fast role) -> grok-4-fast.
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "grok-4.5",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "grok-4.5",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "grok-4-fast",
        "ANTHROPIC_MODEL": "",
        "ANTHROPIC_SMALL_FAST_MODEL": "",
    }
    defaults = {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    }
    return overwrite, defaults
