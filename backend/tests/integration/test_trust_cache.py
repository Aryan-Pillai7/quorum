"""Trust score cache invalidation under failure (ADR-0009, ADR-0023).

The property under test is narrow and load-bearing: **a stale cache can never make Quorum
more permissive.** These tests break Redis in the ways it actually breaks -- fully down,
and the nastier partial case where the delete fails but reads still succeed -- and assert
the guarantee holds in both.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest
from alembic.config import Config
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.cache import redis as cache
from app.config import get_settings
from app.services import trust_store
from app.services.trust import decide_gate
from app.services.trust_store import (
    cache_key,
    read_trust_authoritative,
    read_trust_cached,
    record_outcome,
)

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
CATEGORY = "TIMING_DIFFERENCE"


@contextmanager
def _scratch_database(name: str) -> Iterator[str]:
    base = make_url(get_settings().database_url)
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        connection = admin.connect()
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable: {str(exc).splitlines()[0]}")
    with connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as cleanup:
            cleanup.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def session() -> Iterator[Session]:
    with _scratch_database("quorum_trust_cache_test") as url:
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        engine = create_engine(url)
        db = sessionmaker(bind=engine)()
        try:
            yield db
        finally:
            db.close()
            engine.dispose()


@pytest.fixture(autouse=True)
def clean_cache_key() -> Iterator[None]:
    """Leave no key behind: these tests share one Redis with everything else."""
    with suppress(Exception):
        cache.cache_delete(cache_key(CATEGORY))
    yield
    with suppress(Exception):
        cache.cache_delete(cache_key(CATEGORY))


class _DeadCache:
    """Redis that refuses every operation, as it does when the server is gone."""

    @staticmethod
    def cache_get(key: str) -> None:
        raise RedisConnectionError("connection refused")

    @staticmethod
    def cache_set(key: str, value: str, ttl_seconds: int) -> bool:
        raise RedisConnectionError("connection refused")

    @staticmethod
    def cache_delete(key: str) -> bool:
        raise RedisConnectionError("connection refused")


# --- the happy path -------------------------------------------------------------------


def test_an_update_invalidates_the_cached_score(session):
    read_trust_cached(session, CATEGORY)  # populate
    assert cache.cache_get(cache_key(CATEGORY)) is not None

    result = record_outcome(session, category_code=CATEGORY, was_correct=True)

    assert result.cache_invalidated is True
    assert result.cache_error is None
    assert cache.cache_get(cache_key(CATEGORY)) is None


def test_the_next_read_after_an_update_sees_the_new_score(session):
    read_trust_cached(session, CATEGORY)
    record_outcome(session, category_code=CATEGORY, was_correct=True)

    refreshed = read_trust_cached(session, CATEGORY)
    assert refreshed["sample_size"] == 1
    assert refreshed["correct_count"] == 1
    assert refreshed["score"] == 1.0


def test_postgres_is_written_before_the_cache_is_touched(monkeypatch, session):
    """If invalidation runs first, a concurrent read can re-poison the key.

    Asserted by observing what Postgres already holds at the moment invalidation runs.
    """
    observed: dict[str, int] = {}
    real_delete = cache.cache_delete

    def observing_delete(key: str) -> bool:
        observed["sample_size"] = session.execute(
            text("SELECT sample_size FROM trust_scores WHERE category_code = :c"),
            {"c": CATEGORY},
        ).scalar_one()
        return real_delete(key)

    monkeypatch.setattr(trust_store.cache, "cache_delete", observing_delete)
    record_outcome(session, category_code=CATEGORY, was_correct=True)

    assert observed["sample_size"] == 1, "the write must be committed before invalidation"


# --- Redis fully down -----------------------------------------------------------------


def test_an_update_succeeds_when_redis_is_unreachable(monkeypatch, session):
    """A cache outage must not cost a durable, correct Postgres write."""
    monkeypatch.setattr(trust_store, "cache", _DeadCache)

    result = record_outcome(session, category_code=CATEGORY, was_correct=True)

    assert result.cache_invalidated is False
    assert result.cache_error
    assert result.sample_size == 1

    row = read_trust_authoritative(session, CATEGORY)
    assert row.sample_size == 1
    assert float(row.score) == 1.0


def test_reads_fall_back_to_postgres_when_redis_is_unreachable(monkeypatch, session):
    record_outcome(session, category_code=CATEGORY, was_correct=True)
    monkeypatch.setattr(trust_store, "cache", _DeadCache)

    value = read_trust_cached(session, CATEGORY)

    assert value["sample_size"] == 1, "slower, but correct"
    assert value["score"] == 1.0


# --- the dangerous partial failure ----------------------------------------------------


def test_a_stale_cache_entry_cannot_affect_a_gate_decision(monkeypatch, session):
    """The load-bearing test for ADR-0023.

    The nastiest Redis failure is not an outage: it is a delete that fails while reads
    keep working. The cache then serves a stale value that looks perfectly healthy. Here
    the stale value claims a mature, high score -- exactly the shape that would wrongly
    permit automation -- and the gate must not see it.
    """
    read_trust_cached(session, CATEGORY)

    # Invalidation fails silently while reads keep working.
    monkeypatch.setattr(trust_store.cache, "cache_delete", lambda key: False)
    result = record_outcome(session, category_code=CATEGORY, was_correct=True)
    assert result.cache_invalidated is False

    # Poison the surviving key with a score that would unlock AUTO_APPLY.
    cache.cache_set(
        cache_key(CATEGORY),
        json.dumps(
            {
                "category_code": CATEGORY,
                "score": 0.99,
                "sample_size": 5000,
                "correct_count": 4950,
                "min_sample_size": 30,
                "auto_apply_threshold": 0.85,
                "review_threshold": 0.50,
            }
        ),
        300,
    )

    # The cached read is indeed poisoned -- this is the failure being guarded against.
    assert read_trust_cached(session, CATEGORY)["sample_size"] == 5000

    # Gating reads Postgres, so it sees one observation and refuses to automate.
    row = read_trust_authoritative(session, CATEGORY)
    assert row.sample_size == 1

    evaluation = decide_gate(
        score=row.score,
        sample_size=row.sample_size,
        correct_count=row.correct_count,
        auto_apply_threshold=row.auto_apply_threshold,
        review_threshold=row.review_threshold,
        min_sample_size=row.min_sample_size,
        category_auto_resolvable=True,
    )
    assert evaluation.decision.value == "HUMAN_REVIEW"
    assert evaluation.is_cold_start is True


def test_a_failed_invalidation_is_reported_not_swallowed(monkeypatch, session):
    """An operator has to be able to see that a key may be stale."""
    monkeypatch.setattr(trust_store.cache, "cache_delete", lambda key: False)
    result = record_outcome(session, category_code=CATEGORY, was_correct=False)

    assert result.cache_invalidated is False
    assert result.cache_error == "redis delete did not complete"
    assert result.to_dict()["cache_error"]


def test_an_unreadable_cached_value_is_treated_as_a_miss(session):
    cache.cache_set(cache_key(CATEGORY), "{not valid json", 300)
    value = read_trust_cached(session, CATEGORY)
    assert value["category_code"] == CATEGORY


# --- the invariant that makes the guarantee hold --------------------------------------


def test_no_gating_path_reads_the_trust_cache():
    """ADR-0023, asserted mechanically rather than by convention.

    If a future change routes gating through the cache, this fails.
    """
    import ast

    for module in ("app/services/reporting.py", "app/services/agent/explain.py"):
        source = (BACKEND_ROOT / module).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)

        assert "app.cache.redis" not in imported, module
        assert "app.services.trust_store" not in imported, (
            f"{module} decides gating and must read Postgres directly"
        )
