"""Trust score reads, updates, and cache invalidation (ADR-0009, ADR-0023).

Trust scores decide whether the AI layer may act without a human, which makes a stale
score the one genuinely dangerous piece of stale data in this system. Everything here is
arranged around one property:

    **A stale cache can never make Quorum more permissive.**

Two mechanisms, because one is not enough:

1. **Ordering.** An update writes Postgres and commits, *then* deletes the Redis key.
   Never the reverse. Deleting first opens a window where a concurrent read repopulates
   the cache from the pre-update row, re-poisoning the key with the value the update was
   trying to remove.

2. **Gating never reads the cache at all.** This is the part that actually makes the
   guarantee hold. ADR-0009 argued that a failed invalidation degrades to "slower and
   correct" because reads fall back to Postgres -- but that reasoning only covers Redis
   being *down*. A partial failure, where the delete fails and reads still succeed, is
   not a miss: the cache would happily serve the stale permissive score, fast and wrong.
   So the authoritative read used for gating bypasses the cache entirely, and the cached
   read exists only for display. See ADR-0023.

A failed invalidation is therefore a display-freshness problem bounded by TTL, never a
safety problem.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cache import redis as cache
from app.config import Settings, get_settings
from app.models import ActorType, TrustScore
from app.services import audit

logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "quorum:trust:"


def cache_key(category_code: str) -> str:
    return f"{CACHE_KEY_PREFIX}{category_code}"


@dataclass(frozen=True)
class OutcomeResult:
    """What an outcome recording did, including the parts that failed."""

    category_code: str
    sample_size: int
    correct_count: int
    score: float
    cache_invalidated: bool
    cache_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category_code": self.category_code,
            "sample_size": self.sample_size,
            "correct_count": self.correct_count,
            "score": self.score,
            "cache_invalidated": self.cache_invalidated,
            "cache_error": self.cache_error,
        }


def read_trust_authoritative(session: Session, category_code: str) -> TrustScore | None:
    """The trust score, straight from Postgres. Never cached.

    Every path that can *permit* automation must use this. Gating decisions are exactly
    the thing that must not run on possibly-stale data, and no cache read is cheap enough
    to be worth being wrong about (ADR-0023).
    """
    return session.execute(
        select(TrustScore).where(TrustScore.category_code == category_code)
    ).scalar_one_or_none()


def read_trust_cached(session: Session, category_code: str) -> dict[str, Any] | None:
    """Read-through cached score, for display only.

    Falls back to Postgres on a miss, on a Redis error, and on an unreadable cached
    value. A cache that cannot be trusted to parse is a cache miss, not a crash.
    """
    settings = get_settings()
    key = cache_key(category_code)

    # Defence in depth. app/cache/redis.py already converts RedisError into a miss, but
    # relying on that alone makes the fallback guarantee exactly one layer thick: any
    # error it does not anticipate would propagate and break a read instead of degrading
    # it. On a path this close to the trust layer, a broad catch is the correct trade.
    raw: str | None = None
    try:
        raw = cache.cache_get(key)
    except Exception as exc:  # noqa: BLE001 - a cache must never break a read
        logger.warning(
            "trust score cache read failed; falling back to postgres",
            extra={"cache_key": key, "error": str(exc)[:200]},
        )

    if raw is not None:
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            logger.warning(
                "discarding unreadable cached trust score",
                extra={"cache_key": key},
            )

    row = read_trust_authoritative(session, category_code)
    if row is None:
        return None

    value = {
        "category_code": row.category_code,
        "score": float(row.score),
        "sample_size": row.sample_size,
        "correct_count": row.correct_count,
        "min_sample_size": row.min_sample_size,
        "auto_apply_threshold": float(row.auto_apply_threshold),
        "review_threshold": float(row.review_threshold),
    }
    try:
        cache.cache_set(key, json.dumps(value), settings.trust_cache_ttl_seconds)
    except Exception as exc:  # noqa: BLE001 - failing to cache is not failing to read
        logger.warning(
            "trust score cache write failed; the value is still correct",
            extra={"cache_key": key, "error": str(exc)[:200]},
        )
    return value


def invalidate(category_code: str) -> tuple[bool, str | None]:
    """Drop the cached score. Returns (succeeded, error).

    Never raises. A failed invalidation must not roll back a committed Postgres write --
    the score itself is correct and durable, and losing that to a cache problem would be
    strictly worse than a stale display value.
    """
    key = cache_key(category_code)
    try:
        deleted = cache.cache_delete(key)
    except Exception as exc:  # noqa: BLE001 - invalidation must never propagate
        logger.error(
            "trust score cache invalidation FAILED; cached value may be stale until it "
            "expires. Gating is unaffected: it reads Postgres directly (ADR-0023).",
            extra={"cache_key": key, "error": str(exc)[:200]},
        )
        return False, str(exc)[:200]

    if not deleted:
        logger.error(
            "trust score cache invalidation FAILED; cached value may be stale until it "
            "expires. Gating is unaffected: it reads Postgres directly (ADR-0023).",
            extra={"cache_key": key},
        )
        return False, "redis delete did not complete"

    logger.info("trust score cache invalidated", extra={"cache_key": key})
    return True, None


def record_outcome(
    session: Session,
    *,
    category_code: str,
    was_correct: bool,
    actor_id: str | None = None,
    settings: Settings | None = None,
) -> OutcomeResult:
    """Record one human verdict on an agent proposal and update the category's score.

    Order is the whole point: Postgres is written and committed first, and only then is
    the cache invalidated. Inverting it opens a window in which a concurrent read
    repopulates the key from the pre-update row.
    """
    settings = settings or get_settings()

    row = read_trust_authoritative(session, category_code)
    if row is None:
        raise ValueError(f"no trust score row for category {category_code!r}")

    row.sample_size += 1
    if was_correct:
        row.correct_count += 1
    # Recomputed from the counts rather than nudged, so the stored score can never drift
    # away from the observations it claims to summarize.
    row.score = round(row.correct_count / row.sample_size, 4) if row.sample_size else 0
    row.last_evaluated_at = datetime.now(UTC)

    audit.record(
        session,
        action="trust.outcome_recorded",
        entity_type="trust_score",
        entity_id=category_code,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        payload={
            "was_correct": was_correct,
            "sample_size": row.sample_size,
            "correct_count": row.correct_count,
            "score": float(row.score),
        },
    )

    # Postgres first, and committed, before the cache is touched.
    session.commit()

    invalidated, error = invalidate(category_code)

    return OutcomeResult(
        category_code=category_code,
        sample_size=row.sample_size,
        correct_count=row.correct_count,
        score=float(row.score),
        cache_invalidated=invalidated,
        cache_error=error,
    )
