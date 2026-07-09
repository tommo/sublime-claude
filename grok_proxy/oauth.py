"""xAI SuperGrok OAuth2 + PKCE flow — stdlib port.

Ported from CLIProxyAPI/internal/auth/xai/* and sdk/auth/xai.go, cross-checked
against pi-grok/oauth.ts (stricter id_token validation, CORS on callback,
port-0 fallback, 400/401/403 fatal on refresh).

References (file:line in the Go repo unless noted):
  constants      internal/auth/xai/types.go:6-23
  discover       internal/auth/xai/xai.go:100-141   (pi-grok oauth.ts:140-160)
  pkce           internal/auth/xai/pkce.go:11-20
  authorize url  internal/auth/xai/xai.go:67-97     (pi-grok oauth.ts:440-470)
  callback srv   sdk/auth/xai.go:241-282            (pi-grok oauth.ts:171-277)
  token exchange internal/auth/xai/xai.go:143-179   (pi-grok oauth.ts:380-434)
  id_token check pi-grok oauth.ts:327-376  (Go only extracts email/sub)
  refresh        internal/auth/xai/xai.go:182-219   (pi-grok oauth.ts:560-630)

Limitation: id_token signature is NOT verified (no JWKS fetch). The
iss/aud/nonce/exp checks close the practical token-injection vectors for a
loopback OAuth code flow. Matches both reference implementations.
"""
import base64
import hashlib
import http.client
import json
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- constants (internal/auth/xai/types.go:6-23) -----------------------------
DEFAULT_BASE_URL = "https://api.x.ai/v1"
ISSUER = "https://auth.x.ai"
DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 56121
REDIRECT_PATH = "/callback"
REFRESH_LEAD_S = 300          # refresh 5 min before expiry (types.go:25)
CALLBACK_TIMEOUT_S = 300      # overall login timeout (sdk/auth/xai.go:118)
REFRESH_SKEW_S = 30           # id_token exp clock skew (pi-grok oauth.ts:375)

ALLOWED_HOSTS = ("x.ai", "auth.x.ai", "accounts.x.ai")  # pi-grok oauth.ts:109-133


class ReloginError(Exception):
    """Refresh failed fatally (400/401/403 or no refresh token) — user must re-login."""


class OAuthError(Exception):
    """Generic OAuth flow error."""


# --- endpoint validation (pi-grok oauth.ts:109-133) --------------------------
def _validate_endpoint(url):
    """Endpoint must be https and host must be x.ai / auth.x.ai / accounts.x.ai / *.x.ai."""
    p = urllib.parse.urlparse(url)
    if p.scheme != "https":
        raise OAuthError("endpoint must be https: %s" % url)
    host = (p.hostname or "").lower()
    if not host:
        raise OAuthError("endpoint has no host: %s" % url)
    if host in ALLOWED_HOSTS or host.endswith(".x.ai"):
        return
    raise OAuthError("endpoint host not allowed: %s" % host)


# --- discovery (xai.go:100-141) ----------------------------------------------
def discover(discovery_url=DISCOVERY_URL, timeout=15):
    """GET the OIDC discovery doc; return (authorization_endpoint, token_endpoint)."""
    _validate_endpoint(discovery_url)
    p = urllib.parse.urlparse(discovery_url)
    conn = http.client.HTTPSConnection(p.hostname, port=p.port or 443, timeout=timeout)
    try:
        conn.request("GET", p.path or "/", headers={"Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        if resp.status != 200:
            raise OAuthError("discovery %d: %s" % (resp.status, body[:200]))
        doc = json.loads(body)
    finally:
        conn.close()
    auth_ep = doc.get("authorization_endpoint")
    token_ep = doc.get("token_endpoint")
    if not auth_ep or not token_ep:
        raise OAuthError("discovery doc missing endpoints")
    _validate_endpoint(auth_ep)
    _validate_endpoint(token_ep)
    return auth_ep, token_ep


# --- PKCE (pkce.go:11-20) ----------------------------------------------------
def generate_pkce():
    """Return (verifier, challenge) per RFC 7636 S256, matching the Go 96-byte form.

    verifier = base64url-nopad(96 random bytes) -> 128 chars
    challenge = base64url-nopad(sha256(verifier ASCII))
    """
    raw = os.urandom(96)
    verifier = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _random_state():
    """State/nonce token (misc.GenerateRandomState; pi-grok base64url(16 bytes))."""
    return secrets.token_urlsafe(16)


# --- authorize URL (xai.go:67-97) -------------------------------------------
def build_authorize_url(authorization_endpoint, redirect_uri, verifier, state, nonce):
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    params = [
        ("response_type", "code"),
        ("client_id", CLIENT_ID),
        ("redirect_uri", redirect_uri),
        ("scope", SCOPE),
        ("code_challenge", challenge),
        ("code_challenge_method", "S256"),
        ("state", state),
        ("nonce", nonce),
        ("plan", "generic"),
        ("referrer", "sublime-claude"),
    ]
    sep = "&" if "?" in authorization_endpoint else "?"
    return authorization_endpoint + sep + urllib.parse.urlencode(params)


# --- loopback callback server (sdk/auth/xai.go:241-282; pi-grok 171-277) -----
class _CallbackResult:
    def __init__(self):
        self.done = threading.Event()
        self.code = None
        self.state = None
        self.error = None
        self.error_description = None


class _CallbackHandler(BaseHTTPRequestHandler):
    # set as class attrs by start_callback_server
    result = None
    expected_state = None

    def log_message(self, *args):
        pass  # silence stderr noise

    def _cors_headers(self):
        origin = self.headers.get("Origin", "")
        if origin in ("https://accounts.x.ai", "https://auth.x.ai"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != REDIRECT_PATH:
            self.send_response(404)
            self.end_headers()
            return
        q = urllib.parse.parse_qs(parsed.query)
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [""])[0]
        err = (q.get("error") or [""])[0]
        err_desc = (q.get("error_description") or [""])[0]

        if not self.result.done.is_set():
            # Browser callback must carry the expected state (CSRF). A missing or
            # mismatching state is rejected; only the manual bare-code paste path
            # opts out of state (see _apply_manual_paste, which trusts PKCE).
            if self.expected_state and state != self.expected_state:
                self.result.error = "state mismatch" if state else "missing state"
            elif err:
                self.result.error = err
                self.result.error_description = err_desc
            else:
                self.result.code = code
                self.result.state = state
            self.result.done.set()

        ok = not self.result.error
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._cors_headers()
        self.end_headers()
        if ok:
            self.wfile.write(b"<html><body><h1>xAI authorization received.</h1>"
                             b"You can close this tab.</body></html>")
        else:
            self.wfile.write(b"<html><body><h1>xAI authorization failed.</h1>"
                             b"You can close this tab.</body></html>")


def start_callback_server(port=CALLBACK_PORT, expected_state=None, host=CALLBACK_HOST,
                          timeout=CALLBACK_TIMEOUT_S):
    """Start the loopback callback server. Returns (actual_port, server, result).

    If `port` is taken, falls back to an OS-assigned port (pi-grok oauth.ts:233-239).
    """
    result = _CallbackResult()

    class Handler(_CallbackHandler):
        pass
    Handler.result = result
    Handler.expected_state = expected_state

    last_err = None
    srv = None
    for attempt_port in ([port, 0] if port else [0]):
        try:
            srv = ThreadingHTTPServer((host, attempt_port), Handler)
            srv.timeout = 1
            break
        except OSError as e:
            last_err = e
            srv = None
    if srv is None:
        raise OAuthError("could not bind callback server: %s" % last_err)

    actual_port = srv.server_address[1]

    def _timeout_watchdog():
        if not result.done.wait(timeout):
            if not result.done.is_set():
                result.error = "login timed out"
                result.done.set()
        # shut down regardless once the result is settled or timeout hit
        try:
            srv.shutdown()
        except Exception:
            pass

    t = threading.Thread(target=_timeout_watchdog, daemon=True)
    t.start()
    return actual_port, srv, result


# --- token exchange / refresh (xai.go:143-271) ------------------------------
def _post_token_form(token_endpoint, fields, timeout=30):
    """POST urlencoded form to the token endpoint; return (status, parsed dict)."""
    _validate_endpoint(token_endpoint)
    p = urllib.parse.urlparse(token_endpoint)
    body = urllib.parse.urlencode(fields).encode("ascii")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    if p.scheme == "https":
        conn = http.client.HTTPSConnection(p.hostname, port=p.port or 443, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(p.hostname, port=p.port or 80, timeout=timeout)
    try:
        conn.request("POST", p.path or "/", body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        status = resp.status
    finally:
        conn.close()
    return status, _parse_token_json(raw, status)


def _parse_token_json(raw, status):
    try:
        doc = json.loads(raw)
    except ValueError:
        # some providers return urlencoded error bodies
        doc = dict(urllib.parse.parse_qsl(raw))
    if status != 200:
        err = doc.get("error") if isinstance(doc, dict) else None
        desc = doc.get("error_description") if isinstance(doc, dict) else None
        msg = "token endpoint %d: %s" % (status, err or raw[:200])
        if desc:
            msg += " (%s)" % desc
        if status in (400, 401, 403):
            raise ReloginError(msg)
        raise OAuthError(msg)
    if not isinstance(doc, dict):
        raise OAuthError("token endpoint returned non-object")
    return doc


def exchange_code(token_endpoint, code, redirect_uri, verifier, timeout=30):
    """Exchange the auth code for tokens. Returns dict with access/refresh/id tokens."""
    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
    }
    status, doc = _post_token_form(token_endpoint, fields, timeout)
    access = doc.get("access_token")
    if not access:
        raise OAuthError("token response missing access_token")
    return doc


def refresh(token_endpoint, refresh_token, timeout=30):
    """Refresh the access token. Raises ReloginError on 400/401/403 or missing token."""
    if not refresh_token:
        raise ReloginError("no refresh_token; re-login required")
    fields = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
    }
    # note: no scope sent (xai.go:213-217; pi-grok oauth.ts:590-594)
    status, doc = _post_token_form(token_endpoint, fields, timeout)
    if not doc.get("access_token"):
        raise OAuthError("refresh response missing access_token")
    return doc


# --- id_token validation (pi-grok oauth.ts:299-376) --------------------------
def _b64url_decode(seg):
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def parse_jwt_identity(id_token):
    """Decode the id_token JWT payload; return dict of claims (email, sub, ...).

    Returns {} for an absent/malformed token (callers that merely want the
    email/sub should not fail). validate_id_token() enforces strictness.
    """
    if not id_token:
        return {}
    parts = id_token.split(".")
    if len(parts) != 3:
        return {}
    try:
        return json.loads(_b64url_decode(parts[1]).decode("utf-8", "replace"))
    except Exception:
        return {}


def validate_id_token(id_token, expected_nonce=None):
    """Validate iss/aud/nonce/exp of the id_token. No signature verification.

    Mirrors pi-grok oauth.ts:327-376 but fails closed: a present-but-malformed
    token, or one missing iss/aud, is rejected rather than silently accepted.
    No-op only if the token is absent.
    """
    if not id_token:
        return
    claims = parse_jwt_identity(id_token)
    if not claims:
        raise OAuthError("id_token malformed or undecodable")

    # iss: REQUIRED, https URL whose host is x.ai / *.x.ai (oauth.ts:336-349)
    iss = claims.get("iss")
    if not iss:
        raise OAuthError("id_token missing iss")
    p = urllib.parse.urlparse(iss)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not (host in ALLOWED_HOSTS or host.endswith(".x.ai")):
        raise OAuthError("id_token iss not allowed: %s" % iss)

    # aud: REQUIRED; array must contain CLIENT_ID; string must equal it (oauth.ts:351-358)
    aud = claims.get("aud")
    if aud is None:
        raise OAuthError("id_token missing aud")
    if isinstance(aud, list):
        if CLIENT_ID not in aud:
            raise OAuthError("id_token aud does not contain client_id")
    elif isinstance(aud, str):
        if aud != CLIENT_ID:
            raise OAuthError("id_token aud mismatch")

    # nonce: checked only if present in the token (oauth.ts:360-367)
    nonce = claims.get("nonce")
    if nonce is not None and expected_nonce is not None and nonce != expected_nonce:
        raise OAuthError("id_token nonce mismatch")

    # exp: checked only if present; 30s clock skew (oauth.ts:369-375)
    exp = claims.get("exp")
    if exp is not None:
        try:
            exp_i = int(exp)
        except (TypeError, ValueError):
            raise OAuthError("id_token exp not an integer")
        if exp_i < time.time() - REFRESH_SKEW_S:
            raise OAuthError("id_token expired")


# --- high-level login flow ---------------------------------------------------
def login(open_browser=True, port=CALLBACK_PORT, manual_paste_fallback=True):
    """Run the full interactive login. Returns the raw token-response dict
    (access_token, refresh_token, id_token, expires_in, token_type)."""
    auth_ep, token_ep = discover()
    verifier, _ = generate_pkce()
    state = _random_state()
    nonce = _random_state()

    actual_port, srv, result = start_callback_server(port=port, expected_state=state)
    redirect_uri = "http://%s:%d%s" % (CALLBACK_HOST, actual_port, REDIRECT_PATH)
    auth_url = build_authorize_url(auth_ep, redirect_uri, verifier, state, nonce)

    print("[grok_proxy] Open this URL to log in (if a browser doesn't open):")
    print("  " + auth_url)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(auth_url)
        except Exception:
            pass

    # serve in background until the callback (or timeout) fires
    srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    srv_thread.start()

    # manual paste fallback for headless/SSH (pi-grok oauth.ts:470-532)
    if manual_paste_fallback:
        def _prompt():
            # only offer after a short delay so it doesn't race the browser
            time.sleep(15)
            if result.done.is_set():
                return
            try:
                print("[grok_proxy] No callback received yet. If your browser can't "
                      "reach localhost, paste the redirected URL or the bare code "
                      "here and press Enter (or leave blank to keep waiting):")
                line = input("code/url> ").strip()
                if not line:
                    return
                _apply_manual_paste(result, line, state)
            except Exception:
                pass
        threading.Thread(target=_prompt, daemon=True).start()

    result.done.wait()
    try:
        srv.shutdown()
    except Exception:
        pass

    if result.error:
        raise OAuthError("login failed: %s" % result.error)
    if not result.code:
        raise OAuthError("login failed: no code returned")

    doc = exchange_code(token_ep, result.code, redirect_uri, verifier)
    validate_id_token(doc.get("id_token"), expected_nonce=nonce)
    doc["_token_endpoint"] = token_ep
    doc["_redirect_uri"] = redirect_uri
    return doc


def _apply_manual_paste(result, pasted, expected_state):
    """Accept a full redirect URL, a `code=...&state=...` string, or a bare code.

    Mirrors pi-grok parseRedirectUrl (oauth.ts:77-97). State checked only if the
    pasted value carried one (oauth.ts:508-515).
    """
    code = None
    state = None
    if "://" in pasted:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [""])[0]
    elif "code=" in pasted or "state=" in pasted:
        q = urllib.parse.parse_qs(pasted)
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [""])[0]
    else:
        code = pasted.strip()  # bare code; trust PKCE, skip state (Go sdk/auth/xai.go:230-239)
    if not code:
        return
    if state and expected_state and state != expected_state:
        result.error = "manual paste state mismatch"
    else:
        result.code = code
        result.state = state
    result.done.set()
