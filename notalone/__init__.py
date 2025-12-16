"""
Agent Notification Protocol - Backend-agnostic notification system for AI agents.
"""
from .types import (
    NotificationType,
    NotificationParams,
    Notification,
    NotificationResult,
    ChannelMessage,
)
from .backend import NotificationBackend, NotificationCallback
from .hub import NotificationHub

__version__ = "0.1.0"

__all__ = [
    "NotificationType",
    "NotificationParams",
    "Notification",
    "NotificationResult",
    "ChannelMessage",
    "NotificationBackend",
    "NotificationCallback",
    "NotificationHub",
]
