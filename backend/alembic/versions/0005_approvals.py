"""the approval loop: approvals, audits, and the evidence that moves trust

Phase 6, ADR-0025. Additive: one new table. No existing column changes.

The table carries two decisions about one finding -- the approval that clears it
operationally, and the audit that makes it evidence. Only the second moves a trust score.

Revision ID: 0005_approvals
Revises: 0004_audit_hash_chain
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_approvals"
down_revision: str | None = "0004_audit_hash_chain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("discrepancy_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_code", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("proposed_action", sa.Text(), nullable=True),
        sa.Column("final_action", sa.Text(), nullable=False),
        sa.Column("approver_id", sa.String(128), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_status", sa.String(16), nullable=False, server_default="NOT_SELECTED"),
        sa.Column("audit_selection_reason", sa.Text(), nullable=False),
        sa.Column("auditor_id", sa.String(128), nullable=True),
        sa.Column("audited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("audit_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
        sa.ForeignKeyConstraint(
            ["discrepancy_id"], ["discrepancies.id"],
            name="fk_approvals_discrepancy_id_discrepancies", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["category_code"], ["discrepancy_categories.code"],
            name="fk_approvals_category_code_discrepancy_categories", ondelete="RESTRICT",
        ),
        # One approval per finding: two would double-count as evidence.
        sa.UniqueConstraint("discrepancy_id", name="uq_approvals_discrepancy_id"),
        sa.CheckConstraint(
            "decision IN ('APPROVED', 'EDITED_APPROVED', 'REJECTED')", name="decision"
        ),
        sa.CheckConstraint(
            "audit_status IN ('NOT_SELECTED', 'PENDING', 'CORRECT', 'INCORRECT')",
            name="audit_status",
        ),
        # An audited verdict must name who reached it and when. An anonymous audit is not
        # evidence, and this is the row that moves a trust score.
        sa.CheckConstraint(
            "audit_status NOT IN ('CORRECT', 'INCORRECT') "
            "OR (auditor_id IS NOT NULL AND audited_at IS NOT NULL)",
            name="audited_rows_name_their_auditor",
        ),
    )
    op.create_index("ix_approvals_category_code", "approvals", ["category_code"])
    op.create_index("ix_approvals_audit_status", "approvals", ["audit_status"])


def downgrade() -> None:
    op.drop_table("approvals")
