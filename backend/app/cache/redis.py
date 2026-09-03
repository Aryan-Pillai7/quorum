"""Redis client. Cache only -- see ADR-0009.

Postgres is the source of truth for everything Quorum caches. A Redis outage must make
the system slower, never wrong and never more permissive. Every helper here therefore
swallows connection errors and reports a miss, and callers fall back to Postgres.
"""

from __future__ import annotations

import logging

import redis
from redis.exceptions import RedisError

from app.config import get_settings

logger = logging.getLogger(__name__)

_client: redis.Redis | None = None


def get_client() -> redis.Redis:
    """Process-wide Redis client. Lazily created; never raises on construction."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            # Short timeouts on purpose: a hung cache must not become a hung request.
            socket_timeout=settings.redis_socket_timeout_seconds,
            socket_connect_timeout=settings.redis_socket_timeout_seconds,
        )
    return _client


def ping() -> bool:
    """True if Redis answered. Used by the readiness probe; never raises."""
    try:
        return bool(get_client().ping())
    except RedisError as exc:
        logger.warning("redis ping failed", extra={"error": str(exc)})
        return False


def cache_get(key: str) -> str | None:
    """Read a cached value. A Redis failure is reported as a miss, not an error."""
    try:
        return get_client().get(key)
    except RedisError as exc:
        logger.warning("redis get failed, falling back to source of truth",
                       extra={"cache_key": key, "error": str(exc)})
        return None


def cache_set(key: str, value: str, ttl_seconds: int) -> bool:
    """Write a cached value. Returns whether the write landed; failure is not fatal."""
    try:
        get_client().set(key, value, ex=ttl_seconds)
        return True
    except RedisError as exc:
        logger.warning("redis set failed, continuing uncached",
                       extra={"cache_key": key, "error": str(exc)})
        return False


def cache_delete(key: str) -> bool:
    """Invalidate a key.

    Phase 4 calls this after a trust-score update. Order matters there: write Postgres
    first, delete the key second. A stale *permissive* trust score is the one genuinely
    dangerous failure mode in this system.
    """
    try:
        get_client().delete(key)
        return True
    except RedisError as exc:
        logger.warning("redis delete failed; cached value may be stale",
                       extra={"cache_key": key, "error": str(exc)})
        return False


def reset_client() -> None:
    """Drop the cached client. For tests and for reconnect-after-config-change."""
    global _client
    _client = None
