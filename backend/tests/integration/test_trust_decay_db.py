"""Trust decay against a real trust row (ADR-0030).

The unit tests cover the curve. These check the wiring: that a category holding real
audited evidence in Postgres actually loses automation when it goes quiet, and that the
dashboard says so rather than showing a score with no hint it is stale.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.config import get_settings
from app.services import reporting

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
    with _scratch_database("quorum_decay_test") as url:
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


def earn_trust(session: Session, days_ago: float) -> None:
    """Put a category in the state the Phase 6 demo leaves it, audited `days_ago`."""
    session.execute(
        text(
            "UPDATE trust_scores SET score = 0.9988, sample_size = 30, correct_count = 30, "
            "last_evaluated_at = :at WHERE category_code = :c"
        ),
        {"at": datetime.now(UTC) - timedelta(days=days_ago), "c": CATEGORY},
    )
    session.commit()


def view_for(session: Session, category: str = CATEGORY):
    views = reporting.category_trust_views(session, only_with_findings=False)
    return next(v for v in views if v.code == category)


def test_a_recently_audited_category_keeps_automation(session):
    earn_trust(session, days_ago=3)
    view = view_for(session)
    assert view.gate_decision == "AUTO_APPLY"
    assert view.is_decaying is False


def test_a_silent_category_loses_automation(session):
    """The exit criterion, through the real read path against a real row."""
    earn_trust(session, days_ago=30)
    view = view_for(session)

    assert view.gate_decision == "HUMAN_REVIEW"
    assert view.is_decaying is True
    assert view.score < view.base_score, "the effective score is discounted"
    assert view.base_score == pytest.approx(0.9988), "the earned score is unchanged"


def test_the_stored_score_is_never_mutated_by_decay(session):
    """Decay is computed at read time, so the evidence in Postgres stays intact.

    A background job that wrote decayed values would destroy the record of what the
    audits actually found, and could not be undone by a later audit arriving.
    """
    earn_trust(session, days_ago=60)
    view_for(session)
    view_for(session)

    stored = session.execute(
        text("SELECT score, sample_size FROM trust_scores WHERE category_code = :c"),
        {"c": CATEGORY},
    ).one()
    assert float(stored.score) == pytest.approx(0.9988)
    assert stored.sample_size == 30


def test_a_fresh_audit_restores_automation_immediately(session):
    """Decay is a function of the timestamp, so updating it is the whole recovery."""
    earn_trust(session, days_ago=60)
    assert view_for(session).gate_decision == "HUMAN_REVIEW"

    earn_trust(session, days_ago=0)
    restored = view_for(session)
    assert restored.gate_decision == "AUTO_APPLY"
    assert restored.is_decaying is False


def test_a_cold_start_category_is_not_reported_as_decaying(session):
    """Nothing has been audited, so there is nothing to have gone stale."""
    view = view_for(session, "MISSING_IN_BANK")
    assert view.sample_size == 0
    assert view.is_decaying is False
    assert view.days_since_audit is None


def test_the_dashboard_reports_which_categories_are_decaying(session):
    earn_trust(session, days_ago=30)
    summary = reporting.dashboard_summary(session)

    entry = next(c for c in summary["categories"] if c["code"] == CATEGORY)
    assert entry["is_decaying"] is True
    assert entry["days_since_audit"] >= 29
    assert entry["base_score"] > entry["score"]

    assert any("gone quiet" in caveat for caveat in summary["caveats"])


def test_the_dashboard_says_nothing_about_decay_when_nothing_is_stale(session):
    earn_trust(session, days_ago=1)
    summary = reporting.dashboard_summary(session)
    assert not any("gone quiet" in caveat for caveat in summary["caveats"])
