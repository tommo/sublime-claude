"""
Core types for the Agent Notification Protocol.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Dict, List
from datetime import datetime
import uuid


class NotificationType(Enum):
    """Types of notifications supported by the protocol."""
    TIMER = "timer"                         # Wake after N seconds
    SUBSESSION_COMPLETE = "subsession_complete"  # Wake when session completes
    AGENT_COMPLETE = "agent_complete"       # Alias for subsession_complete
    TICKET_UPDATE = "ticket_update"         # Wake on ticket state change
    BROADCAST = "broadcast"                 # Wake all subscribers
    CHANNEL = "channel"                     # Per-channel pub/sub
    ENV_CHANGE = "env_change"               # Environment/context change


@dataclass
class NotificationParams:
    """Event-specific parameters for a notification."""
    # Timer params
    seconds: Optional[int] = None
    repeat: bool = False  # For recurring timers

    # Subsession/Agent params
    subsession_id: Optional[str] = None
    agent_id: Optional[str] = None

    # Ticket params (for VibeKanban integration)
    ticket_id: Optional[int] = None
    project_id: Optional[int] = None
    states: Optional[List[str]] = None  # e.g., ["done", "in_progress"]

    # Channel params
    channel: Optional[str] = None

    # Generic payload for extensibility
    data: Optional[Dict[str, Any]] = None


@dataclass
class Notification:
    """A notification configuration."""
    notification_type: NotificationType
    params: NotificationParams
    wake_prompt: str  # Prompt injected when notification fires

    # Auto-generated fields
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: datetime = field(default_factory=datetime.utcnow)

    # Session targeting
    source_session_id: Optional[str] = None  # Who set this notification
    target_session_id: Optional[str] = None  # Who receives it (None = self)

    # Status tracking
    fired: bool = False
    fired_at: Optional[datetime] = None
    cancelled: bool = False


@dataclass
class NotificationResult:
    """Result from a notification operation."""
    notification_id: str
    status: str  # "set", "cancelled", "fired", "error", "not_found"
    message: Optional[str] = None

    @classmethod
    def success(cls, notification_id: str, status: str = "set", message: str = None):
        return cls(notification_id, status, message)

    @classmethod
    def error(cls, notification_id: str, message: str):
        return cls(notification_id, "error", message)

    @classmethod
    def not_found(cls, notification_id: str):
        return cls(notification_id, "not_found", f"Notification {notification_id} not found")


@dataclass
class ChannelMessage:
    """A message broadcast to a channel."""
    channel: str
    message: str
    data: Optional[Dict[str, Any]] = None
    sender_session_id: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
