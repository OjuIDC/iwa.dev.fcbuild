"""FC27 Correlation ID tracking and logging integration.

Implements Spec §22.3 and Amendment C-5:
- correlation_id_var: ContextVar[UUID | None]
- CorrelationIdFilter: adds correlation_id to logging.LogRecord
- CorrelationMiddleware: FastAPI/Starlette middleware extracting or generating UUIDs,
  rejecting malformed headers with 422, echoing response headers.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from uuid import UUID, uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

correlation_id_var: ContextVar[UUID | None] = ContextVar("correlation_id", default=None)


class CorrelationIdFilter(logging.Filter):
    """Logging filter attaching correlation_id to all log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        cid = correlation_id_var.get()
        record.correlation_id = str(cid) if cid is not None else None
        return True


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Middleware enforcing canonical UUID correlation headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        raw_header = request.headers.get("x-correlation-id")
        if raw_header is not None:
            stripped = raw_header.strip()
            if len(stripped) != 36:
                return JSONResponse(
                    status_code=422,
                    content={"detail": "invalid correlation id"},
                )
            try:
                cid = UUID(stripped)
                if str(cid).lower() != stripped.lower():
                    return JSONResponse(
                        status_code=422,
                        content={"detail": "invalid correlation id"},
                    )
            except (ValueError, TypeError, AttributeError):
                return JSONResponse(
                    status_code=422,
                    content={"detail": "invalid correlation id"},
                )
        else:
            cid = uuid4()

        token = correlation_id_var.set(cid)
        request.state.correlation_id = cid
        try:
            response = await call_next(request)
            response.headers["X-Correlation-Id"] = str(cid)
            return response
        finally:
            correlation_id_var.reset(token)
