"""
Sublime Text MCP backend for the notification protocol.

Maps the notification protocol to Sublime's existing alarm system patterns.
This is an adapter that can be used within the Sublime MCP bridge.
"""
import asyncio
import logging
from typing import Callable, Optional, Dict, Any, List, Set
from datetime import datetime

from ..types import (
    Notification, NotificationResult, NotificationType,
    NotificationParams
)
from ..backend import NotificationBackend, NotificationCallback

logger = logging.getLogger(__name__)


# Type for the send_notification function from bridge
SendNotificationFunc = Callable[[str, Dict[str, Any]], None]


class SublimeNotificationBackend(NotificationBackend):
    """
    Sublime Text implementation of the notification backend.

    Maps to the existing alarm system in bridge/main.py:
    - TIMER → _monitor_time_alarm pattern (asyncio.sleep)
    - SUBSESSION_COMPLETE → _monitor_subsession_alarm with asyncio.Event
    - AGENT_COMPLETE → alias for SUBSESSION_COMPLETE
    - CHANNEL → channel subscriptions with broadcast
    - BROADCAST → global broadcast to all sessions

    Usage:
        # In bridge/main.py:
        from notification_protocol.backends.sublime import SublimeNotificationBackend

        backend = SublimeNotificationBackend(send_notification_func)
        await backend.start()

        # Set a timer
        await backend.set_notification(
            Notification(
                notification_type=NotificationType.TIMER,
                params=NotificationParams(seconds=30),
                wake_prompt="Timer completed!"
            ),
            callback=lambda n: print(f"Fired: {n.id}")
        )
    """

    def __init__(
        self,
        send_notification: SendNotificationFunc,
        session_id: Optional[str] = None
    ):
        """
        Initialize the Sublime backend.

        Args:
            send_notification: Function to send JSON-RPC notifications to Sublime.
                               Signature: (method: str, params: dict) -> None
            session_id: The owning session's ID (for self-targeting)
        """
        self._send_notification = send_notification
        self._session_id = session_id

        # Notification storage
        self._notifications: Dict[str, Notification] = {}
        self._callbacks: Dict[str, NotificationCallback] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

        # Subsession completion events (shared across notifications)
        self._subsession_events: Dict[str, asyncio.Event] = {}

        # Channel subscriptions: channel_name -> set of session_ids
        self._channel_subscriptions: Dict[str, Set[str]] = {}

        # Registered sessions for broadcast
        self._registered_sessions: Set[str] = set()

    @property
    def name(self) -> str:
        return "sublime"

    @property
    def capabilities(self) -> List[str]:
        # Sublime backend supports these notification types
        return [
            NotificationType.TIMER.value,
            NotificationType.SUBSESSION_COMPLETE.value,
            NotificationType.AGENT_COMPLETE.value,
            NotificationType.CHANNEL.value,
            NotificationType.BROADCAST.value,
        ]

    # =========================================================================
    # Core Notification Methods
    # =========================================================================

    async def set_notification(
        self,
        notification: Notification,
        callback: NotificationCallback
    ) -> NotificationResult:
        """Register a notification based on its type."""
        ntype = notification.notification_type

        # Store notification and callback
        self._notifications[notification.id] = notification
        self._callbacks[notification.id] = callback

        try:
            if ntype == NotificationType.TIMER:
                task = asyncio.create_task(
                    self._monitor_timer(notification)
                )
                self._tasks[notification.id] = task

            elif ntype in (NotificationType.SUBSESSION_COMPLETE, NotificationType.AGENT_COMPLETE):
                task = asyncio.create_task(
                    self._monitor_subsession(notification)
                )
                self._tasks[notification.id] = task

            elif ntype == NotificationType.CHANNEL:
                # Channel subscriptions are passive - they fire on broadcast
                channel = notification.params.channel
                if channel:
                    target = notification.target_session_id or self._session_id
                    if target:
                        await self.subscribe_channel(target, channel)

            elif ntype == NotificationType.BROADCAST:
                # Broadcast notifications are passive - stored for delivery
                pass

            else:
                return NotificationResult.error(
                    notification.id,
                    f"Unsupported notification type: {ntype.value}"
                )

            logger.debug(f"[Sublime] Set notification {notification.id}: {ntype.value}")
            return NotificationResult.success(notification.id, "set")

        except Exception as e:
            # Cleanup on error
            self._notifications.pop(notification.id, None)
            self._callbacks.pop(notification.id, None)
            logger.error(f"[Sublime] Error setting notification: {e}")
            return NotificationResult.error(notification.id, str(e))

    async def cancel_notification(self, notification_id: str) -> NotificationResult:
        """Cancel a pending notification."""
        notification = self._notifications.get(notification_id)
        if not notification:
            return NotificationResult.not_found(notification_id)

        # Cancel the monitoring task if exists
        task = self._tasks.pop(notification_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Clean up
        notification.cancelled = True
        self._notifications.pop(notification_id, None)
        self._callbacks.pop(notification_id, None)

        logger.debug(f"[Sublime] Cancelled notification {notification_id}")
        return NotificationResult.success(notification_id, "cancelled")

    async def list_notifications(
        self,
        session_id: Optional[str] = None
    ) -> List[Notification]:
        """List active notifications, optionally filtered by session."""
        notifications = list(self._notifications.values())

        if session_id:
            notifications = [
                n for n in notifications
                if n.source_session_id == session_id or n.target_session_id == session_id
            ]

        return [n for n in notifications if not n.cancelled and not n.fired]

    # =========================================================================
    # Timer Monitoring (maps to _monitor_time_alarm)
    # =========================================================================

    async def _monitor_timer(self, notification: Notification) -> None:
        """Monitor a timer notification - sleep then fire."""
        seconds = notification.params.seconds or 0

        logger.debug(f"[Sublime] Timer {notification.id}: sleeping {seconds}s")

        try:
            await asyncio.sleep(seconds)
            await self._fire_notification(notification)

            # Handle repeat timers
            if notification.params.repeat and not notification.cancelled:
                # Reset and reschedule
                notification.fired = False
                notification.fired_at = None
                task = asyncio.create_task(self._monitor_timer(notification))
                self._tasks[notification.id] = task

        except asyncio.CancelledError:
            logger.debug(f"[Sublime] Timer {notification.id} cancelled")

    # =========================================================================
    # Subsession Monitoring (maps to _monitor_subsession_alarm)
    # =========================================================================

    async def _monitor_subsession(self, notification: Notification) -> None:
        """Monitor a subsession completion - wait on asyncio.Event then fire."""
        # Get subsession_id (or agent_id for AGENT_COMPLETE type)
        subsession_id = (
            notification.params.subsession_id or
            notification.params.agent_id
        )

        if not subsession_id:
            logger.error(f"[Sublime] No subsession_id in notification {notification.id}")
            return

        logger.debug(f"[Sublime] Waiting for subsession {subsession_id}")

        # Create or get the completion event
        if subsession_id not in self._subsession_events:
            self._subsession_events[subsession_id] = asyncio.Event()

        event = self._subsession_events[subsession_id]

        try:
            # Wait with timeout (1 hour max to prevent infinite wait)
            await asyncio.wait_for(event.wait(), timeout=3600)
            await self._fire_notification(notification)

        except asyncio.TimeoutError:
            logger.warning(f"[Sublime] Subsession wait {notification.id} timed out")

        except asyncio.CancelledError:
            logger.debug(f"[Sublime] Subsession wait {notification.id} cancelled")

    # =========================================================================
    # Notification Firing (maps to _fire_alarm)
    # =========================================================================

    async def _fire_notification(self, notification: Notification) -> None:
        """Fire a notification by sending the wake prompt."""
        if notification.cancelled:
            return

        notification.fired = True
        notification.fired_at = datetime.utcnow()

        logger.info(f"[Sublime] FIRING notification {notification.id}")

        # Send wake notification to Sublime session
        # This matches the existing alarm_wake notification format
        self._send_notification("alarm_wake", {
            "alarm_id": notification.id,
            "event_type": notification.notification_type.value,
            "wake_prompt": notification.wake_prompt,
        })

        # Call the registered callback
        callback = self._callbacks.get(notification.id)
        if callback:
            try:
                callback(notification)
            except Exception as e:
                logger.error(f"[Sublime] Callback error for {notification.id}: {e}")

        # Cleanup (unless repeating)
        if not notification.params.repeat:
            self._tasks.pop(notification.id, None)
            self._notifications.pop(notification.id, None)
            self._callbacks.pop(notification.id, None)

    # =========================================================================
    # Session Completion Signaling (maps to signal_subsession_complete)
    # =========================================================================

    async def signal_session_complete(
        self,
        session_id: str,
        result: Optional[Any] = None
    ) -> int:
        """Signal that a session has completed, triggering waiting notifications."""
        if session_id in self._subsession_events:
            self._subsession_events[session_id].set()
            logger.info(f"[Sublime] Session {session_id} completed - event signaled")

            # Count how many notifications were waiting
            count = sum(
                1 for n in self._notifications.values()
                if (n.params.subsession_id == session_id or
                    n.params.agent_id == session_id)
                and not n.fired
            )
            return count

        return 0

    # =========================================================================
    # Channel/Broadcast Methods
    # =========================================================================

    async def subscribe_channel(self, session_id: str, channel: str) -> bool:
        """Subscribe a session to a channel."""
        if channel not in self._channel_subscriptions:
            self._channel_subscriptions[channel] = set()

        self._channel_subscriptions[channel].add(session_id)
        logger.debug(f"[Sublime] Session {session_id} subscribed to channel {channel}")
        return True

    async def unsubscribe_channel(self, session_id: str, channel: str) -> bool:
        """Unsubscribe a session from a channel."""
        if channel in self._channel_subscriptions:
            self._channel_subscriptions[channel].discard(session_id)
            logger.debug(f"[Sublime] Session {session_id} unsubscribed from channel {channel}")
            return True
        return False

    async def get_channel_subscribers(self, channel: str) -> List[str]:
        """Get list of session IDs subscribed to a channel."""
        return list(self._channel_subscriptions.get(channel, set()))

    async def broadcast(
        self,
        channel: Optional[str],
        message: str,
        data: Optional[Dict[str, Any]] = None,
        sender_session_id: Optional[str] = None
    ) -> int:
        """Broadcast a message to channel subscribers or all sessions."""
        if channel:
            # Channel-specific broadcast
            subscribers = self._channel_subscriptions.get(channel, set())
        else:
            # Global broadcast to all registered sessions
            subscribers = self._registered_sessions

        # Exclude sender
        if sender_session_id:
            subscribers = subscribers - {sender_session_id}

        count = 0
        for session_id in subscribers:
            # Find notifications for this session/channel
            for notification in list(self._notifications.values()):
                if notification.notification_type == NotificationType.CHANNEL:
                    if notification.params.channel == channel:
                        if notification.target_session_id == session_id:
                            # Send the broadcast as a wake
                            self._send_notification("alarm_wake", {
                                "alarm_id": notification.id,
                                "event_type": "channel_broadcast",
                                "wake_prompt": message,
                                "channel": channel,
                                "data": data,
                            })
                            count += 1

        # Also send direct broadcast notification for global broadcasts
        if not channel:
            for session_id in subscribers:
                self._send_notification("broadcast", {
                    "message": message,
                    "data": data,
                    "sender": sender_session_id,
                })
                count += 1

        logger.debug(f"[Sublime] Broadcast to {count} sessions (channel={channel})")
        return count

    # =========================================================================
    # Session Management
    # =========================================================================

    async def register_session(self, session_id: str) -> None:
        """Register a session for broadcast."""
        self._registered_sessions.add(session_id)
        logger.debug(f"[Sublime] Session {session_id} registered")

    async def unregister_session(self, session_id: str) -> None:
        """Unregister a session, cleaning up notifications and subscriptions."""
        self._registered_sessions.discard(session_id)

        # Cancel notifications for this session
        to_cancel = [
            nid for nid, n in self._notifications.items()
            if n.source_session_id == session_id or n.target_session_id == session_id
        ]
        for nid in to_cancel:
            await self.cancel_notification(nid)

        # Remove from all channels
        for subs in self._channel_subscriptions.values():
            subs.discard(session_id)

        # Clean up subsession event if exists
        if session_id in self._subsession_events:
            del self._subsession_events[session_id]

        logger.debug(f"[Sublime] Session {session_id} unregistered")

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """Start the backend."""
        logger.info("[Sublime] Notification backend started")

    async def stop(self) -> None:
        """Stop the backend, cancelling all pending notifications."""
        # Cancel all tasks
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()

        # Wait for all to complete
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

        self._tasks.clear()
        self._notifications.clear()
        self._callbacks.clear()
        self._subsession_events.clear()
        self._channel_subscriptions.clear()
        self._registered_sessions.clear()

        logger.info("[Sublime] Notification backend stopped")


# =============================================================================
# Factory function for easy integration with bridge/main.py
# =============================================================================

def create_sublime_backend(
    send_notification: SendNotificationFunc,
    session_id: Optional[str] = None
) -> SublimeNotificationBackend:
    """
    Factory function to create a Sublime notification backend.

    Example integration with bridge/main.py:

        from notification_protocol.backends.sublime import create_sublime_backend

        class Bridge:
            def __init__(self):
                # ... existing init ...
                self.notification_backend = create_sublime_backend(
                    send_notification=send_notification,
                    session_id=self._session_id
                )

            async def set_alarm(self, id: int, params: dict) -> None:
                # Map old API to new protocol
                from notification_protocol import NotificationType, NotificationParams, Notification

                event_type = params.get("event_type")
                event_params = params.get("event_params", {})
                wake_prompt = params.get("wake_prompt", "")
                alarm_id = params.get("alarm_id")

                if event_type == "time_elapsed":
                    ntype = NotificationType.TIMER
                    nparams = NotificationParams(seconds=event_params.get("seconds", 0))
                elif event_type in ("subsession_complete", "agent_complete"):
                    ntype = NotificationType.SUBSESSION_COMPLETE
                    nparams = NotificationParams(
                        subsession_id=event_params.get("subsession_id"),
                        agent_id=event_params.get("agent_id")
                    )
                else:
                    send_error(id, -32602, f"Unknown event_type: {event_type}")
                    return

                notification = Notification(
                    notification_type=ntype,
                    params=nparams,
                    wake_prompt=wake_prompt,
                )
                if alarm_id:
                    notification.id = alarm_id

                result = await self.notification_backend.set_notification(
                    notification,
                    callback=lambda n: None  # Callback optional
                )
                send_result(id, {
                    "alarm_id": result.notification_id,
                    "status": result.status,
                    "event_type": event_type
                })
    """
    return SublimeNotificationBackend(send_notification, session_id)
