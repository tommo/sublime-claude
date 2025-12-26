"""
Global notalone2 client for sublime-claude.

ONE pool connection for the entire plugin. Routes injects to correct sessions.
"""
import json
import os
import socket
import threading
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Callable

import sublime

logger = logging.getLogger(__name__)

SOCKET_PATH = str(Path.home() / ".notalone" / "notalone.sock")
POOL_PREFIX = "sublime"


class NotaloneClient:
    """Global client that receives all injects for sublime.* sessions."""

    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the pool connection in background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        logger.info("notalone: client started")

    def stop(self):
        """Stop the client."""
        self._running = False
        logger.info("notalone: client stopped")

    def _listen_loop(self):
        """Listen for inject callbacks (runs in thread)."""
        import time
        reconnect_delay = 2

        while self._running:
            sock = None
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(30)
                sock.connect(SOCKET_PATH)

                # Register as pool
                req = {"method": "register_pool", "prefix": POOL_PREFIX}
                sock.sendall((json.dumps(req) + "\n").encode())

                # Read registration response
                data = b""
                while b"\n" not in data:
                    chunk = sock.recv(1024)
                    if not chunk:
                        break
                    data += chunk

                resp = json.loads(data.decode().strip())
                if resp.get("ok"):
                    logger.info(f"notalone: connected as pool '{POOL_PREFIX}'")
                    print(f"[Claude] notalone: connected as pool '{POOL_PREFIX}'")
                    reconnect_delay = 2
                else:
                    logger.error(f"notalone: failed to register pool: {resp}")
                    sock.close()
                    continue

                # Listen for injects
                buffer = b""
                sock.settimeout(60)
                while self._running:
                    try:
                        chunk = sock.recv(4096)
                        if not chunk:
                            logger.warning("notalone: daemon disconnected")
                            break

                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            try:
                                msg = json.loads(line.decode())
                                if "inject" in msg:
                                    self._handle_inject(msg["inject"])
                            except json.JSONDecodeError as e:
                                logger.error(f"notalone: invalid JSON: {e}")

                    except socket.timeout:
                        # Check if connection still alive
                        try:
                            sock.setblocking(False)
                            sock.recv(1, socket.MSG_PEEK)
                            sock.setblocking(True)
                            sock.settimeout(60)
                        except BlockingIOError:
                            sock.setblocking(True)
                            sock.settimeout(60)
                        except Exception:
                            logger.warning("notalone: connection stale")
                            break

                sock.close()

            except FileNotFoundError:
                logger.debug(f"notalone: socket not found, retrying in {reconnect_delay}s")
            except ConnectionRefusedError:
                logger.debug(f"notalone: daemon not ready, retrying in {reconnect_delay}s")
            except Exception as e:
                logger.error(f"notalone: listen error: {e}")
            finally:
                if sock:
                    try:
                        sock.close()
                    except:
                        pass

            if self._running:
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, 30)

    def _handle_inject(self, inject: dict):
        """Handle an inject callback - route to correct session."""
        session_id = inject.get("session_id", "")
        wake_prompt = inject.get("wake_prompt", "")
        context = inject.get("context")

        # Parse session_id: "sublime.{view_id}"
        parts = session_id.split(".", 1)
        if len(parts) != 2 or parts[0] != POOL_PREFIX:
            logger.debug(f"notalone: ignoring inject for {session_id}")
            return

        view_id_str = parts[1]
        try:
            view_id = int(view_id_str)
        except ValueError:
            logger.error(f"notalone: invalid view_id in session_id: {session_id}")
            return

        logger.info(f"notalone: inject for view {view_id}: {wake_prompt[:50]}...")
        print(f"[Claude] notalone inject for view {view_id}: {wake_prompt[:50]}...")

        # Find the session and inject
        def do_inject():
            if not hasattr(sublime, '_claude_sessions'):
                logger.error("notalone: _claude_sessions not initialized")
                return

            session = sublime._claude_sessions.get(view_id)
            if not session:
                logger.error(f"notalone: session not found for view {view_id}")
                print(f"[Claude] notalone: session not found for view {view_id}, available: {list(sublime._claude_sessions.keys())}")
                return

            # Inject the wake prompt
            display_message = wake_prompt.split("\n")[0] if wake_prompt else "Notification"
            print(f"[Claude] notalone: injecting to session {view_id}: {display_message}")

            try:
                # If session is busy, queue it
                if session.working:
                    print(f"[Claude] notalone: session busy, queuing...")

                    def try_inject():
                        if not session.working:
                            session.query(wake_prompt, display_prompt=display_message)
                        else:
                            sublime.set_timeout(try_inject, 500)

                    sublime.set_timeout(try_inject, 500)
                else:
                    session.query(wake_prompt, display_prompt=display_message)
            except Exception as e:
                logger.error(f"notalone: inject failed: {e}")
                print(f"[Claude] notalone: inject failed: {e}")

        # Schedule on main thread
        sublime.set_timeout(do_inject, 0)


# Global client instance
_client: Optional[NotaloneClient] = None


def start():
    """Start the global notalone client."""
    global _client
    if _client is None:
        _client = NotaloneClient()
    _client.start()


def stop():
    """Stop the global notalone client."""
    global _client
    if _client:
        _client.stop()
        _client = None
