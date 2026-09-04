"""aggregated payouts: settlement groups, new match strategies, ambiguity category

Phase 4, ADR-0019. Removes the 1:1 settlement-to-bank assumption from ADR-0014 for the
settlement->bank leg only. The ledger->settlement leg is untouched.

Two new MatchStrategy values are added to the CHECK constraint. This is exactly the
migration ADR-0006 chose VARCHAR + CHECK to make ordinary rather than an ALTER TYPE.

Revision ID: 0003_settlement_groups
Revises: 0002_ingestion_and_discrepancies
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_settlement_groups"
down_revision: str | None = "0002_ingestion_and_discrepancies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MATCH_STRATEGIES_V2 = (
    "'EXACT_REFERENCE'",
    "'REFERENCE_AMOUNT_TOLERANCE'",
    "'AMOUNT_DATE_WINDOW'",
    "'AGGREGATE_SHARED_REFERENCE'",
    "'AGGREGATE_SUBSET_SUM'",
    "'MANUAL'",
)
MATCH_STRATEGIES_V1 = (
    "'EXACT_REFERENCE'",
    "'REFERENCE_AMOUNT_TOLERANCE'",
    "'AMOUNT_DATE_WINDOW'",
    "'MANUAL'",
)

# One new category, and only one. A settlement row that resolves into no group at all is
# already covered by MISSING_IN_BANK; a group whose members do not sum is an
# AMOUNT_MISMATCH. What has no existing home is the case where SEVERAL sets of settlement
# rows each explain the credit equally well -- the engine can see that it does not know,
# and refusing to guess is a distinct outcome worth naming and scoring separately.
AMBIGUOUS_CATEGORY = (
    "AGGREGATION_AMBIGUOUS",
    "Aggregation ambiguous",
    "More than one distinct set of settlement rows sums to this bank credit. The engine "
    "will not choose between them: picking one would silently attribute money to the "
    "wrong payments. Routed to a human with every candidate set recorded.",
    "HIGH",
    0,
    False,
)


def upgrade() -> None:
    op.create_table(
        "settlement_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bank_transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("bank_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("members_total_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("delta_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidates_considered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("solution_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("nodes_explored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_settlement_groups"),
        sa.ForeignKeyConstraint(
            ["bank_transaction_id"],
            ["transactions.id"],
            name="fk_settlement_groups_bank_transaction_id_transactions",
            ondelete="RESTRICT",
        ),
        # A bank credit is explained by at most one group. Without this, two overlapping
        # groups could each claim the same money.
        sa.UniqueConstraint(
            "bank_transaction_id", name="uq_settlement_groups_bank_transaction_id"
        ),
        sa.CheckConstraint(
            "method IN ('SHARED_REFERENCE', 'SUBSET_SUM')", name="method"
        ),
        sa.CheckConstraint(
            "status IN ('RESOLVED', 'AMBIGUOUS', 'INCONCLUSIVE')", name="status"
        ),
        sa.CheckConstraint("member_count >= 0", name="member_count_non_negative"),
        sa.CheckConstraint("candidates_considered >= 0", name="candidates_non_negative"),
        sa.CheckConstraint("solution_count >= 0", name="solution_count_non_negative"),
        # A RESOLVED group must be unique and plural: exactly one solution, at least two
        # members. A "group" of one is a 1:1 match and belongs on the ordinary path.
        sa.CheckConstraint(
            "status <> 'RESOLVED' OR (solution_count = 1 AND member_count >= 2)",
            name="resolved_group_is_unique_and_plural",
        ),
    )
    op.create_index("ix_settlement_groups_status", "settlement_groups", ["status"])
    op.create_index("ix_settlement_groups_method", "settlement_groups", ["method"])

    op.add_column(
        "transactions",
        sa.Column("settlement_group_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_transactions_settlement_group_id_settlement_groups",
        "transactions",
        "settlement_groups",
        ["settlement_group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_transactions_settlement_group_id", "transactions", ["settlement_group_id"]
    )

    op.add_column(
        "match_records",
        sa.Column("settlement_group_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_match_records_settlement_group_id_settlement_groups",
        "match_records",
        "settlement_groups",
        ["settlement_group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_match_records_settlement_group_id", "match_records", ["settlement_group_id"]
    )

    # Widen the strategy CHECK. An ordinary migration, which is the whole point of
    # ADR-0006 having chosen VARCHAR + CHECK over a native enum type.
    # Bare name: the metadata naming convention expands it to ck_match_records_strategy.
    # Passing the expanded name gets it expanded a second time -- the same trap that
    # produced ck_transactions_ck_transactions_source back in Phase 1.
    op.drop_constraint("strategy", "match_records", type_="check")
    op.create_check_constraint(
        "strategy", "match_records", f"strategy IN ({', '.join(MATCH_STRATEGIES_V2)})"
    )

    _seed_ambiguous_category()


def _seed_ambiguous_category() -> None:
    code, display_name, description, severity, tolerance, auto_resolvable = AMBIGUOUS_CATEGORY
    op.bulk_insert(
        sa.table(
            "discrepancy_categories",
            sa.column("code", sa.String),
            sa.column("display_name", sa.String),
            sa.column("description", sa.Text),
            sa.column("severity", sa.String),
            sa.column("tolerance_minor", sa.BigInteger),
            sa.column("auto_resolvable", sa.Boolean),
        ),
        [
            {
                "code": code,
                "display_name": display_name,
                "description": description,
                "severity": severity,
                "tolerance_minor": tolerance,
                "auto_resolvable": auto_resolvable,
            }
        ],
    )
    op.bulk_insert(
        sa.table(
            "trust_scores",
            sa.column("category_code", sa.String),
            sa.column("score", sa.Numeric),
            sa.column("sample_size", sa.Integer),
            sa.column("correct_count", sa.Integer),
            sa.column("auto_apply_threshold", sa.Numeric),
            sa.column("review_threshold", sa.Numeric),
            sa.column("min_sample_size", sa.Integer),
        ),
        [
            {
                "category_code": code,
                "score": 0,
                "sample_size": 0,
                "correct_count": 0,
                "auto_apply_threshold": 0.95,
                "review_threshold": 0.70,
                "min_sample_size": 100,
            }
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM trust_scores WHERE category_code = 'AGGREGATION_AMBIGUOUS'")
    op.execute("DELETE FROM discrepancy_categories WHERE code = 'AGGREGATION_AMBIGUOUS'")

    # Any record written with an aggregation strategy would violate the narrowed CHECK,
    # so those rows are reverted to the closest Phase 2 meaning before it is re-applied.
    op.execute(
        "UPDATE match_records SET strategy = 'AMOUNT_DATE_WINDOW' "
        "WHERE strategy IN ('AGGREGATE_SHARED_REFERENCE', 'AGGREGATE_SUBSET_SUM')"
    )
    op.drop_constraint("strategy", "match_records", type_="check")
    op.create_check_constraint(
        "strategy", "match_records", f"strategy IN ({', '.join(MATCH_STRATEGIES_V1)})"
    )

    op.drop_index("ix_match_records_settlement_group_id", table_name="match_records")
    op.drop_constraint(
        "fk_match_records_settlement_group_id_settlement_groups",
        "match_records",
        type_="foreignkey",
    )
    op.drop_column("match_records", "settlement_group_id")

    op.drop_index("ix_transactions_settlement_group_id", table_name="transactions")
    op.drop_constraint(
        "fk_transactions_settlement_group_id_settlement_groups",
        "transactions",
        type_="foreignkey",
    )
    op.drop_column("transactions", "settlement_group_id")

    op.drop_table("settlement_groups")
