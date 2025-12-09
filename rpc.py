"""JSON-RPC client for bridge communication."""

import subprocess
import threading
import json
from typing import Dict, Any, Optional, Callable


class JsonRpcClient:
    def __init__(self, on_notification: Callable[[str, dict], None]):
        self.proc: Optional[subprocess.Popen] = None
        self.request_id = 0
        self.pending: Dict[int, Callable[[dict], None]] = {}
        self.on_notification = on_notification
        self.reader_thread: Optional[threading.Thread] = None
        self.running = False

    def start(self, cmd: list) -> None:
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
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

    def send(self, method: str, params: dict, callback: Optional[Callable[[dict], None]] = None) -> None:
        if not self.proc or not self.proc.stdin:
            return

        self.request_id += 1
        req = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}

        if callback:
            self.pending[self.request_id] = callback

        self.proc.stdin.write((json.dumps(req) + "\n").encode())
        self.proc.stdin.flush()

    def _read_loop(self) -> None:
        import sublime
        while self.running and self.proc and self.proc.stdout:
            try:
                line = self.proc.stdout.readline()
                if not line:
                    break
                msg = json.loads(line.decode())
                sublime.set_timeout(lambda m=msg: self._handle(m), 0)
            except:
                continue

    def _handle(self, msg: dict) -> None:
        if "id" in msg and msg["id"] in self.pending:
            cb = self.pending.pop(msg["id"])
            cb({"error": msg["error"]} if "error" in msg else msg.get("result", {}))
        elif "method" in msg:
            self.on_notification(msg["method"], msg.get("params", {}))
