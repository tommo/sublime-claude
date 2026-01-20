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
        self.on_notification = on_notification
        self.reader_thread: Optional[threading.Thread] = None
        self.running = False

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
        """Read stderr and print to console."""
        while self.running and self.proc and self.proc.stderr:
            try:
                line = self.proc.stderr.readline()
                if not line:
                    break
                print(f"[Claude Bridge] {line.decode().rstrip()}")
            except:
                continue

    def stop(self) -> None:
        self.running = False
        if self.proc:
            self.proc.terminate()
            self.proc.wait()
            self.proc = None

    def is_alive(self) -> bool:
        """Check if bridge process is still running."""
        return self.proc is not None and self.proc.poll() is None

    def send(self, method: str, params: dict, callback: Optional[Callable[[dict], None]] = None) -> bool:
        """Send request to bridge. Returns False if bridge is dead."""
        if not self.proc or not self.proc.stdin:
            return False
        if self.proc.poll() is not None:
            # Process has died
            print(f"[Claude] Bridge process died with code {self.proc.returncode}")
            return False

        self.request_id += 1
        req = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}

        if callback:
            self.pending[self.request_id] = callback

        self.proc.stdin.write((json.dumps(req) + "\n").encode())
        self.proc.stdin.flush()
        return True

    def send_wait(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        """Send request and wait for response. Returns {"result": ...} or {"error": ...}."""
        import time
        result = {}
        done = threading.Event()

        def callback(response):
            result.update(response)
            done.set()

        if not self.send(method, params, callback):
            return {"error": {"message": "Failed to send request - bridge is dead"}}

        if not done.wait(timeout):
            return {"error": {"message": f"Request timed out after {timeout}s"}}

        return result

    def _read_loop(self) -> None:
        import sublime
        while self.running and self.proc and self.proc.stdout:
            try:
                line = self.proc.stdout.readline()
                if not line:
                    print("[Claude RPC] read_loop: got empty line, breaking")
                    break
                msg = json.loads(line.decode())
                # Debug: log message types
                if "method" in msg:
                    print(f"[Claude RPC] notification: {msg.get('method')}")
                elif "id" in msg:
                    print(f"[Claude RPC] response for id={msg.get('id')}")
                sublime.set_timeout(lambda m=msg: self._handle(m), 0)
            except Exception as e:
                print(f"[Claude RPC] read_loop error: {e}")
                continue

    def _handle(self, msg: dict) -> None:
        if "id" in msg and msg["id"] in self.pending:
            cb = self.pending.pop(msg["id"])
            cb({"error": msg["error"]} if "error" in msg else msg.get("result", {}))
        elif "method" in msg:
            self.on_notification(msg["method"], msg.get("params", {}))
