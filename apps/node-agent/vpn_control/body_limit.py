"""Bound request bodies before framework parsing, including chunked requests."""
from uuid import uuid4
from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    def __init__(self, app, limit=16384):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            block = message.get("body", b"")
            size += len(block)
            if size > self.limit:
                request_id = str(uuid4())
                response = JSONResponse({"error": {"code": "request_too_large", "request_id": request_id}}, status_code=413,
                    headers={"X-Request-ID": request_id, "Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"})
                return await response(scope, receive, send)
            chunks.append(block)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)
