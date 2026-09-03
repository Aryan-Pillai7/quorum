"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1.router import root_router, v1_router
from app.config import get_settings, validate_settings
from app.core.errors import QuorumError
from app.core.logging import configure_logging, get_request_id, set_request_id

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    # Fail at boot on bad configuration, with a message naming the problem, rather than
    # at request time with a confusing one. This is the call that makes the guard real.
    validate_settings(settings)

    app = FastAPI(
        title="Quorum",
        version="0.1.0",
        description=(
            "AI-augmented three-way payment reconciliation: processor settlement report, "
            "bank statement, and internal ledger. Matching is deterministic and auditable; "
            "the AI layer explains and proposes, and every proposal passes a trust gate."
        ),
    )

    _install_middleware(app)
    _install_error_handlers(app)

    app.include_router(root_router)
    app.include_router(v1_router)

    logger.info(
        "quorum started",
        extra={
            "environment": settings.environment,
            # Honest about capability: the agent layer is off without a key, and says so
            # at boot rather than failing mysteriously on first use.
            "agent_enabled": settings.agent_enabled,
        },
    )
    return app


def _install_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[object]]
    ):
        # Honour an inbound request id so a trace survives across services; generate one
        # otherwise. Every log line in this request carries it.
        request_id = set_request_id(request.headers.get(REQUEST_ID_HEADER))
        started = time.perf_counter()

        response = await call_next(request)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(QuorumError)
    async def handle_quorum_error(_request: Request, exc: QuorumError) -> JSONResponse:
        # A deliberate, typed failure: log at warning and return its stable error code.
        logger.warning(
            "handled error", extra={"error_code": exc.code, "error_message": exc.message}
        )
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.to_payload(), "request_id": get_request_id()},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        # Anything reaching here is a bug, and is reported as one rather than being
        # dressed up as a handled condition. The message is not echoed to the client;
        # the request id is, so the log line can be found.
        logger.exception("unhandled error", extra={"error_type": type(exc).__name__})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected error occurred. Quote the request_id when reporting.",
                    "details": {},
                },
                "request_id": get_request_id(),
            },
        )


app = create_app()
