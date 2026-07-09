"""Token persistence + live credential (auto-refresh).

Ported from CLIProxyAPI/internal/auth/xai/token.go (JSON shape + filename
sanitization) and the runtime refresh logic in xai_executor.go:735-796.

The JSON shape matches the Go token store so the file is interchangeable with
CLIProxyAPI's `auths/xai-*.json` (note the deliberate "expired" key name from
token.go:23 and "sub" from token.go:26 — preserved verbatim).
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from . import oauth


def _utc_rfc3339(epoch_s=None):
    """RFC3339 UTC timestamp, matching Go's time.RFC3339 (e.g. 2026-07-09T12:00:00Z)."""
    if epoch_s is None:
        epoch_s = time.time()
    dt = datetime.fromtimestamp(epoch_s, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_rfc3339(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def sanitize_file_segment(name):
    """Keep [a-zA-Z0-9@._-], replace others with '-', trim '-' (token.go:83-104)."""
    cleaned = re.sub(r"[^A-Za-z0-9@._-]", "-", str(name))
    return cleaned.strip("-")


def credential_file_name(token_dir, email=None, sub=None):
    """Build the token file path: <dir>/xai-<email|sub|timestamp>.json (token.go:71-81)."""
    ident = sanitize_file_segment(email or sub or str(int(time.time())))
    return os.path.join(token_dir, "xai-%s.json" % ident)


class TokenStore:
    """Loads/saves a single xAI OAuth credential JSON file."""

    # Keys persisted verbatim (token.go:16-33). "expired" and "sub" are
    # deliberately spelled this way to match the Go struct tags.
    FIELDS = (
        "type", "access_token", "refresh_token", "id_token", "token_type",
        "expires_in", "expired", "last_refresh", "email", "sub", "base_url",
        "redirect_uri", "token_endpoint", "auth_kind",
    )

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _save(self):
        d = os.path.dirname(self.path)
        if d:
            try:
                os.makedirs(d, exist_ok=True)
                os.chmod(d, 0o700)
            except OSError:
                pass
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self):
        with self._lock:
            return dict(self._data)

    def is_present(self):
        with self._lock:
            return bool(self._data.get("access_token"))

    def update(self, doc, base_url=oauth.DEFAULT_BASE_URL, email=None, sub=None):
        """Merge a raw token-response dict into the stored credential and persist.

        `doc` is the dict returned by oauth.exchange_code / oauth.refresh. The
        caller may stash discovery metadata on it under _-prefixed keys.
        """
        with self._lock:
            d = self._data
            expires_in = int(doc.get("expires_in") or d.get("expires_in") or 3600)
            now = time.time()
            d["type"] = "xai"
            d["auth_kind"] = "oauth"
            if doc.get("access_token"):
                d["access_token"] = doc["access_token"]
            # keep the new refresh token if rotated; otherwise retain the old one
            if doc.get("refresh_token"):
                d["refresh_token"] = doc["refresh_token"]
            if doc.get("id_token"):
                d["id_token"] = doc["id_token"]
            d["token_type"] = doc.get("token_type") or d.get("token_type") or "Bearer"
            d["expires_in"] = expires_in
            # refresh_lead baked into stored expiry so a refresh triggered "at
            # expiry" actually fires REFRESH_LEAD_S early (pi-grok oauth.ts:427)
            d["expired"] = _utc_rfc3339(now + expires_in - oauth.REFRESH_LEAD_S)
            d["last_refresh"] = _utc_rfc3339(now)
            d["base_url"] = doc.get("base_url") or d.get("base_url") or base_url
            d["token_endpoint"] = (doc.get("_token_endpoint") or doc.get("token_endpoint")
                                   or d.get("token_endpoint"))
            d["redirect_uri"] = (doc.get("_redirect_uri") or doc.get("redirect_uri")
                                 or d.get("redirect_uri"))
            if email is None and doc.get("id_token"):
                claims = oauth.parse_jwt_identity(doc.get("id_token"))
                email = claims.get("email")
                sub = sub or claims.get("sub")
            if email:
                d["email"] = email
            if sub:
                d["sub"] = sub
            self._data = d
            self._save()
            return dict(d)

    def clear(self):
        with self._lock:
            self._data = {}
        try:
            os.remove(self.path)
        except OSError:
            pass


class Credential:
    """Live credential wrapping a TokenStore with single-flight auto-refresh.

    Shared across request threads (one per proxy process). get_access_token()
    returns a bearer token guaranteed to be valid (refreshed if within
    REFRESH_LEAD_S of expiry). 400/401/403 on refresh raises ReloginError so
    the caller can surface a re-login message.
    """

    def __init__(self, store, token_endpoint=None, refresh_lead=oauth.REFRESH_LEAD_S):
        self.store = store
        self._refresh_lock = threading.Lock()
        self._last_refresh_key = None  # single-flight keyed on refresh_token (xai.go:199-209)

    def _token_endpoint(self):
        d = self.store.get()
        return d.get("token_endpoint")

    def _needs_refresh(self, d, now=None):
        if not d.get("access_token"):
            return True
        exp = _parse_rfc3339(d.get("expired"))
        if exp is None:
            return True
        now = now or time.time()
        return now >= exp

    def get_access_token(self, now=None):
        d = self.store.get()
        now = now or time.time()
        if not self._needs_refresh(d, now):
            return d["access_token"]
        return self._refresh(d)

    def _refresh(self, d):
        refresh_token = d.get("refresh_token")
        # single-flight: concurrent callers share one upstream refresh
        key = refresh_token or "<none>"
        with self._refresh_lock:
            # re-read; another thread may have refreshed while we waited
            d = self.store.get()
            if not self._needs_refresh(d):
                return d["access_token"]
            token_ep = self._token_endpoint()
            if not token_ep or not refresh_token:
                raise oauth.ReloginError("no refresh_token or token_endpoint; re-login required")
            doc = oauth.refresh(token_ep, refresh_token)  # raises ReloginError on 400/401/403
            updated = self.store.update(doc, base_url=d.get("base_url"))
            return updated["access_token"]

    def is_logged_in(self):
        return self.store.is_present()

    def email(self):
        return self.store.get().get("email")


def login_and_store(store, **login_kwargs):
    """Run the interactive OAuth login and persist the resulting tokens."""
    doc = oauth.login(**login_kwargs)
    return store.update(doc)
