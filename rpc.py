"""JSON-RPC client for bridge communication."""

import subprocess
import threading
import json
import os
from typing import Dict, Any, Optional, Callable


class JsonRpcClient:
    def __init__(self, on_notification: Callable[[str, dict], None]):
        self.proc: Optional[subprocess.Popen] = None
        self.request_id = 0
        self.pending: Dict[int, Callable[[dict], None]] = {}
        # send_wait: id → (holder_dict, Event) completed on the reader thread
        # so a blocked UI thread cannot deadlock waiting for set_timeout.
        self._sync_waits: Dict[int, tuple] = {}
        self._send_lock = threading.Lock()
        self.on_notification = on_notification
        self.reader_thread: Optional[threading.Thread] = None
        self.running = False
        self.stderr_thread: Optional[threading.Thread] = None

    def start(self, cmd: list, env: Dict[str, str] = None) -> None:
        # Merge custom env with current environment
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=proc_env,
        )
        self.running = True
        self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.reader_thread.start()
        # Start stderr reader
        self.stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self.stderr_thread.start()

    def _stderr_loop(self) -> None:
        """Read stderr and print to console.

        Bridges self-prefix their log lines (e.g. "[codex-bridge] ..."), so we
        forward verbatim. We only add a generic "[bridge]" wrapper when the line
        has no bracket prefix at all.
        """
        while self.running and self.proc and self.proc.stderr:
            try:
                line = self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if not text:
                    continue
                if text.startswith("["):
                    print(text)
                else:
                    print(f"[bridge] {text}")
            except Exception as e:
                print(f"[Claude] stderr_loop error: {e}")
                continue

    def stop(self) -> None:
        self.running = False
        self.pending.clear()
        # Unblock any send_wait callers
        for rid, (holder, ev) in list(self._sync_waits.items()):
            holder["error"] = {"message": "Bridge stopped"}
            ev.set()
        self._sync_waits.clear()
        if self.proc:
            self.proc.terminate()
            self.proc.wait()
            self.proc = None

    def is_alive(self) -> bool:
        """Check if bridge process is still running."""
        return self.proc is not None and self.proc.poll() is None

    def send(self, method: str, params: dict, callback: Optional[Callable[[dict], None]] = None) -> bool:
        """Send request to bridge. Returns False if bridge is dead.

        Response callbacks run on the Sublime main thread (via set_timeout).
        """
        if not self.proc or not self.proc.stdin:
            return False
        if self.proc.poll() is not None:
            print(f"[Claude] Bridge process died with code {self.proc.returncode}")
            return False

        with self._send_lock:
            self.request_id += 1
            rid = self.request_id
            req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            if callback:
                self.pending[rid] = callback
            try:
                self.proc.stdin.write((json.dumps(req) + "\n").encode())
                self.proc.stdin.flush()
            except Exception as e:
                self.pending.pop(rid, None)
                print(f"[Claude] Bridge send failed: {e}")
                return False
        return True

    def send_wait(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        """Send request and wait for response. Returns {"result": ...} or {"error": ...}.

        Completes on the reader thread (not set_timeout), so this must NOT be
        used from the UI thread for long ops — it freezes the editor until the
        bridge replies. Prefer async send() + callback for UI actions.
        """
        if not self.proc or not self.proc.stdin:
            return {"error": {"message": "Failed to send request - bridge is dead"}}
        if self.proc.poll() is not None:
            return {"error": {"message": f"Bridge process died with code {self.proc.returncode}"}}

        holder: dict = {}
        done = threading.Event()
        with self._send_lock:
            self.request_id += 1
            rid = self.request_id
            self._sync_waits[rid] = (holder, done)
            req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            try:
                self.proc.stdin.write((json.dumps(req) + "\n").encode())
                self.proc.stdin.flush()
            except Exception as e:
                self._sync_waits.pop(rid, None)
                return {"error": {"message": f"Bridge send failed: {e}"}}

        if not done.wait(timeout):
            self._sync_waits.pop(rid, None)
            return {"error": {"message": f"Request timed out after {timeout}s"}}

        if "error" in holder:
            return {"error": holder["error"]}
        return {"result": holder.get("result", {})}

    def _read_loop(self) -> None:
        import sublime
        while self.running and self.proc and self.proc.stdout:
            try:
                line = self.proc.stdout.readline()
                if not line:
                    self._handle_stdout_closed()
                    break

                text = line.decode(errors="replace")
                if not text.strip():
                    continue

                try:
                    msg = json.loads(text)
                except json.JSONDecodeError as e:
                    snippet = text.strip().replace("\n", "\\n")[:200]
                    print(f"[Claude RPC] read_loop: invalid JSON line: {e}: {snippet!r}")
                    continue

                mid = msg.get("id")
                # send_wait completions: always on reader thread
                if mid is not None and mid in self._sync_waits:
                    holder, ev = self._sync_waits.pop(mid)
                    if "error" in msg:
                        holder["error"] = msg["error"]
                    else:
                        holder["result"] = msg.get("result", {})
                    ev.set()
                    continue

                # Normal responses + notifications on the main thread
                sublime.set_timeout(lambda m=msg: self._handle(m), 0)
            except Exception as e:
                print(f"[Claude RPC] read_loop error: {e}")
                continue

    def _handle_stdout_closed(self) -> None:
        """Bridge stdout reached EOF; fail pending RPC requests promptly."""
        import sublime

        proc = self.proc
        returncode = proc.poll() if proc else None
        if returncode is None:
            detail = "Bridge stdout closed while process is still running"
        else:
            detail = f"Bridge process exited with code {returncode}"

        print(f"[Claude RPC] read_loop: {detail}")
        self.running = False

        for rid, (holder, ev) in list(self._sync_waits.items()):
            holder["error"] = {"message": detail}
            ev.set()
        self._sync_waits.clear()

        pending = list(self.pending.items())
        self.pending.clear()
        if not pending:
            return

        response = {"error": {"message": detail}}
        for _, callback in pending:
            sublime.set_timeout(lambda cb=callback, r=response: cb(r), 0)

    def _handle(self, msg: dict) -> None:
        if "id" in msg and msg["id"] in self.pending:
            cb = self.pending.pop(msg["id"])
            # Historical shape: bare result dict, or {"error": ...}
            cb({"error": msg["error"]} if "error" in msg else msg.get("result", {}))
        elif "method" in msg:
            self.on_notification(msg["method"], msg.get("params", {}))
