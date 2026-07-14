from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
CORRELATION_ID_HEADER = "X-Correlation-ID"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

request_id_context: ContextVar[str] = ContextVar("request_id", default="")
correlation_id_context: ContextVar[str] = ContextVar("correlation_id", default="")


def _header_id(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if _SAFE_ID.fullmatch(value) else None


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach safe request/correlation IDs to state, logs, and responses."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = _header_id(request.headers.get(REQUEST_ID_HEADER)) or str(
            uuid.uuid4()
        )
        correlation_id = (
            _header_id(request.headers.get(CORRELATION_ID_HEADER)) or request_id
        )
        request.state.request_id = request_id
        request.state.correlation_id = correlation_id
        request_token = request_id_context.set(request_id)
        correlation_token = correlation_id_context.set(correlation_id)
        try:
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            return response
        finally:
            request_id_context.reset(request_token)
            correlation_id_context.reset(correlation_token)
