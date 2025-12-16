"""HTTP/JSON-RPC client for registering notifications with remote systems."""

import aiohttp
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class NotificationClient:
    """Client for registering notifications with remote notalone systems."""

    def __init__(self, auth_token: Optional[str] = None):
        """
        Initialize the RPC client.

        Args:
            auth_token: Optional authentication token for requests
        """
        self.auth_token = auth_token
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        """Start the HTTP client session."""
        self._session = aiohttp.ClientSession()
        logger.info("NotificationClient started")

    async def stop(self):
        """Stop the HTTP client session."""
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("NotificationClient stopped")

    async def register(
        self,
        remote_url: str,
        session_id: str,
        callback_endpoint: str,
        notification: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Register a notification with a remote system.

        Args:
            remote_url: URL of remote notalone registration endpoint
            session_id: Unique ID of this session
            callback_endpoint: URL where this session receives notifications
            notification: Notification details (type, params, wake_prompt)

        Returns:
            Response dict with notification_id and status

        Raises:
            aiohttp.ClientError: If registration fails
        """
        if not self._session:
            raise RuntimeError("Client not started - call start() first")

        payload = {
            "jsonrpc": "2.0",
            "method": "notalone.register",
            "params": {
                "session_id": session_id,
                "callback_endpoint": callback_endpoint,
                "notification": notification
            },
            "id": 1
        }

        if self.auth_token:
            payload["params"]["auth_token"] = self.auth_token

        logger.info(f"Registering notification with {remote_url}")
        async with self._session.post(remote_url, json=payload) as resp:
            resp.raise_for_status()
            result = await resp.json()

            if "error" in result:
                error = result["error"]
                raise Exception(f"Registration failed: {error.get('message')} (code: {error.get('code')})")

            return result.get("result", {})

    async def unregister(
        self,
        remote_url: str,
        notification_id: str
    ) -> Dict[str, Any]:
        """
        Unregister a notification from a remote system.

        Args:
            remote_url: URL of remote notalone registration endpoint
            notification_id: ID of notification to unregister

        Returns:
            Response dict with status
        """
        if not self._session:
            raise RuntimeError("Client not started - call start() first")

        payload = {
            "jsonrpc": "2.0",
            "method": "notalone.unregister",
            "params": {
                "notification_id": notification_id
            },
            "id": 2
        }

        if self.auth_token:
            payload["params"]["auth_token"] = self.auth_token

        logger.info(f"Unregistering notification {notification_id} from {remote_url}")
        async with self._session.post(remote_url, json=payload) as resp:
            resp.raise_for_status()
            result = await resp.json()

            if "error" in result:
                error = result["error"]
                raise Exception(f"Unregister failed: {error.get('message')}")

            return result.get("result", {})

    async def notify(
        self,
        callback_url: str,
        notification_id: str,
        session_id: str,
        event_type: str,
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Send a notification to a callback endpoint.

        Args:
            callback_url: URL to send notification to
            notification_id: ID of the notification
            session_id: Target session ID
            event_type: Type of event (ticket_update, channel_message, etc.)
            data: Event data

        Returns:
            Response dict with status
        """
        if not self._session:
            raise RuntimeError("Client not started - call start() first")

        payload = {
            "jsonrpc": "2.0",
            "method": "notalone.notify",
            "params": {
                "notification_id": notification_id,
                "session_id": session_id,
                "event_type": event_type,
                "data": data
            }
        }

        logger.info(f"Sending notification {notification_id} to {callback_url}")
        async with self._session.post(callback_url, json=payload) as resp:
            resp.raise_for_status()
            result = await resp.json()

            if "error" in result:
                error = result["error"]
                logger.error(f"Notification delivery failed: {error.get('message')}")
                raise Exception(f"Notify failed: {error.get('message')}")

            return result.get("result", {})
