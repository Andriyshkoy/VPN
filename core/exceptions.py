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


class APIGatewayError(ServiceError):
    """Base class for failures while talking to a VPN Manager API."""


class APIConfigurationError(APIGatewayError, ValueError):
    """Raised when a VPN Manager gateway is configured incorrectly."""


class APIConnectionError(APIGatewayError):
    """Backward-compatible base class for VPN Manager request failures."""

    def __init__(self, message: str, *, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = attempts


class APITLSConfigurationError(APIConnectionError):
    """Retryable Manager TLS material/configuration failure.

    Certificate mounts and rotations are external runtime state. Treating a
    temporarily missing or unreadable file as a definitive Manager rejection
    would incorrectly terminate provisioning and refund its reservation.
    """


class APITransportError(APIConnectionError):
    """Raised after a transport or timeout failure cannot be retried."""


class APIRetryableResponseError(APIConnectionError):
    """Raised after retryable VPN Manager responses are exhausted."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        attempts: int = 1,
        retryable: bool = False,
    ) -> None:
        super().__init__(message, attempts=attempts)
        self.status_code = status_code
        self.retryable = retryable


class APIRateLimitError(APIRetryableResponseError):
    """Raised when VPN Manager keeps rate-limiting a request."""


class APIServerError(APIRetryableResponseError):
    """Raised when VPN Manager keeps returning a server-side failure."""


class APIRequestRejectedError(APIGatewayError):
    """Raised when VPN Manager definitively rejects a request."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        attempts: int = 1,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts
        self.retryable = retryable


class APIHTTPError(APIRequestRejectedError):
    """Raised for an otherwise unclassified, definitive HTTP rejection."""


class APIAuthenticationError(APIRequestRejectedError):
    """Raised when VPN Manager rejects the configured API key."""


class APINotFoundError(APIRequestRejectedError):
    """Raised when VPN Manager cannot find the requested resource."""


class APIConflictError(APIRequestRejectedError):
    """Raised when a VPN Manager operation conflicts with current state."""


class APIProtocolError(APIConnectionError):
    """Raised when VPN Manager returns a malformed success payload."""


class InvalidOperationError(ServiceError):
    """Raised when an operation is invalid in the current context."""
