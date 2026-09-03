"""Migrations applied to a real Postgres, then checked against the ORM.

Marked `integration` and excluded from the default run. Run it with:

    docker compose up -d db
    cd backend && pytest -m integration

The risk this covers is the specific cost of hand-authored migrations (ADR-0011): the
migration and the models drift, nothing notices, and a constraint the code relies on
turns out not to exist in the database. Only a real Postgres can settle that -- sqlite
has neither JSONB nor `num_nonnulls`.

Each test runs against a scratch database that is created and dropped here, so it never
touches development data.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, make_url, text
from sqlalchemy.exc import IntegrityError, OperationalError

import app.models  # noqa: F401  (populates Base.metadata)
from alembic import command
from app.config import get_settings
from app.db.base import Base

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _admin_engine():
    """Engine bound to the maintenance database, used to CREATE/DROP the scratch one."""
    url = make_url(get_settings().database_url).set(database="postgres")
    return create_engine(url, isolation_level="AUTOCOMMIT")


@contextmanager
def _scratch_database(name: str) -> Iterator[str]:
    """Create a throwaway database, yield its URL, and drop it afterwards."""
    admin = _admin_engine()
    try:
        connection = admin.connect()
    except OperationalError as exc:
        pytest.skip(
            f"Postgres not reachable at {admin.url.render_as_string(hide_password=True)}: "
            f"{str(exc).splitlines()[0]}. Start it with `docker compose up -d db`."
        )

    with connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        # render_as_string(hide_password=False), not str(): SQLAlchemy's URL.__str__
        # redacts the password to "***", which would be passed through as a literal.
        yield make_url(get_settings().database_url).set(database=name).render_as_string(
            hide_password=False
        )
    finally:
        with admin.connect() as cleanup:
            cleanup.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _alembic_config(url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(scope="module")
def migrated_url() -> Iterator[str]:
    with _scratch_database("quorum_migration_test") as url:
        command.upgrade(_alembic_config(url), "head")
        yield url


@pytest.fixture(scope="module")
def migrated_engine(migrated_url: str):
    engine = create_engine(migrated_url)
    yield engine
    engine.dispose()


def test_migration_creates_every_orm_table(migrated_engine):
    actual = set(inspect(migrated_engine).get_table_names())
    assert set(Base.metadata.tables) <= actual
    assert "alembic_version" in actual


def test_migrated_columns_match_the_orm(migrated_engine):
    """The drift check. Hand-written migration vs models, column by column."""
    inspector = inspect(migrated_engine)
    drift: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        migrated = {c["name"] for c in inspector.get_columns(table_name)}
        expected = {c.name for c in table.columns}
        for missing in sorted(expected - migrated):
            drift.append(f"{table_name}.{missing} in models but not in migration")
        for extra in sorted(migrated - expected):
            drift.append(f"{table_name}.{extra} in migration but not in models")
    assert not drift, "model/migration drift:\n  " + "\n  ".join(drift)


def test_migrated_check_constraints_match_the_orm(migrated_engine):
    """Constraints are the part of the schema the application logic actually relies on."""
    inspector = inspect(migrated_engine)
    for table_name, table in Base.metadata.tables.items():
        migrated = {c["name"] for c in inspector.get_check_constraints(table_name)}
        expected = {
            c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"
        }
        assert expected <= migrated, (
            f"{table_name}: check constraints in models but missing from the database: "
            f"{sorted(expected - migrated)}"
        )


def test_seed_data_covers_every_category_with_a_trust_row(migrated_engine):
    with migrated_engine.connect() as conn:
        categories = conn.execute(
            text("SELECT COUNT(*) FROM discrepancy_categories")
        ).scalar_one()
        trust_rows = conn.execute(text("SELECT COUNT(*) FROM trust_scores")).scalar_one()
        orphans = conn.execute(
            text(
                "SELECT COUNT(*) FROM discrepancy_categories c "
                "LEFT JOIN trust_scores t ON t.category_code = c.code "
                "WHERE t.category_code IS NULL"
            )
        ).scalar_one()

    assert categories == 14, f"expected the seeded taxonomy of 14 categories, found {categories}"
    assert trust_rows == categories
    assert orphans == 0, "every category needs a trust row, or its gate decision is undefined"


def test_every_seeded_trust_score_starts_cold(migrated_engine):
    """Honest telemetry: nothing has been observed yet, so nothing claims to be trusted."""
    with migrated_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT COUNT(*) FROM trust_scores WHERE sample_size > 0 OR score > 0")
        ).scalar_one()
    assert rows == 0, "seeded trust scores must start at zero observations"


def test_duplicate_source_and_external_id_is_rejected(migrated_engine):
    """The constraint that makes re-ingesting a settlement file idempotent."""
    row = {
        "id": uuid.uuid4(),
        "source": "PSP_SETTLEMENT",
        "external_id": "pay_DUPLICATE_001",
        "amount_minor": 150000,
        "currency": "INR",
        "direction": "CREDIT",
        "occurred_at": datetime(2026, 3, 1, 10, 30, tzinfo=UTC),
    }
    insert = text(
        "INSERT INTO transactions (id, source, external_id, amount_minor, currency, "
        "direction, occurred_at) VALUES (:id, :source, :external_id, :amount_minor, "
        ":currency, :direction, :occurred_at)"
    )
    with migrated_engine.begin() as conn:
        conn.execute(insert, row)

    with (
        pytest.raises(IntegrityError, match="uq_transactions_source_external_id"),
        migrated_engine.begin() as conn,
    ):
        conn.execute(insert, {**row, "id": uuid.uuid4()})


def test_match_record_with_no_legs_is_rejected(migrated_engine):
    """num_nonnulls(...) >= 1. A match linking nothing is meaningless."""
    with (
        pytest.raises(IntegrityError, match="ck_match_records_at_least_one_leg"),
        migrated_engine.begin() as conn,
    ):
        conn.execute(
            text(
                "INSERT INTO match_records (id, match_key, status, strategy, confidence) "
                "VALUES (:id, 'orphan', 'BROKEN', 'MANUAL', 1.0)"
            ),
            {"id": uuid.uuid4()},
        )


def test_trust_score_thresholds_are_enforced_by_the_database(migrated_engine):
    """A script bypassing the app must still not be able to create an incoherent gate."""
    with (
        pytest.raises(IntegrityError, match="ck_trust_scores_review_below_auto_apply"),
        migrated_engine.begin() as conn,
    ):
        conn.execute(
            text(
                "UPDATE trust_scores SET review_threshold = 0.99, "
                "auto_apply_threshold = 0.50 WHERE category_code = 'ROUNDING_DIFFERENCE'"
            )
        )


def test_downgrade_removes_every_table():
    """Reversibility, on its own database so ordering cannot affect other tests."""
    with _scratch_database("quorum_downgrade_test") as url:
        config = _alembic_config(url)
        command.upgrade(config, "head")

        engine = create_engine(url)
        try:
            assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
            command.downgrade(config, "base")
            remaining = set(inspect(engine).get_table_names()) & set(Base.metadata.tables)
            assert not remaining, f"downgrade left tables behind: {sorted(remaining)}"
        finally:
            engine.dispose()
