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
    "discrepancies",
    "trust_scores",
    "audit_events",
    "ingestion_batches",
    "quarantined_rows",
    "settlement_groups",
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
        ("transactions", "gross_amount_minor"),
        ("transactions", "fee_minor"),
        ("match_records", "amount_delta_minor"),
        ("discrepancies", "delta_minor"),
        ("discrepancy_categories", "tolerance_minor"),
        ("settlement_groups", "bank_amount_minor"),
        ("settlement_groups", "members_total_minor"),
        ("settlement_groups", "delta_minor"),
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


def test_match_records_no_longer_carry_a_single_category():
    """ADR-0012 supersedes ADR-0007: findings live in `discrepancies`, one row per rule.

    A settlement row can be late and short on fee at once, and a single column would
    force discarding one of two true statements.
    """
    assert "category_code" not in Base.metadata.tables["match_records"].columns


def test_a_match_can_carry_several_findings_but_not_two_from_one_rule():
    """Multi-label is the point; duplicate findings from one rule are not."""
    unique = {
        tuple(c.name for c in uc.columns)
        for uc in Base.metadata.tables["discrepancies"].constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert ("match_record_id", "rule_id") in unique
    match_fk = [
        c.name
        for c in Base.metadata.tables["discrepancies"].columns
        if c.name == "match_record_id"
    ]
    assert match_fk, "discrepancies must reference a match record"


def test_ingestion_batch_row_counts_must_reconcile():
    """A batch whose counts do not add up makes every derived number unreliable."""
    check_names = {
        c.name
        for c in Base.metadata.tables["ingestion_batches"].constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert "ck_ingestion_batches_row_counts_reconcile" in check_names


def test_quarantined_rows_are_write_once():
    """Like audit rows: written once with a reason, never revised."""
    assert "updated_at" not in Base.metadata.tables["quarantined_rows"].columns


def test_a_bank_credit_can_be_claimed_by_at_most_one_settlement_group():
    """Two overlapping groups would each claim the same money (ADR-0019)."""
    unique = {
        tuple(c.name for c in uc.columns)
        for uc in Base.metadata.tables["settlement_groups"].constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert ("bank_transaction_id",) in unique


def test_a_resolved_group_must_be_unique_and_plural():
    """A group of one is a 1:1 match; a group with two solutions is a guess."""
    check_names = {
        c.name
        for c in Base.metadata.tables["settlement_groups"].constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert "ck_settlement_groups_resolved_group_is_unique_and_plural" in check_names


def test_the_one_to_one_match_legs_are_untouched_by_phase_4():
    """ADR-0019 extends ADR-0005 for one case; it does not redesign it."""
    columns = Base.metadata.tables["match_records"].columns
    for leg in ("psp_transaction_id", "bank_transaction_id", "ledger_transaction_id"):
        assert leg in columns
        assert columns[leg].nullable is True
