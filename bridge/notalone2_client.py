"""
notalone2 client - connects to notalone daemon for notification management.

Pool connection is persistent and only receives inject callbacks.
Commands use separate synchronous connections (like hive).
"""
import asyncio
import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Callable, Optional, Dict, Any

logger = logging.getLogger(__name__)

SOCKET_PATH = str(Path.home() / ".notalone" / "notalone.sock")
POOL_PREFIX = "sublime"

InjectCallback = Callable[[str, str, Optional[Dict[str, Any]]], None]


def _sync_command(req: dict) -> dict:
    """Send command to daemon and get response (sync, new connection each time)."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(SOCKET_PATH)
        sock.sendall((json.dumps(req) + "\n").encode())

        data = b""
        while b"\n" not in data:
            chunk = sock.recv(1024)
            if not chunk:
                break
            data += chunk

        sock.close()
        return json.loads(data.decode().strip())
    except FileNotFoundError:
        return {"error": "notalone daemon not running"}
    except Exception as e:
        logger.error(f"Command failed: {e}")
        return {"error": str(e)}


class Notalone2Client:
    """
    Client for notalone2 daemon.

    Pool connection is persistent and only receives inject callbacks.
    Commands use separate synchronous connections.
    """

    def __init__(
        self,
        prefix: str,
        session_id: str,
        on_inject: InjectCallback
    ):
        """
        Args:
            prefix: Pool prefix (e.g., "sublime")
            session_id: Instance ID within the pool (e.g., view ID)
            on_inject: Callback for injected prompts: (session_id, wake_prompt, context) -> None
        """
        self.prefix = prefix
        self.session_id = session_id
        self.full_session_id = f"{prefix}.{session_id}"
        self.on_inject = on_inject

        self._running = False
        self._thread: Optional[threading.Thread] = None

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
                req = {"method": "register_pool", "prefix": self.prefix}
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
                    logger.info(f"notalone2: connected as pool '{self.prefix}'")
                    reconnect_delay = 2
                else:
                    logger.error(f"notalone2: failed to register pool: {resp}")
                    sock.close()
                    continue

                # Listen for injects
                buffer = b""
                sock.settimeout(60)
                while self._running:
                    try:
                        chunk = sock.recv(4096)
                        if not chunk:
                            logger.warning("notalone2: daemon disconnected")
                            break

                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            try:
                                msg = json.loads(line.decode())
                                if "inject" in msg:
                                    self._handle_inject(msg["inject"])
                            except json.JSONDecodeError as e:
                                logger.error(f"notalone2: invalid JSON: {e}")

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
                            logger.warning("notalone2: connection stale")
                            break

                sock.close()

            except FileNotFoundError:
                logger.debug(f"notalone2: socket not found, retrying in {reconnect_delay}s")
            except ConnectionRefusedError:
                logger.debug(f"notalone2: daemon not ready, retrying in {reconnect_delay}s")
            except Exception as e:
                logger.error(f"notalone2: listen error: {e}")
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
        """Handle an inject callback."""
        session_id = inject.get("session_id", "")
        wake_prompt = inject.get("wake_prompt", "")
        context = inject.get("context")

        parts = session_id.split(".", 1)
        if len(parts) == 2 and parts[0] == self.prefix:
            agent_id = parts[1]
            # Only handle injects for our session
            if agent_id == self.session_id:
                logger.info(f"notalone2: inject for {session_id}: {wake_prompt[:50]}...")
                try:
                    self.on_inject(session_id, wake_prompt, context)
                except Exception as e:
                    logger.error(f"notalone2: inject callback failed: {e}")
        else:
            logger.debug(f"notalone2: ignoring inject for {session_id}")

    async def connect(self) -> bool:
        """Start the client (non-blocking, uses background thread)."""
        if not os.path.exists(SOCKET_PATH):
            logger.warning(f"notalone2: socket not found at {SOCKET_PATH}")
            return False

        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        return True

    async def disconnect(self) -> None:
        """Stop the client."""
        self._running = False
        logger.info("notalone2: disconnected")

    async def register(
        self,
        notification_type: str,
        params: Dict[str, Any],
        wake_prompt: str
    ) -> Optional[str]:
        """Register a notification. Returns notification_id."""
        resp = _sync_command({
            "method": "register",
            "session_id": self.full_session_id,
            "type": notification_type,
            "params": params,
            "wake_prompt": wake_prompt
        })
        nid = resp.get("notification_id")
        if nid:
            logger.debug(f"notalone2: registered {notification_type} -> {nid}")
        else:
            logger.error(f"notalone2: register failed: {resp.get('error', 'unknown')}")
        return nid

    async def unregister(self, notification_id: str) -> bool:
        """Unregister a notification by ID."""
        resp = _sync_command({
            "method": "unregister",
            "notification_id": notification_id
        })
        return resp.get("ok", False)

    async def list_notifications(self) -> list:
        """List active notifications for this session."""
        resp = _sync_command({
            "method": "list",
            "session_id": self.full_session_id
        })
        return resp.get("notifications", [])

    async def signal_complete(self, subsession_id: str) -> bool:
        """Signal that a subsession has completed."""
        resp = _sync_command({
            "method": "signal_complete",
            "subsession_id": subsession_id
        })
        return resp.get("ok", False)

    async def discover_services(self) -> dict:
        """Discover available notification services from daemon."""
        return _sync_command({"method": "services"})


# Convenience functions
async def set_timer(
    client: Notalone2Client,
    seconds: int,
    wake_prompt: str
) -> Optional[str]:
    """Register a timer notification."""
    return await client.register("timer", {"seconds": seconds}, wake_prompt)


async def wait_for_subsession(
    client: Notalone2Client,
    subsession_id: str,
    wake_prompt: str
) -> Optional[str]:
    """Register to wait for a subsession to complete."""
    return await client.register(
        "subsession",
        {"subsession_id": subsession_id},
        wake_prompt
    )


# ============================================================================
# Chatroom functions
# ============================================================================

def chatroom_list() -> dict:
    """List all chatrooms."""
    return _sync_command({"method": "chatroom_list"})


def chatroom_rooms_for_session(session_id: str) -> dict:
    """List rooms a session has joined."""
    return _sync_command({
        "method": "chatroom_rooms_for_session",
        "session_id": session_id
    })


def chatroom_create(room_id: str = None, name: str = None, max_chars: int = 1000, prompt_hint: int = 500) -> dict:
    """Create a new chatroom."""
    req = {"method": "chatroom_create", "max_chars": max_chars, "prompt_hint": prompt_hint}
    if room_id:
        req["room_id"] = room_id
    if name:
        req["name"] = name
    return _sync_command(req)


def chatroom_join(session_id: str, room_id: str, role: str = "agent") -> dict:
    """Join a chatroom."""
    return _sync_command({
        "method": "chatroom_join",
        "room_id": room_id,
        "session_id": session_id,
        "role": role
    })


def chatroom_leave(session_id: str, room_id: str) -> dict:
    """Leave a chatroom."""
    return _sync_command({
        "method": "chatroom_leave",
        "room_id": room_id,
        "session_id": session_id
    })


def chatroom_post(session_id: str, room_id: str, content: str) -> dict:
    """Post a message to a chatroom."""
    return _sync_command({
        "method": "chatroom_post",
        "room_id": room_id,
        "session_id": session_id,
        "content": content
    })


def chatroom_history(room_id: str, limit: int = 50, before_id: int = 0) -> dict:
    """Get chat history."""
    req = {"method": "chatroom_history", "room_id": room_id, "limit": limit}
    if before_id > 0:
        req["before_id"] = before_id
    return _sync_command(req)
