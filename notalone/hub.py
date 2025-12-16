"""
NotificationHub - Central coordinator for the notification protocol.
"""
import asyncio
import logging
from typing import Callable, Optional, Dict, Any, List
from datetime import datetime

from .types import (
    Notification, NotificationResult, NotificationType,
    NotificationParams, ChannelMessage
)
from .backend import NotificationBackend, NotificationCallback

logger = logging.getLogger(__name__)


class NotificationHub:
    """
    Central hub for managing notifications across backends.

    The hub provides a unified API and delegates to the configured backend
    for actual notification delivery.
    """

    def __init__(self, backend: NotificationBackend):
        self.backend = backend
        self._started = False

        # Internal tracking (backend may also track)
        self._notifications: Dict[str, Notification] = {}
        self._callbacks: Dict[str, NotificationCallback] = {}
        self._channel_subscriptions: Dict[str, set] = {}  # channel -> session_ids

    async def start(self) -> None:
        """Start the hub and backend."""
        if self._started:
            return
        await self.backend.start()
        self._started = True
        logger.info(f"NotificationHub started with backend: {self.backend.name}")

    async def stop(self) -> None:
        """Stop the hub and backend."""
        if not self._started:
            return
        await self.backend.stop()
        self._started = False
        logger.info("NotificationHub stopped")

    # =========================================================================
    # Notification Management
    # =========================================================================

    async def set_notification(
        self,
        notification_type: NotificationType,
        params: NotificationParams,
        wake_prompt: str,
        source_session_id: Optional[str] = None,
        target_session_id: Optional[str] = None,
        callback: Optional[NotificationCallback] = None
    ) -> NotificationResult:
        """
        Set a notification to fire when an event occurs.

        Args:
            notification_type: Type of notification
            params: Event-specific parameters
            wake_prompt: Prompt to inject when notification fires
            source_session_id: Who's setting the notification
            target_session_id: Who receives it (None = source)
            callback: Optional callback when notification fires

        Returns:
            NotificationResult with the notification ID
        """
        # Check backend capability
        if notification_type.value not in self.backend.capabilities:
            return NotificationResult.error(
                "",
                f"Backend '{self.backend.name}' doesn't support {notification_type.value}"
            )

        # Create notification
        notification = Notification(
            notification_type=notification_type,
            params=params,
            wake_prompt=wake_prompt,
            source_session_id=source_session_id,
            target_session_id=target_session_id or source_session_id,
        )

        # Store locally
        self._notifications[notification.id] = notification
        if callback:
            self._callbacks[notification.id] = callback

        # Delegate to backend
        def wrapped_callback(n: Notification):
            self._on_notification_fired(n)
            if callback:
                callback(n)

        result = await self.backend.set_notification(notification, wrapped_callback)

        if result.status == "error":
            # Clean up on error
            self._notifications.pop(notification.id, None)
            self._callbacks.pop(notification.id, None)

        return result

    async def cancel_notification(self, notification_id: str) -> NotificationResult:
        """Cancel a pending notification."""
        notification = self._notifications.get(notification_id)
        if not notification:
            return NotificationResult.not_found(notification_id)

        result = await self.backend.cancel_notification(notification_id)

        if result.status != "error":
            notification.cancelled = True
            self._notifications.pop(notification_id, None)
            self._callbacks.pop(notification_id, None)

        return result

    async def list_notifications(
        self,
        session_id: Optional[str] = None
    ) -> List[Notification]:
        """List active notifications."""
        return await self.backend.list_notifications(session_id)

    def _on_notification_fired(self, notification: Notification) -> None:
        """Internal callback when notification fires."""
        notification.fired = True
        notification.fired_at = datetime.utcnow()
        logger.debug(f"Notification fired: {notification.id} ({notification.notification_type})")

    # =========================================================================
    # Convenience Methods for Common Notification Types
    # =========================================================================

    async def set_timer(
        self,
        seconds: int,
        wake_prompt: str,
        session_id: Optional[str] = None,
        repeat: bool = False,
        callback: Optional[NotificationCallback] = None
    ) -> NotificationResult:
        """Set a timer notification."""
        return await self.set_notification(
            NotificationType.TIMER,
            NotificationParams(seconds=seconds, repeat=repeat),
            wake_prompt,
            source_session_id=session_id,
            callback=callback
        )

    async def wait_for_session(
        self,
        subsession_id: str,
        wake_prompt: str,
        session_id: Optional[str] = None,
        callback: Optional[NotificationCallback] = None
    ) -> NotificationResult:
        """Wait for a subsession to complete."""
        return await self.set_notification(
            NotificationType.SUBSESSION_COMPLETE,
            NotificationParams(subsession_id=subsession_id),
            wake_prompt,
            source_session_id=session_id,
            callback=callback
        )

    async def watch_ticket(
        self,
        ticket_id: int,
        states: List[str],
        wake_prompt: str,
        session_id: Optional[str] = None,
        callback: Optional[NotificationCallback] = None
    ) -> NotificationResult:
        """Watch a ticket for state changes."""
        return await self.set_notification(
            NotificationType.TICKET_UPDATE,
            NotificationParams(ticket_id=ticket_id, states=states),
            wake_prompt,
            source_session_id=session_id,
            callback=callback
        )

    # =========================================================================
    # Channel/Broadcast Methods
    # =========================================================================

    async def subscribe(
        self,
        session_id: str,
        channel: str
    ) -> bool:
        """Subscribe a session to a channel."""
        result = await self.backend.subscribe_channel(session_id, channel)
        if result:
            if channel not in self._channel_subscriptions:
                self._channel_subscriptions[channel] = set()
            self._channel_subscriptions[channel].add(session_id)
        return result

    async def unsubscribe(
        self,
        session_id: str,
        channel: str
    ) -> bool:
        """Unsubscribe a session from a channel."""
        result = await self.backend.unsubscribe_channel(session_id, channel)
        if result and channel in self._channel_subscriptions:
            self._channel_subscriptions[channel].discard(session_id)
        return result

    async def broadcast(
        self,
        message: str,
        channel: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        sender_session_id: Optional[str] = None
    ) -> int:
        """
        Broadcast a message to subscribers.

        Args:
            message: The wake_prompt/message to send
            channel: Channel name, or None for global broadcast
            data: Optional data payload
            sender_session_id: Exclude sender from delivery

        Returns:
            Number of sessions notified
        """
        return await self.backend.broadcast(
            channel, message, data, sender_session_id
        )

    # =========================================================================
    # Session Lifecycle
    # =========================================================================

    async def register_session(self, session_id: str) -> None:
        """Register a session with the hub."""
        await self.backend.register_session(session_id)

    async def unregister_session(self, session_id: str) -> None:
        """Unregister a session, cleaning up notifications and subscriptions."""
        # Clean up local tracking
        to_remove = [
            nid for nid, n in self._notifications.items()
            if n.source_session_id == session_id or n.target_session_id == session_id
        ]
        for nid in to_remove:
            self._notifications.pop(nid, None)
            self._callbacks.pop(nid, None)

        # Remove from channels
        for subs in self._channel_subscriptions.values():
            subs.discard(session_id)

        await self.backend.unregister_session(session_id)

    async def signal_session_complete(
        self,
        session_id: str,
        result: Optional[Any] = None
    ) -> int:
        """Signal that a session has completed."""
        return await self.backend.signal_session_complete(session_id, result)
