class ServiceError(Exception):
    """Base class for all service-related exceptions."""


class InsufficientBalanceError(ServiceError):
    """Raised when user's balance is insufficient for an operation."""


class UserNotFoundError(ServiceError):
    """Raised when a requested user cannot be found."""


class ServerNotFoundError(ServiceError):
    """Raised when a requested server cannot be found."""


class ConfigNotFoundError(ServiceError):
    """Raised when a requested configuration cannot be found."""


class APIConnectionError(ServiceError):
    """Raised when there's an error connecting to the VPN API."""


class InvalidOperationError(ServiceError):
    """Raised when an operation is invalid in the current context."""
