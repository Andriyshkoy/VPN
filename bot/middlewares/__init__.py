"""Cross-cutting Telegram update boundaries."""

from .invite_access import InviteOnlyAccessMiddleware

__all__ = ["InviteOnlyAccessMiddleware"]
