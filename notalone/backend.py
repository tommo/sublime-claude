"""
Abstract backend interface for notification delivery.
"""
from abc import ABC, abstractmethod
from typing import Callable, Optional, Dict, Any, List
from .types import Notification, NotificationResult, ChannelMessage


# Type alias for notification callback
NotificationCallback = Callable[[Notification], None]


class NotificationBackend(ABC):
    """
    Abstract base class for notification backends.

    Each backend (Sublime, VS Code, terminal, etc.) implements this interface
    to provide notification delivery using its native mechanisms.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the backend name (e.g., 'sublime', 'vscode')."""
        pass

    @property
    def capabilities(self) -> List[str]:
        """Return list of supported notification types.

        Override to restrict capabilities. Default: all types supported.
        """
        from .types import NotificationType
        return [t.value for t in NotificationType]

    # =========================================================================
    # Core Notification Methods
    # =========================================================================

    @abstractmethod
    async def set_notification(
        self,
        notification: Notification,
        callback: NotificationCallback
    ) -> NotificationResult:
        """
        Register a notification to fire when its event occurs.

        Args:
            notification: The notification configuration
            callback: Called when notification fires

        Returns:
            NotificationResult with status and ID
        """
        pass

    @abstractmethod
    async def cancel_notification(self, notification_id: str) -> NotificationResult:
        """Cancel a pending notification."""
        pass

    @abstractmethod
    async def list_notifications(
        self,
        session_id: Optional[str] = None
    ) -> List[Notification]:
        """List active notifications, optionally filtered by session."""
        pass

    # =========================================================================
    # Channel/Pub-Sub Methods
    # =========================================================================

    @abstractmethod
    async def broadcast(
        self,
        channel: Optional[str],
        message: str,
        data: Optional[Dict[str, Any]] = None,
        sender_session_id: Optional[str] = None
    ) -> int:
        """
        Broadcast a message to channel subscribers.

        Args:
            channel: Channel name, or None for global broadcast
            message: The message/wake_prompt to send
            data: Optional additional data payload
            sender_session_id: Who's sending (excluded from delivery)

        Returns:
            Number of sessions notified
        """
        pass

    @abstractmethod
    async def subscribe_channel(
        self,
        session_id: str,
        channel: str
    ) -> bool:
        """Subscribe a session to a channel."""
        pass

    @abstractmethod
    async def unsubscribe_channel(
        self,
        session_id: str,
        channel: str
    ) -> bool:
        """Unsubscribe a session from a channel."""
        pass

    async def get_channel_subscribers(self, channel: str) -> List[str]:
        """Get list of session IDs subscribed to a channel."""
        return []  # Default: no subscriber tracking

    # =========================================================================
    # Session Management
    # =========================================================================

    async def register_session(self, session_id: str) -> None:
        """Register a new session. Called when session starts."""
        pass

    async def unregister_session(self, session_id: str) -> None:
        """Unregister a session. Called when session ends.

        Should clean up any notifications and subscriptions for this session.
        """
        pass

    # =========================================================================
    # Event Signaling (for subsession/agent completion)
    # =========================================================================

    async def signal_session_complete(
        self,
        session_id: str,
        result: Optional[Any] = None
    ) -> int:
        """
        Signal that a session has completed.

        Args:
            session_id: The completed session's ID
            result: Optional result data

        Returns:
            Number of notifications triggered
        """
        return 0  # Default: no-op

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """Start the backend. Called on initialization."""
        pass

    async def stop(self) -> None:
        """Stop the backend. Clean up resources."""
        pass
