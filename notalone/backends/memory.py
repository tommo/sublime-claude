"""
In-memory backend for testing and simple use cases.
"""
import asyncio
import logging
from typing import Callable, Optional, Dict, Any, List, Set
from datetime import datetime

from ..types import Notification, NotificationResult, NotificationType
from ..backend import NotificationBackend, NotificationCallback

logger = logging.getLogger(__name__)


class InMemoryBackend(NotificationBackend):
    """
    In-memory notification backend for testing.

    Provides a fully functional implementation that stores everything in memory.
    Useful for unit tests and single-process applications.
    """

    def __init__(self):
        self._notifications: Dict[str, Notification] = {}
        self._callbacks: Dict[str, NotificationCallback] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

        # Channel subscriptions: channel -> set of session_ids
        self._subscriptions: Dict[str, Set[str]] = {}

        # Session completion events for subsession notifications
        self._session_events: Dict[str, asyncio.Event] = {}
        self._session_results: Dict[str, Any] = {}

        # Fired notifications log (for testing)
        self._fired_log: List[Notification] = []

    @property
    def name(self) -> str:
        return "memory"

    async def start(self) -> None:
        logger.info("InMemoryBackend started")

    async def stop(self) -> None:
        # Cancel all pending tasks
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        self._notifications.clear()
        self._callbacks.clear()
        logger.info("InMemoryBackend stopped")

    # =========================================================================
    # Core Notification Methods
    # =========================================================================

    async def set_notification(
        self,
        notification: Notification,
        callback: NotificationCallback
    ) -> NotificationResult:
        """Register a notification."""
        self._notifications[notification.id] = notification
        self._callbacks[notification.id] = callback

        # Start monitoring based on type
        if notification.notification_type == NotificationType.TIMER:
            task = asyncio.create_task(
                self._monitor_timer(notification)
            )
            self._tasks[notification.id] = task

        elif notification.notification_type in (
            NotificationType.SUBSESSION_COMPLETE,
            NotificationType.AGENT_COMPLETE
        ):
            task = asyncio.create_task(
                self._monitor_session_complete(notification)
            )
            self._tasks[notification.id] = task

        elif notification.notification_type == NotificationType.CHANNEL:
            # Channel notifications fire on broadcast - no task needed
            pass

        elif notification.notification_type == NotificationType.TICKET_UPDATE:
            # Ticket updates are triggered externally - no task needed
            pass

        logger.debug(f"Set notification: {notification.id} ({notification.notification_type})")
        return NotificationResult.success(notification.id, "set")

    async def cancel_notification(self, notification_id: str) -> NotificationResult:
        """Cancel a pending notification."""
        if notification_id not in self._notifications:
            return NotificationResult.not_found(notification_id)

        # Cancel task if exists
        if notification_id in self._tasks:
            self._tasks[notification_id].cancel()
            del self._tasks[notification_id]

        notification = self._notifications.pop(notification_id)
        notification.cancelled = True
        self._callbacks.pop(notification_id, None)

        logger.debug(f"Cancelled notification: {notification_id}")
        return NotificationResult.success(notification_id, "cancelled")

    async def list_notifications(
        self,
        session_id: Optional[str] = None
    ) -> List[Notification]:
        """List active notifications."""
        notifications = list(self._notifications.values())
        if session_id:
            notifications = [
                n for n in notifications
                if n.source_session_id == session_id or n.target_session_id == session_id
            ]
        return notifications

    # =========================================================================
    # Timer Monitoring
    # =========================================================================

    async def _monitor_timer(self, notification: Notification) -> None:
        """Monitor a timer notification."""
        try:
            seconds = notification.params.seconds or 0
            await asyncio.sleep(seconds)

            if notification.id in self._notifications:
                await self._fire_notification(notification)

                # Handle repeat
                if notification.params.repeat:
                    # Reset and reschedule
                    task = asyncio.create_task(self._monitor_timer(notification))
                    self._tasks[notification.id] = task

        except asyncio.CancelledError:
            logger.debug(f"Timer cancelled: {notification.id}")

    # =========================================================================
    # Session Completion Monitoring
    # =========================================================================

    async def _monitor_session_complete(self, notification: Notification) -> None:
        """Monitor for session completion."""
        try:
            session_id = (
                notification.params.subsession_id or
                notification.params.agent_id
            )
            if not session_id:
                logger.warning(f"No session_id in notification {notification.id}")
                return

            # Get or create event for this session
            if session_id not in self._session_events:
                self._session_events[session_id] = asyncio.Event()

            event = self._session_events[session_id]
            await event.wait()

            if notification.id in self._notifications:
                # Include session result in notification data
                if session_id in self._session_results:
                    if notification.params.data is None:
                        notification.params.data = {}
                    notification.params.data["session_result"] = self._session_results[session_id]

                await self._fire_notification(notification)

        except asyncio.CancelledError:
            logger.debug(f"Session monitor cancelled: {notification.id}")

    async def signal_session_complete(
        self,
        session_id: str,
        result: Optional[Any] = None
    ) -> int:
        """Signal that a session has completed."""
        self._session_results[session_id] = result

        # Count notifications that will fire
        count = sum(
            1 for n in self._notifications.values()
            if n.notification_type in (NotificationType.SUBSESSION_COMPLETE, NotificationType.AGENT_COMPLETE)
            and (n.params.subsession_id == session_id or n.params.agent_id == session_id)
        )

        # Set the event (create if not exists)
        if session_id not in self._session_events:
            self._session_events[session_id] = asyncio.Event()
        self._session_events[session_id].set()

        return count

    # =========================================================================
    # Fire Notification
    # =========================================================================

    async def _fire_notification(self, notification: Notification) -> None:
        """Fire a notification, calling its callback."""
        notification.fired = True
        notification.fired_at = datetime.utcnow()

        self._fired_log.append(notification)

        callback = self._callbacks.get(notification.id)
        if callback:
            try:
                callback(notification)
            except Exception as e:
                logger.error(f"Callback error for {notification.id}: {e}")

        # Clean up (unless repeating timer)
        if not (notification.notification_type == NotificationType.TIMER and notification.params.repeat):
            self._notifications.pop(notification.id, None)
            self._callbacks.pop(notification.id, None)
            self._tasks.pop(notification.id, None)

        logger.debug(f"Fired notification: {notification.id}")

    # =========================================================================
    # Channel/Broadcast Methods
    # =========================================================================

    async def broadcast(
        self,
        channel: Optional[str],
        message: str,
        data: Optional[Dict[str, Any]] = None,
        sender_session_id: Optional[str] = None
    ) -> int:
        """Broadcast to channel subscribers."""
        if channel:
            subscribers = self._subscriptions.get(channel, set())
        else:
            # Global broadcast - all unique session IDs
            subscribers = set()
            for subs in self._subscriptions.values():
                subscribers.update(subs)

        # Exclude sender
        if sender_session_id:
            subscribers = subscribers - {sender_session_id}

        # Fire channel notifications for these subscribers
        fired_count = 0
        for notification in list(self._notifications.values()):
            if notification.notification_type == NotificationType.CHANNEL:
                if notification.params.channel == channel or channel is None:
                    if notification.target_session_id in subscribers:
                        notification.wake_prompt = message
                        if data:
                            notification.params.data = data
                        await self._fire_notification(notification)
                        fired_count += 1

        return fired_count

    async def subscribe_channel(self, session_id: str, channel: str) -> bool:
        """Subscribe a session to a channel."""
        if channel not in self._subscriptions:
            self._subscriptions[channel] = set()
        self._subscriptions[channel].add(session_id)
        logger.debug(f"Session {session_id} subscribed to {channel}")
        return True

    async def unsubscribe_channel(self, session_id: str, channel: str) -> bool:
        """Unsubscribe a session from a channel."""
        if channel in self._subscriptions:
            self._subscriptions[channel].discard(session_id)
            logger.debug(f"Session {session_id} unsubscribed from {channel}")
            return True
        return False

    async def get_channel_subscribers(self, channel: str) -> List[str]:
        """Get subscribers for a channel."""
        return list(self._subscriptions.get(channel, set()))

    # =========================================================================
    # Session Management
    # =========================================================================

    async def unregister_session(self, session_id: str) -> None:
        """Clean up when a session ends."""
        # Cancel notifications for this session
        to_cancel = [
            nid for nid, n in self._notifications.items()
            if n.source_session_id == session_id or n.target_session_id == session_id
        ]
        for nid in to_cancel:
            await self.cancel_notification(nid)

        # Remove from all channels
        for subs in self._subscriptions.values():
            subs.discard(session_id)

        # Clean up session event
        self._session_events.pop(session_id, None)
        self._session_results.pop(session_id, None)

    # =========================================================================
    # Testing Helpers
    # =========================================================================

    def get_fired_notifications(self) -> List[Notification]:
        """Get log of fired notifications (for testing)."""
        return list(self._fired_log)

    def clear_fired_log(self) -> None:
        """Clear the fired notifications log."""
        self._fired_log.clear()

    async def trigger_ticket_update(
        self,
        ticket_id: int,
        new_state: str,
        data: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Trigger ticket update notifications (for testing/integration).

        Returns number of notifications fired.
        """
        fired = 0
        for notification in list(self._notifications.values()):
            if notification.notification_type == NotificationType.TICKET_UPDATE:
                if notification.params.ticket_id == ticket_id:
                    if notification.params.states is None or new_state in notification.params.states:
                        if data:
                            notification.params.data = data
                        notification.params.data = notification.params.data or {}
                        notification.params.data["new_state"] = new_state
                        await self._fire_notification(notification)
                        fired += 1
        return fired
