"""tamper-evident audit trail: sequence and hash chain

Phase 5, ADR-0022. Additive only: three columns on audit_events, no data rewritten.

Rows written before this migration keep NULL hashes. Back-filling a chain over them was
considered and rejected -- computing hashes retroactively proves nothing about rows that
were never hashed when written, since anyone with write access could compute the same
values. Verification reports them as predating the chain instead of implying coverage
they do not have.

Revision ID: 0004_audit_hash_chain
Revises: 0003_settlement_groups
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_audit_hash_chain"
down_revision: str | None = "0003_settlement_groups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A total order for the chain. Timestamps are not one: two events can share a
    # microsecond, and "the previous entry" has to be unambiguous.
    op.add_column("audit_events", sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False))
    op.create_unique_constraint("uq_audit_events_seq", "audit_events", ["seq"])
    op.create_index("ix_audit_events_seq", "audit_events", ["seq"])

    op.add_column("audit_events", sa.Column("prev_hash", sa.String(64), nullable=True))
    op.add_column("audit_events", sa.Column("entry_hash", sa.String(64), nullable=True))
    # Two rows sharing an entry hash would mean a replayed entry. The row id is part of
    # the hashed content, so this should be unreachable -- which is exactly why it is
    # worth asserting at the database level rather than trusting.
    op.create_unique_constraint("uq_audit_events_entry_hash", "audit_events", ["entry_hash"])


def downgrade() -> None:
    op.drop_constraint("uq_audit_events_entry_hash", "audit_events", type_="unique")
    op.drop_column("audit_events", "entry_hash")
    op.drop_column("audit_events", "prev_hash")
    op.drop_index("ix_audit_events_seq", table_name="audit_events")
    op.drop_constraint("uq_audit_events_seq", "audit_events", type_="unique")
    op.drop_column("audit_events", "seq")
