from enum import Enum


class TelegramUpdateStatus(str, Enum):
    """Lifecycle states for an incoming Telegram update."""

    PENDING = "pending"
    PROCESSING = "processing"
    FAILED = "failed"
    PROCESSED = "processed"
    DEAD = "dead"
