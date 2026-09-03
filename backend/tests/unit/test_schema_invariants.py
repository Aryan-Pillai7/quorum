"""Schema invariants asserted against ORM metadata. No database required.

These pin down the schema properties that reconciliation correctness depends on. Each
one is a rule that would be quietly easy to break in a later migration.
"""

from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, DateTime, Float

import app.models  # noqa: F401  (populates Base.metadata)
from app.db.base import Base

EXPECTED_TABLES = {
    "transactions",
    "match_records",
    "discrepancy_categories",
    "trust_scores",
    "audit_events",
}


def test_all_expected_tables_are_registered():
    """A model not imported in app/models/__init__.py is invisible to migrations."""
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_no_float_columns_anywhere():
    """ADR-0002. A Float column in a reconciliation schema is a latent rounding bug."""
    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Float)
    ]
    assert not offenders, f"float columns found: {offenders}"


def test_money_columns_are_bigint_minor_units():
    money_columns = [
        ("transactions", "amount_minor"),
        ("match_records", "amount_delta_minor"),
        ("discrepancy_categories", "tolerance_minor"),
    ]
    for table_name, column_name in money_columns:
        column = Base.metadata.tables[table_name].columns[column_name]
        assert isinstance(column.type, BigInteger), f"{table_name}.{column_name} is not BIGINT"


def test_every_timestamp_column_is_timezone_aware():
    """A naive timestamp turns an IST/UTC offset into a phantom date discrepancy."""
    naive = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime) and not column.type.timezone
    ]
    assert not naive, f"timezone-naive timestamp columns: {naive}"


def test_transactions_are_unique_per_source_and_external_id():
    """This constraint is what makes re-ingesting a settlement file idempotent."""
    constraints = {
        tuple(c.name for c in uc.columns)
        for uc in Base.metadata.tables["transactions"].constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert ("source", "external_id") in constraints


@pytest.mark.parametrize(
    "column", ["psp_transaction_id", "bank_transaction_id", "ledger_transaction_id"]
)
def test_each_match_leg_is_uniquely_indexed(column):
    """A transaction may belong to at most one match record."""
    indexes = Base.metadata.tables["match_records"].indexes
    matching = [ix for ix in indexes if [c.name for c in ix.columns] == [column]]
    assert matching, f"no index on match_records.{column}"
    assert matching[0].unique, f"index on match_records.{column} is not unique"


@pytest.mark.parametrize(
    "column", ["psp_transaction_id", "bank_transaction_id", "ledger_transaction_id"]
)
def test_match_legs_are_nullable(column):
    """A partial match is a legitimate, expected outcome -- not an error state."""
    assert Base.metadata.tables["match_records"].columns[column].nullable is True


def test_trust_score_thresholds_are_constrained_at_the_database_level():
    """Application checks can be bypassed by a script; a CHECK constraint cannot."""
    check_names = {
        c.name
        for c in Base.metadata.tables["trust_scores"].constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    # Full names, so this also pins down the metadata naming convention: an unnamed
    # constraint cannot be dropped or altered by a later migration.
    assert "ck_trust_scores_review_below_auto_apply" in check_names
    assert "ck_trust_scores_correct_count_within_sample" in check_names


def test_match_records_reject_zero_legs():
    check_names = {
        c.name
        for c in Base.metadata.tables["match_records"].constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert "ck_match_records_at_least_one_leg" in check_names


def test_audit_events_have_no_updated_at_column():
    """Append-only: an updated_at on an audit row would be permanently a lie (ADR-0008)."""
    assert "updated_at" not in Base.metadata.tables["audit_events"].columns
