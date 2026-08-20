"""Local HTTP MCP in front of a stdio MCP process.

Kimi ACP 0.37.2 session/new throws on stdio MCP relay. Docs advertise
mcpCapabilities.http — sandbox: type=http session/new succeeds.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional


class _StdioClient:
    def __init__(self, command: str, args: list, env: Optional[dict] = None):
        e = os.environ.copy()
        if env:
            e.update({str(k): str(v) for k, v in env.items()})
        self.proc = subprocess.Popen(
            [command, *(args or [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=e,
            bufsize=0,
        )
        self._lock = threading.Lock()

    def rpc(self, msg: dict) -> Optional[dict]:
        if self.proc.poll() is not None or not self.proc.stdin or not self.proc.stdout:
            raise RuntimeError("stdio mcp dead")
        line = (json.dumps(msg) + "\n").encode()
        with self._lock:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
            if "id" not in msg:
                return None
            rid = msg["id"]
            while True:
                raw = self.proc.stdout.readline()
                if not raw:
                    raise RuntimeError("stdio mcp eof")
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if obj.get("id") == rid:
                    return obj

    def close(self) -> None:
        try:
            self.proc.kill()
        except Exception:
            pass


def start_stdio_http_mcp(command: str, args: list, env: Optional[dict] = None):
    """Listen on 127.0.0.1:0; return (url, server). Caller must keep server."""
    child = _StdioClient(command, args, env)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b"{}"
            try:
                msg = json.loads(body.decode() or "{}")
            except Exception:
                self.send_error(400)
                return
            if not isinstance(msg, dict):
                self.send_error(400)
                return
            try:
                out = child.rpc(msg)
            except Exception as e:
                if "id" not in msg:
                    self.send_response(202)
                    self.end_headers()
                    return
                payload = json.dumps({
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "error": {"code": -32603, "message": str(e)},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if out is None:
                self.send_response(202)
                self.end_headers()
                return
            payload = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.send_response(200)
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.child = child  # type: ignore[attr-defined]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address[:2]
    url = f"http://{host}:{port}/mcp"
    return url, httpd


def acp_stdio_env(server: dict) -> Dict[str, str]:
    env: Dict[str, str] = {}
    raw = server.get("env")
    if isinstance(raw, list):
        for p in raw:
            if isinstance(p, dict) and p.get("name"):
                env[str(p["name"])] = str(p.get("value") or "")
    elif isinstance(raw, dict):
        env = {str(k): str(v) for k, v in raw.items()}
    return env
