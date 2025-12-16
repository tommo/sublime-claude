"""Integration helpers for using RPC with NotificationHub."""

import asyncio
import logging
import uuid
from typing import Optional, Dict, Any

from ..hub import NotificationHub
from ..types import NotificationType, NotificationParams, Notification
from .client import NotificationClient
from .server import NotificationServer

logger = logging.getLogger(__name__)


class RemoteNotificationHub:
    """
    Wrapper around NotificationHub that adds RPC support for remote notifications.

    This enables cross-system notifications:
    - Local notifications (timer, subsession) handled by hub/backend
    - Remote notifications (ticket, channel) registered via RPC
    """

    def __init__(
        self,
        hub: NotificationHub,
        session_id: Optional[str] = None,
        rpc_host: str = "localhost",
        rpc_port: int = 0,
        auth_token: Optional[str] = None
    ):
        """
        Initialize remote notification support.

        Args:
            hub: Local NotificationHub instance
            session_id: Unique session ID (generated if not provided)
            rpc_host: Host for RPC callback server
            rpc_port: Port for RPC callback server (0 = auto-assign)
            auth_token: Optional auth token
        """
        self.hub = hub
        self.session_id = session_id or str(uuid.uuid4())

        # RPC components
        self.server = NotificationServer(rpc_host, rpc_port, auth_token)
        self.client = NotificationClient(auth_token)

        # Track remote registrations: notification_id -> remote_url
        self._remote_registrations: Dict[str, str] = {}

    async def start(self):
        """Start hub and RPC server/client."""
        await self.hub.start()
        await self.server.start()
        await self.client.start()

        # Register RPC handlers
        self.server.register_handler("notalone.notify", self._handle_remote_notification)

        logger.info(f"RemoteNotificationHub started (session: {self.session_id})")
        logger.info(f"Callback endpoint: {self.server.get_callback_url()}")

    async def stop(self):
        """Stop hub and RPC components."""
        await self.client.stop()
        await self.server.stop()
        await self.hub.stop()
        logger.info("RemoteNotificationHub stopped")

    async def watch_ticket_remote(
        self,
        remote_url: str,
        ticket_id: int,
        states: list,
        wake_prompt: str
    ) -> str:
        """
        Watch a ticket on a remote system (e.g., VibeKanban).

        Args:
            remote_url: URL of remote notalone registration endpoint
            ticket_id: Ticket ID to watch
            states: States to watch for
            wake_prompt: Prompt to inject when ticket changes

        Returns:
            notification_id
        """
        # Register with remote system via RPC
        result = await self.client.register(
            remote_url=remote_url,
            session_id=self.session_id,
            callback_endpoint=self.server.get_callback_url(),
            notification={
                "type": "ticket_update",
                "params": {
                    "ticket_id": ticket_id,
                    "states": states
                },
                "wake_prompt": wake_prompt
            }
        )

        notification_id = result.get("notification_id")
        self._remote_registrations[notification_id] = remote_url

        logger.info(f"Registered remote watch for ticket {ticket_id}: {notification_id}")
        return notification_id

    async def subscribe_channel_remote(
        self,
        remote_url: str,
        channel: str,
        wake_prompt: str
    ) -> str:
        """
        Subscribe to a channel on a remote system.

        Args:
            remote_url: URL of remote notalone registration endpoint
            channel: Channel name
            wake_prompt: Prompt to inject when message received

        Returns:
            notification_id
        """
        result = await self.client.register(
            remote_url=remote_url,
            session_id=self.session_id,
            callback_endpoint=self.server.get_callback_url(),
            notification={
                "type": "channel",
                "params": {"channel": channel},
                "wake_prompt": wake_prompt
            }
        )

        notification_id = result.get("notification_id")
        self._remote_registrations[notification_id] = remote_url

        logger.info(f"Subscribed to remote channel {channel}: {notification_id}")
        return notification_id

    async def unregister_remote(self, notification_id: str):
        """Unregister a remote notification."""
        remote_url = self._remote_registrations.get(notification_id)
        if not remote_url:
            raise ValueError(f"Unknown notification: {notification_id}")

        await self.client.unregister(remote_url, notification_id)
        del self._remote_registrations[notification_id]

        logger.info(f"Unregistered remote notification: {notification_id}")

    async def _handle_remote_notification(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle incoming notification from remote system.

        Called by RPC server when remote system sends notification.
        """
        notification_id = params.get("notification_id")
        session_id = params.get("session_id")
        event_type = params.get("event_type")
        data = params.get("data")

        logger.info(f"Received remote notification: {notification_id} ({event_type})")

        # Verify it's for this session
        if session_id != self.session_id:
            logger.warning(f"Notification for different session: {session_id}")
            return {"status": "ignored", "reason": "wrong_session"}

        # Look up the registration to get the wake_prompt
        if notification_id not in self._remote_registrations:
            logger.warning(f"Unknown notification_id: {notification_id}")
            return {"status": "ignored", "reason": "unknown_notification"}

        # Format wake prompt with event data
        wake_prompt = f"🔔 Remote notification: {event_type}"
        if data:
            import json
            wake_prompt += f"\n```json\n{json.dumps(data, indent=2)}\n```"

        # Send the notification through the backend
        # The backend's _send_notification callback will inject it into the Sublime session
        try:
            if hasattr(self.hub, 'backend') and hasattr(self.hub.backend, '_send_notification'):
                self.hub.backend._send_notification(
                    "notification_wake",
                    {
                        "notification_id": notification_id,
                        "event_type": event_type,
                        "wake_prompt": wake_prompt,
                        "data": data
                    }
                )
                logger.info(f"Injected wake_prompt for {notification_id}")
            else:
                logger.warning("Backend doesn't support _send_notification")
        except Exception as e:
            logger.error(f"Failed to inject wake_prompt: {e}")

        return {"status": "delivered"}

    # =========================================================================
    # Delegate local notification methods to hub
    # =========================================================================

    async def set_timer(self, seconds: int, wake_prompt: str, **kwargs):
        """Set a local timer notification."""
        return await self.hub.set_timer(seconds, wake_prompt, **kwargs)

    async def wait_for_session(self, subsession_id: str, wake_prompt: str, **kwargs):
        """Wait for a subsession to complete (local)."""
        return await self.hub.wait_for_session(subsession_id, wake_prompt, **kwargs)

    async def cancel_notification(self, notification_id: str):
        """Cancel a notification (local or remote)."""
        if notification_id in self._remote_registrations:
            await self.unregister_remote(notification_id)
        else:
            return await self.hub.cancel_notification(notification_id)

    async def list_notifications(self):
        """List all notifications (local only for now)."""
        return await self.hub.list_notifications()
