"""Anthropic-compatible HTTP server for the grok_proxy.

Exposes POST /v1/messages (stream + non-stream), POST /v1/messages/count_tokens,
GET /v1/models. Translates to xAI Responses API and authenticates via the
SuperGrok OAuth credential (grok_proxy.tokens).

Run as a module:
    python -m grok_proxy --port 8787 --auth-token <token>
    python -m grok_proxy --login          # interactive xAI OAuth
"""
import argparse
import http.client
import json
import os
import signal
import ssl
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import oauth, tokens
from . import replay
from . import translate_request as tr
from . import translate_response as rr

DEFAULT_PORT = 8787
DEFAULT_DATA_DIR = os.path.expanduser("~/.claude/grok_proxy")

# Embedded minimal Grok catalog for GET /v1/models (Anthropic shape).
GROK_MODELS = [
    ("grok-4.5", "Grok 4.5"),
    ("grok-4-fast", "Grok 4 Fast"),
    ("grok-4.3", "Grok 4.3"),
    ("grok-4", "Grok 4"),
]


def _anthropic_error(status, err_type, message):
    body = json.dumps({"type": "error",
                       "error": {"type": err_type, "message": message}}).encode("utf-8")
    return status, body


class _State:
    """Shared per-process state handed to the request handler."""

    def __init__(self, auth_token, credential, base_url):
        self.auth_token = auth_token
        self.credential = credential  # tokens.Credential or None
        self.base_url = base_url      # e.g. https://api.x.ai/v1

    def authorized(self, handler):
        if not self.auth_token:
            return True
        auth = handler.headers.get("Authorization", "")
        token = ""
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):].strip()
        elif auth.startswith("bearer "):
            token = auth[len("bearer "):].strip()
        if not token:
            xkey = handler.headers.get("x-api-key", "")
            token = xkey.strip()
        return token == self.auth_token


class Handler(BaseHTTPRequestHandler):
    # set by make_server
    state = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[grok_proxy] " + (fmt % args) + "\n")

    # --- helpers ----------------------------------------------------------
    def _send(self, status, body_bytes, content_type="application/json", extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if body_bytes:
            self.wfile.write(body_bytes)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _require_auth(self):
        if not self.state.authorized(self):
            status, body = _anthropic_error(401, "authentication_error",
                                            "invalid or missing auth token")
            self._send(status, body)
            return False
        return True

    # --- routing ----------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/v1/models":
            if not self._require_auth():
                return
            self._models()
        elif parsed.path == "/healthz":
            self._send(200, b'{"status":"ok"}')
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/v1/messages":
            if not self._require_auth():
                return
            self._messages()
        elif parsed.path == "/v1/messages/count_tokens":
            if not self._require_auth():
                return
            self._count_tokens()
        else:
            self._send(404, b'{"error":"not found"}')

    # --- endpoints --------------------------------------------------------
    def _models(self):
        is_anthropic = bool(self.headers.get("Anthropic-Version")) or \
            "claude" in (self.headers.get("User-Agent", "").lower())
        if is_anthropic:
            data = {"data": [{"type": "model", "id": mid, "display_name": label,
                              "created_at": "2025-01-01T00:00:00Z"}
                             for mid, label in GROK_MODELS]}
        else:
            data = {"object": "list",
                    "data": [{"id": mid, "object": "model", "owned_by": "xai"}
                             for mid, label in GROK_MODELS]}
        self._send(200, json.dumps(data).encode("utf-8"))

    def _count_tokens(self):
        body = self._read_body()
        try:
            req = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            status, b = _anthropic_error(400, "invalid_request_error", "invalid JSON")
            self._send(status, b)
            return
        model = req.get("model", "grok-4")
        xai_body, _ = tr.translate_request(model, req)
        # heuristic estimate (no cl100k in stdlib): ~4 chars/token over the
        # serialized translated body. Advisory only for Claude Code budgeting.
        approx = max(1, len(json.dumps(xai_body)) // 4)
        self._send(200, json.dumps({"input_tokens": approx}).encode("utf-8"))

    def _messages(self):
        body = self._read_body()
        try:
            req = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            status, b = _anthropic_error(400, "invalid_request_error", "invalid JSON")
            self._send(status, b)
            return

        model = req.get("model", "grok-4")
        want_stream = bool(req.get("stream", False))
        xai_body, short_to_original = tr.translate_request(model, req)

        # Reasoning replay: re-inject the session's last reasoning items (full
        # items with id/status/encrypted_content) so Grok continues its chain of
        # thought instead of re-reasoning each turn. Disabled when no session key.
        session_key = replay.session_key_from_request(req, self.headers)
        if session_key:
            replay.inject_reasoning_replay(xai_body, replay.cache.get(session_key))

        # resolve an access token (auto-refresh if needed)
        try:
            access_token = self._access_token()
        except oauth.ReloginError as e:
            status, b = _anthropic_error(401, "authentication_error",
                                         "xAI session expired — re-login required: %s" % e)
            self._send(status, b)
            return
        except oauth.OAuthError as e:
            status, b = _anthropic_error(500, "api_error", "xAI auth error: %s" % e)
            self._send(status, b)
            return

        try:
            upstream = self._post_upstream(xai_body, access_token, stream=True)
        except (http.client.HTTPException, OSError, TimeoutError) as e:
            status, b = _anthropic_error(502, "api_error",
                                         "could not reach xAI upstream: %s" % e)
            self._send(status, b)
            return
        if upstream.status != 200:
            err_body = upstream.read().decode("utf-8", "replace")
            status, b = _anthropic_error(self._map_status(upstream.status),
                                         "api_error", "upstream xAI error: %s" % err_body[:500])
            try:
                upstream._conn.close()
            except Exception:
                pass
            self._send(status, b)
            return

        if want_stream:
            self._relay_stream(upstream, short_to_original, session_key)
        else:
            self._relay_nonstream(upstream, short_to_original, session_key)

    def _cache_completed(self, session_key, completed):
        """Store the response's reasoning items for the next turn's replay."""
        if session_key and completed:
            replay.cache.set(session_key, replay.extract_reasoning_items(completed))

    # --- upstream ---------------------------------------------------------
    def _access_token(self):
        if self.state.credential is None:
            raise oauth.OAuthError("no credential configured (run --login)")
        return self.state.credential.get_access_token()

    def _post_upstream(self, body, access_token, stream):
        # always send stream:true to xAI; for non-stream Anthropic requests we
        # consume the whole SSE and synthesize one JSON response (matches Go).
        body["stream"] = True
        parsed = urllib.parse.urlparse(self.state.base_url)
        host = parsed.hostname
        path = (parsed.path.rstrip("/") or "") + "/responses"
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + access_token,
            "Accept": "text/event-stream",
            "Connection": "keep-alive",
        }
        # x-grok-conv-id / prompt_cache_key only for composer models (optional);
        # set body fields BEFORE serialization so they reach the upstream.
        if isinstance(body.get("model"), str) and "composer" in body["model"]:
            conv = body.get("prompt_cache_key") or "grok-session"
            headers["x-grok-conv-id"] = conv
            body.setdefault("prompt_cache_key", conv)
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Length"] = str(len(payload))

        if parsed.scheme == "https":
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(host, port=parsed.port or 443,
                                               timeout=30, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port=parsed.port or 80, timeout=30)
        conn.request("POST", path, body=payload, headers=headers)
        resp = conn.getresponse()
        resp._conn = conn  # keep ref so we can close it
        return resp

    @staticmethod
    def _map_status(s):
        return {400: 400, 401: 401, 403: 403, 404: 404, 429: 429}.get(s, 502)

    def _iter_data_lines(self, resp):
        """Yield raw data payloads (str) from an upstream SSE stream.

        Iterating the HTTPResponse de-chunks transparently if the upstream uses
        Transfer-Encoding: chunked, so this works for both chunked and
        content-length SSE bodies.
        """
        for raw in resp:
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", "replace")
            else:
                line = raw
            line = line.rstrip("\r\n")
            if line.startswith("data:"):
                yield line[len("data:"):].lstrip()

    def _relay_stream(self, resp, short_to_original, session_key=""):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        pipeline = rr.StreamingPipeline(short_to_original)
        try:
            for data_str in self._iter_data_lines(resp):
                if data_str == "[DONE]":
                    continue
                frames = pipeline.feed_data_line(data_str)
                if frames:
                    self._write_chunk(frames)
            # Cache the completed response's reasoning items for next-turn replay.
            self._cache_completed(session_key, pipeline.last_completed)
        finally:
            try:
                self._write_chunk(b"")  # final zero-length chunk
            except Exception:
                pass
            try:
                resp._conn.close()
            except Exception:
                pass

    def _write_chunk(self, data):
        if not data:
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.wfile.write(("%X\r\n" % len(data)).encode("ascii"))
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _relay_nonstream(self, resp, short_to_original, session_key=""):
        # Collect every data line (including output_item.done) and patch a
        # terminal event whose response.output is empty — xAI frequently sends
        # the real items only on output_item.done, with output:[] at completion.
        data_lines = [d for d in self._iter_data_lines(resp) if d != "[DONE]"]
        try:
            resp._conn.close()
        except Exception:
            pass

        completed = rr.collect_nonstream_terminal(data_lines)
        if completed is None:
            status, b = _anthropic_error(504, "api_error",
                                         "stream disconnected before response.completed")
            self._send(status, b)
            return

        out = rr.non_stream_response(completed, short_to_original)
        if out is None:
            status, b = _anthropic_error(502, "api_error", "could not synthesize response")
            self._send(status, b)
            return
        self._cache_completed(session_key, completed)
        self._send(200, json.dumps(out).encode("utf-8"))


# --- server bootstrap -------------------------------------------------------

def make_server(port, auth_token, credential, base_url=oauth.DEFAULT_BASE_URL):
    state = _State(auth_token=auth_token, credential=credential, base_url=base_url)

    class BoundHandler(Handler):
        pass
    BoundHandler.state = state
    srv = ThreadingHTTPServer(("127.0.0.1", port), BoundHandler)
    srv.daemon_threads = True
    return srv


def _load_credential(data_dir, base_url):
    path = tokens.credential_file_name(data_dir)
    store = tokens.TokenStore(path)
    if not store.is_present():
        # fall back to any xai-*.json in the data dir
        for fn in sorted(os.listdir(data_dir)) if os.path.isdir(data_dir) else []:
            if fn.startswith("xai-") and fn.endswith(".json"):
                store = tokens.TokenStore(os.path.join(data_dir, fn))
                if store.is_present():
                    break
    if not store.is_present():
        return None, store
    return tokens.Credential(store), store


def main(argv=None):
    parser = argparse.ArgumentParser(prog="grok_proxy", description="Anthropic<->xAI proxy")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--auth-token", default=None,
                        help="bearer token clients must present (empty = no auth)")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--base-url", default=oauth.DEFAULT_BASE_URL)
    parser.add_argument("--login", action="store_true",
                        help="run interactive xAI OAuth login, then exit")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    os.makedirs(args.data_dir, exist_ok=True)
    try:
        os.chmod(args.data_dir, 0o700)
    except OSError:
        pass

    if args.login:
        store, _ = _login_store(args.data_dir, args.base_url)
        email = store.get().get("email")
        print("[grok_proxy] login complete. Token saved for %s." % (email or "(no email)"))
        return 0

    credential, store = _load_credential(args.data_dir, args.base_url)
    if credential is None:
        print("[grok_proxy] not logged in. Run: python -m grok_proxy --login", file=sys.stderr)
        return 1

    srv = make_server(args.port, args.auth_token, credential, args.base_url)
    stop = threading.Event()

    def _shutdown(signum=None, frame=None):
        stop.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    print("[grok_proxy] listening on http://127.0.0.1:%d (model: xAI via %s)"
          % (args.port, args.base_url), flush=True)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
    return 0


def _login_store(data_dir, base_url):
    _, store = _load_credential(data_dir, base_url)
    if not isinstance(store, tokens.TokenStore) or not store.is_present():
        # create a fresh store at the default path
        store = tokens.TokenStore(tokens.credential_file_name(data_dir))
    tokens.login_and_store(store, open_browser=True)
    return store, store


if __name__ == "__main__":
    sys.exit(main())
