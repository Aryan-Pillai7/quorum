"""ingestion batches, quarantined rows, normalized discrepancies, taxonomy extension

Phase 2. Three changes, each with a reason:

1. Ingestion becomes traceable: every transaction points at the batch that produced it,
   and every row that could not be normalized is kept with the reason it failed.

2. Discrepancies are normalized out of `match_records.category_code` into their own
   table (ADR-0012 supersedes ADR-0007). A settlement row can be late *and* short by a
   fee variance simultaneously; one column would force discarding one true statement.

3. The taxonomy gains the failure modes the Phase 2 engine actually classifies, and
   `UNCLASSIFIED` is replaced by `__novel__` -- see the note on that below.

Revision ID: 0002_ingestion_and_discrepancies
Revises: 0001_initial_schema
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_ingestion_and_discrepancies"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Failure modes the Phase 2 matching engine classifies that Phase 1 had no code for.
# (code, display_name, description, severity, tolerance_minor, auto_resolvable)
NEW_CATEGORIES: list[tuple[str, str, str, str, int, bool]] = [
    (
        "MDR_FEE_VARIANCE",
        "MDR fee variance",
        "The fee the processor actually deducted differs from the fee the ledger "
        "expected under the contracted rate. Distinct from FEE_DEDUCTION, which is the "
        "expected fee correctly explaining a gross/net gap.",
        "MEDIUM",
        200,  # up to 2.00 INR of fee drift is treated as rounding, not variance
        True,
    ),
    (
        "PARTIAL_CAPTURE",
        "Partial capture",
        "Less was captured than the ledger authorised, so the settled amount is short "
        "by a whole-rupee margin rather than by a fee.",
        "HIGH",
        0,
        False,
    ),
    (
        "ROUTING_SPLIT",
        "Routing split",
        "One ledger order settled across several processor rows, typically because the "
        "payment was routed through more than one acquirer. The parts should sum to the "
        "ledger amount.",
        "MEDIUM",
        0,
        False,
    ),
    (
        # Not UPPER_SNAKE on purpose: this is the absence of a classification, not a peer
        # of AMOUNT_MISMATCH, and it should not read like one in a report. Never
        # auto-resolvable by construction -- automating what no rule understood is
        # precisely the thing this system exists to avoid.
        "__novel__",
        "Novel (unclassified by any rule)",
        "A real disagreement that no deterministic rule profile explains. Phase 3 asks "
        "the agent layer to propose a classification; the trust gate decides whether "
        "that proposal may be acted on.",
        "HIGH",
        0,
        False,
    ),
]

# Cold-start trust posture, matching the Phase 1 seeding convention.
THRESHOLDS_BY_SEVERITY: dict[str, tuple[float, float, int]] = {
    "LOW": (0.85, 0.50, 30),
    "MEDIUM": (0.90, 0.60, 50),
    "HIGH": (0.95, 0.70, 100),
    "CRITICAL": (0.99, 0.80, 250),
}


def upgrade() -> None:
    # --- ingestion_batches ----------------------------------------------------------
    op.create_table(
        "ingestion_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("total_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ingested_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quarantined_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_batches"),
        sa.CheckConstraint(
            "source IN ('PSP_SETTLEMENT', 'BANK_STATEMENT', 'INTERNAL_LEDGER')", name="source"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'COMPLETED', 'FAILED')", name="status"
        ),
        # A batch whose counts do not add up makes every number derived from it suspect.
        sa.CheckConstraint(
            "ingested_rows + quarantined_rows = total_rows", name="row_counts_reconcile"
        ),
        sa.CheckConstraint(
            "total_rows >= 0 AND ingested_rows >= 0 AND quarantined_rows >= 0",
            name="row_counts_non_negative",
        ),
    )
    op.create_index(
        "ix_ingestion_batches_source_started_at", "ingestion_batches", ["source", "started_at"]
    )
    op.create_index("ix_ingestion_batches_content_hash", "ingestion_batches", ["content_hash"])

    # --- quarantined_rows -----------------------------------------------------------
    op.create_table(
        "quarantined_rows",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quarantined_rows"),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["ingestion_batches.id"],
            name="fk_quarantined_rows_batch_id_ingestion_batches",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "reason IN ('MISSING_REQUIRED_FIELD', 'INVALID_AMOUNT', 'INVALID_DATE', "
            "'UNSUPPORTED_CURRENCY', 'DUPLICATE_IN_BATCH', 'MALFORMED_ROW')",
            name="reason",
        ),
    )
    op.create_index("ix_quarantined_rows_batch_id", "quarantined_rows", ["batch_id"])
    op.create_index("ix_quarantined_rows_reason", "quarantined_rows", ["reason"])

    # --- transactions: batch link, commerce reference, gross/fee detail --------------
    op.add_column(
        "transactions", sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("transactions", sa.Column("order_ref", sa.String(128), nullable=True))
    op.add_column("transactions", sa.Column("gross_amount_minor", sa.BigInteger(), nullable=True))
    op.add_column("transactions", sa.Column("fee_minor", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_transactions_batch_id_ingestion_batches",
        "transactions",
        "ingestion_batches",
        ["batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_transactions_order_ref", "transactions", ["order_ref"])
    op.create_index("ix_transactions_batch_id", "transactions", ["batch_id"])

    # --- taxonomy: add Phase 2 categories, retire UNCLASSIFIED ----------------------
    _seed_new_categories()

    # Phase 1 described TIMING_DIFFERENCE as requiring the amounts to agree. Phase 2
    # treats lateness and shortness as independent observations about one payment
    # (ADR-0012), so the description is corrected to match what the rule actually asserts.
    op.execute(
        "UPDATE discrepancy_categories SET description = "
        "'The bank credit lands in a later settlement window than the processor "
        "settlement date. Independent of whether the amounts also agree.' "
        "WHERE code = 'TIMING_DIFFERENCE'"
    )

    # UNCLASSIFIED is replaced by __novel__, which carries the same meaning with a name
    # that cannot be mistaken for a real classification. Safe to delete outright: nothing
    # references it, because Phase 1 shipped no code that assigns categories.
    op.execute("DELETE FROM trust_scores WHERE category_code = 'UNCLASSIFIED'")
    op.execute("DELETE FROM discrepancy_categories WHERE code = 'UNCLASSIFIED'")

    # --- match_records: category_code -> discrepancies table -------------------------
    op.add_column(
        "match_records",
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.drop_constraint(
        "fk_match_records_category_code_discrepancy_categories", "match_records", type_="foreignkey"
    )
    op.drop_column("match_records", "category_code")

    # --- discrepancies ---------------------------------------------------------------
    op.create_table(
        "discrepancies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("match_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_code", sa.String(64), nullable=False),
        sa.Column("rule_id", sa.String(64), nullable=False),
        sa.Column("detected_by", sa.String(16), nullable=False, server_default="DETERMINISTIC"),
        sa.Column("delta_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_discrepancies"),
        sa.ForeignKeyConstraint(
            ["match_record_id"],
            ["match_records.id"],
            name="fk_discrepancies_match_record_id_match_records",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_code"],
            ["discrepancy_categories.code"],
            name="fk_discrepancies_category_code_discrepancy_categories",
            ondelete="RESTRICT",
        ),
        # Re-running the engine must not accumulate duplicate findings for one cause.
        sa.UniqueConstraint(
            "match_record_id", "rule_id", name="uq_discrepancies_match_record_id_rule_id"
        ),
        sa.CheckConstraint(
            "detected_by IN ('DETERMINISTIC', 'AGENT')", name="detected_by"
        ),
    )
    op.create_index("ix_discrepancies_match_record_id", "discrepancies", ["match_record_id"])
    op.create_index("ix_discrepancies_category_code", "discrepancies", ["category_code"])
    op.create_index("ix_discrepancies_rule_id", "discrepancies", ["rule_id"])


def _seed_new_categories() -> None:
    categories_table = sa.table(
        "discrepancy_categories",
        sa.column("code", sa.String),
        sa.column("display_name", sa.String),
        sa.column("description", sa.Text),
        sa.column("severity", sa.String),
        sa.column("tolerance_minor", sa.BigInteger),
        sa.column("auto_resolvable", sa.Boolean),
    )
    op.bulk_insert(
        categories_table,
        [
            {
                "code": code,
                "display_name": display_name,
                "description": description,
                "severity": severity,
                "tolerance_minor": tolerance,
                "auto_resolvable": auto_resolvable,
            }
            for code, display_name, description, severity, tolerance, auto_resolvable in (
                NEW_CATEGORIES
            )
        ],
    )

    trust_table = sa.table(
        "trust_scores",
        sa.column("category_code", sa.String),
        sa.column("score", sa.Numeric),
        sa.column("sample_size", sa.Integer),
        sa.column("correct_count", sa.Integer),
        sa.column("auto_apply_threshold", sa.Numeric),
        sa.column("review_threshold", sa.Numeric),
        sa.column("min_sample_size", sa.Integer),
    )
    op.bulk_insert(
        trust_table,
        [
            {
                "category_code": code,
                "score": 0,
                "sample_size": 0,
                "correct_count": 0,
                "auto_apply_threshold": THRESHOLDS_BY_SEVERITY[severity][0],
                "review_threshold": THRESHOLDS_BY_SEVERITY[severity][1],
                "min_sample_size": THRESHOLDS_BY_SEVERITY[severity][2],
            }
            for code, _display, _desc, severity, _tol, _auto in NEW_CATEGORIES
        ],
    )


def downgrade() -> None:
    op.drop_table("discrepancies")

    op.add_column("match_records", sa.Column("category_code", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_match_records_category_code_discrepancy_categories",
        "match_records",
        "discrepancy_categories",
        ["category_code"],
        ["code"],
        ondelete="RESTRICT",
    )
    op.drop_column("match_records", "evidence")

    # Restore UNCLASSIFIED before removing __novel__, so the taxonomy is never empty of
    # a catch-all category at any point during the downgrade.
    op.execute(
        "INSERT INTO discrepancy_categories "
        "(code, display_name, description, severity, tolerance_minor, auto_resolvable) "
        "VALUES ('UNCLASSIFIED', 'Unclassified discrepancy', "
        "'No deterministic rule matched. Never auto-resolvable by construction.', "
        "'HIGH', 0, false)"
    )
    op.execute(
        "INSERT INTO trust_scores (category_code, score, sample_size, correct_count, "
        "auto_apply_threshold, review_threshold, min_sample_size) "
        "VALUES ('UNCLASSIFIED', 0, 0, 0, 0.95, 0.70, 100)"
    )
    codes = ", ".join(f"'{code}'" for code, *_ in NEW_CATEGORIES)
    op.execute(f"DELETE FROM trust_scores WHERE category_code IN ({codes})")
    op.execute(f"DELETE FROM discrepancy_categories WHERE code IN ({codes})")

    op.drop_index("ix_transactions_batch_id", table_name="transactions")
    op.drop_index("ix_transactions_order_ref", table_name="transactions")
    op.drop_constraint(
        "fk_transactions_batch_id_ingestion_batches", "transactions", type_="foreignkey"
    )
    op.drop_column("transactions", "fee_minor")
    op.drop_column("transactions", "gross_amount_minor")
    op.drop_column("transactions", "order_ref")
    op.drop_column("transactions", "batch_id")

    op.drop_table("quarantined_rows")
    op.drop_table("ingestion_batches")
