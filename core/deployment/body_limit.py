"""Small ASGI request-body limiter that also covers chunked bodies."""

from __future__ import annotations

from starlette.responses import JSONResponse


class RequestBodyLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > self.max_bytes:
                    return await JSONResponse({"detail": "request body exceeds policy limit"}, status_code=413)(scope, receive, send)
            except ValueError:
                return await JSONResponse({"detail": "invalid content-length"}, status_code=400)(scope, receive, send)
        messages, received = [], 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            received += len(message.get("body", b""))
            if received > self.max_bytes:
                return await JSONResponse({"detail": "request body exceeds policy limit"}, status_code=413)(scope, receive, send)
            if not message.get("more_body", False):
                break

        async def replay_receive():
            if messages:
                return messages.pop(0)
            return {"type": "http.request", "body": b"", "more_body": False}
        return await self.app(scope, replay_receive, send)
