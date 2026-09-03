"""Health and readiness checks.

The distinction matters operationally:

- liveness  = the process is running and can serve. No dependencies consulted.
- readiness = the process can do useful work right now.

Postgres is required, so it failing means not ready. Redis is a cache (ADR-0009), so it
failing degrades performance but not correctness, and must not fail readiness -- a
readiness probe that fails on a cache outage turns a slowdown into an outage.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.cache import redis as cache
from app.db.session import engine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ComponentHealth:
    name: str
    status: str  # "ok" | "down" | "degraded"
    latency_ms: float
    detail: str | None = None


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    components: list[ComponentHealth]

    def to_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "components": [asdict(c) for c in self.components]}


def _check_postgres() -> ComponentHealth:
    started = time.perf_counter()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return ComponentHealth("postgres", "ok", _elapsed_ms(started))
    except SQLAlchemyError as exc:
        logger.error("postgres readiness check failed", extra={"error": str(exc)})
        return ComponentHealth(
            "postgres", "down", _elapsed_ms(started), detail=_first_line(str(exc))
        )


def _check_redis() -> ComponentHealth:
    started = time.perf_counter()
    if cache.ping():
        return ComponentHealth("redis", "ok", _elapsed_ms(started))
    # "degraded", not "down": the app is still correct without it, only slower.
    return ComponentHealth(
        "redis",
        "degraded",
        _elapsed_ms(started),
        detail="cache unavailable; trust score reads fall back to postgres",
    )


def check_readiness() -> ReadinessReport:
    """Check every backing service. Only Postgres can make the service not ready."""
    postgres = _check_postgres()
    redis_health = _check_redis()
    return ReadinessReport(
        ready=postgres.status == "ok",
        components=[postgres, redis_health],
    )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _first_line(message: str) -> str:
    """Keep probe output to one useful line rather than a full driver traceback."""
    return message.strip().splitlines()[0][:200]
