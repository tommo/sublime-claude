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


# Batching config
MAX_BATCH_SIZE = 5  # Flush after this many notifications
MAX_BATCH_AGE_SECS = 30  # Flush oldest batch after this many seconds


class NotaloneClient:
    """Global client that receives all injects for sublime.* sessions."""

    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Pending notifications per session: view_id -> list of (wake_prompt, context, timestamp)
        self._pending: Dict[int, list] = {}
        self._pending_lock = threading.Lock()
        self._flush_timer: Optional[int] = None  # sublime.set_timeout handle

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
                                elif "channel" in msg:
                                    self._handle_channel(msg["channel"])
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
        import time

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

        # Queue the notification with timestamp
        with self._pending_lock:
            if view_id not in self._pending:
                self._pending[view_id] = []
            self._pending[view_id].append((wake_prompt, context, time.time()))

            # Check if batch size reached - force immediate flush
            if len(self._pending[view_id]) >= MAX_BATCH_SIZE:
                print(f"[Claude] notalone: batch size {MAX_BATCH_SIZE} reached for view {view_id}, flushing")
                sublime.set_timeout(lambda vid=view_id: self._flush_batch(vid), 0)
                return

        # Schedule processing on main thread
        sublime.set_timeout(lambda: self._process_pending(view_id), 0)

        # Start periodic flush timer if not running
        self._ensure_flush_timer()

    def _handle_channel(self, channel_msg: dict):
        """Handle a channel message - requires sync response."""
        channel_id = channel_msg.get("channel_id", "")
        session_id = channel_msg.get("session_id", "")
        data = channel_msg.get("data", {})
        interrupt = channel_msg.get("interrupt", True)  # Default: interrupt mode

        logger.info(f"notalone: channel message for {session_id} (interrupt={interrupt})")

        # Parse session_id: "sublime.{view_id}"
        parts = session_id.split(".", 1)
        if len(parts) != 2 or parts[0] != POOL_PREFIX:
            self._send_channel_response(channel_id, {"error": "invalid session"})
            return

        try:
            view_id = int(parts[1])
        except ValueError:
            self._send_channel_response(channel_id, {"error": "invalid view_id"})
            return

        # Queue on main thread with response callback
        sublime.set_timeout(
            lambda: self._process_channel(view_id, channel_id, data, interrupt), 0
        )

    def _process_channel(self, view_id: int, channel_id: str, data: dict, interrupt: bool = True, retries: int = 0):
        """Process channel message on main thread.

        Args:
            view_id: Target session's view ID
            channel_id: Channel identifier for response
            data: Message data
            interrupt: If True, interrupt current generation before sending
            retries: Number of retry attempts (for waiting on busy session)
        """
        if not hasattr(sublime, '_claude_sessions'):
            self._send_channel_response(channel_id, {"error": "sessions not initialized"})
            return

        session = sublime._claude_sessions.get(view_id)
        if not session:
            self._send_channel_response(channel_id, {"error": "session not found"})
            return

        # If session is busy, handle based on interrupt flag
        if session.working:
            if retries >= 25:  # 25 * 200ms = 5 seconds max wait
                self._send_channel_response(channel_id, {"error": "session busy timeout"})
                return
            if interrupt and retries == 0:
                print(f"[Claude] notalone: interrupting session {view_id} for channel message")
                session.interrupt()
            # Wait for session to become idle, then retry
            sublime.set_timeout(
                lambda: self._process_channel(view_id, channel_id, data, interrupt=False, retries=retries+1), 200
            )
            return

        # Format data as user message
        # Include hint for terse response (channel mode expects quick action commands)
        screen = data.get("screen", "")
        state = data.get("state", {})
        msg = data.get("msg", "")

        if screen:
            user_msg = f"[CHANNEL - respond with action only, no explanation]\n\nGame Screen:\n```\n{screen}\n```"
            if state:
                user_msg += f"\n\nGame State: {json.dumps(state)}"
        elif msg:
            # Simple message
            user_msg = f"[CHANNEL - respond briefly]\n\n{msg}"
        else:
            # Fallback to raw data
            user_msg = f"[CHANNEL - respond briefly]\n\n{json.dumps(data)}"

        logger.info(f"notalone: sending channel message to session {view_id}")

        # Send to session and wait for response
        def on_response(response_text: str):
            print(f"[Claude] notalone: channel callback received: {len(response_text)} chars")
            self._send_channel_response(channel_id, response_text)

        print(f"[Claude] notalone: calling send_message_with_callback")
        session.send_message_with_callback(user_msg, on_response, silent=False)

    def _send_channel_response(self, channel_id: str, response):
        """Send response back to daemon via new connection."""
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(SOCKET_PATH)

            req = {
                "method": "channel_respond",
                "channel_id": channel_id,
                "response": response
            }
            sock.sendall((json.dumps(req) + "\n").encode())

            # Read ack
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                data += chunk

            sock.close()
            logger.info(f"notalone: sent channel response for {channel_id}")
        except Exception as e:
            logger.error(f"notalone: failed to send channel response: {e}")

    def _ensure_flush_timer(self):
        """Ensure periodic flush timer is running."""
        if self._flush_timer is not None:
            return
        # Check every 5 seconds for aged batches
        self._flush_timer = sublime.set_timeout(self._periodic_flush, 5000)

    def _periodic_flush(self):
        """Periodically check for aged batches and flush them."""
        import time
        self._flush_timer = None

        now = time.time()
        view_ids_to_flush = []

        with self._pending_lock:
            for view_id, pending in self._pending.items():
                if pending:
                    oldest_ts = pending[0][2]  # timestamp of oldest notification
                    if now - oldest_ts >= MAX_BATCH_AGE_SECS:
                        view_ids_to_flush.append(view_id)

            # Check if there are still pending notifications
            has_pending = any(self._pending.values())

        # Flush aged batches
        for view_id in view_ids_to_flush:
            print(f"[Claude] notalone: batch aged {MAX_BATCH_AGE_SECS}s for view {view_id}, flushing")
            self._flush_batch(view_id)

        # Reschedule if there are still pending notifications
        if has_pending:
            self._flush_timer = sublime.set_timeout(self._periodic_flush, 5000)

    def _flush_batch(self, view_id: int):
        """Force flush pending notifications for a session (inject even if busy)."""
        if not hasattr(sublime, '_claude_sessions'):
            logger.error("notalone: _claude_sessions not initialized")
            return

        session = sublime._claude_sessions.get(view_id)
        if not session:
            logger.error(f"notalone: session not found for view {view_id}")
            with self._pending_lock:
                self._pending.pop(view_id, None)
            return

        # Collect pending notifications
        with self._pending_lock:
            pending = self._pending.pop(view_id, [])

        if not pending:
            return

        # Build wake prompt
        if len(pending) == 1:
            wake_prompt = pending[0][0]
            display_message = wake_prompt.split("\n")[0] if wake_prompt else "Notification"
        else:
            prompts = [p[0] for p in pending]
            wake_prompt = f"📬 {len(pending)} notifications:\n\n" + "\n\n---\n\n".join(prompts)
            display_message = f"📬 {len(pending)} notifications"
            print(f"[Claude] notalone: batching {len(pending)} notifications for view {view_id}")

        print(f"[Claude] notalone: flushing to session {view_id} (working={session.working}): {display_message}")

        try:
            if session.working:
                # Inject into running query
                session.queue_prompt(wake_prompt)
            else:
                # Start new query
                session.query(wake_prompt, display_prompt=display_message)
        except Exception as e:
            logger.error(f"notalone: flush failed: {e}")
            print(f"[Claude] notalone: flush failed: {e}")

    def _process_pending(self, view_id: int):
        """Process pending notifications for a session (runs on main thread).

        Only processes if session is idle. If busy, relies on _flush_batch
        which is triggered by batch size or time window.
        """
        if not hasattr(sublime, '_claude_sessions'):
            logger.error("notalone: _claude_sessions not initialized")
            return

        session = sublime._claude_sessions.get(view_id)
        if not session:
            logger.error(f"notalone: session not found for view {view_id}")
            with self._pending_lock:
                self._pending.pop(view_id, None)
            return

        # If session is busy, let _flush_batch handle it (via timer or batch size)
        if session.working:
            return

        # Session is idle - flush immediately
        self._flush_batch(view_id)


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
