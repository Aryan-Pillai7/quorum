"""SettlementGroup -- N settlement rows explaining one bank credit.

Phase 4, ADR-0019. Extends ADR-0005 rather than replacing it: the 1:1 case is untouched
and still uses three explicit legs on a match record.

The bank credit belongs to the *group*, not to any member. That is the honest modelling --
no individual settlement row matched that credit, the set did -- and it also avoids having
to anoint one member as an arbitrary anchor. The group's bank leg is carried by its own
match record, so the unique index on `match_records.bank_transaction_id` still guarantees
exactly one consumer per credit, and every transaction still belongs to exactly one match
record.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, uuid_pk
from app.models.enums import GroupMethod, GroupStatus


class SettlementGroup(Base, TimestampMixin):
    __tablename__ = "settlement_groups"

    id: Mapped[uuid.UUID] = uuid_pk()

    # UNIQUE: a bank credit is explained by at most one group. Without this, two
    # overlapping groups could each claim the same money.
    bank_transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=False, unique=True
    )

    method: Mapped[GroupMethod] = mapped_column(
        Enum(GroupMethod, native_enum=False, create_constraint=True, name="method", length=32),
        nullable=False,
    )
    status: Mapped[GroupStatus] = mapped_column(
        Enum(GroupStatus, native_enum=False, create_constraint=True, name="status", length=16),
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # What the bank credited, and what the members sum to. Stored separately rather than
    # as a single delta so the two sides of the comparison remain readable in the row.
    bank_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    members_total_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    delta_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    member_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Search accounting. `solution_count` is capped at 2 by the search itself: knowing
    # whether more than one set works is the whole question, and counting past that is
    # wasted work. `candidates_considered` and `nodes_explored` make an INCONCLUSIVE
    # result auditable -- it says the search was bounded out, not that nothing exists.
    candidates_considered: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    solution_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    nodes_explored: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    bank_transaction = relationship("Transaction", foreign_keys=[bank_transaction_id])
    members = relationship(
        "Transaction",
        back_populates="settlement_group",
        foreign_keys="Transaction.settlement_group_id",
    )

    __table_args__ = (
        CheckConstraint("member_count >= 0", name="member_count_non_negative"),
        CheckConstraint("candidates_considered >= 0", name="candidates_non_negative"),
        CheckConstraint("solution_count >= 0", name="solution_count_non_negative"),
        # A RESOLVED group must have exactly one solution and at least two members --
        # a "group" of one is a 1:1 match and belongs on the ordinary path.
        CheckConstraint(
            "status <> 'RESOLVED' OR (solution_count = 1 AND member_count >= 2)",
            name="resolved_group_is_unique_and_plural",
        ),
        Index("ix_settlement_groups_status", "status"),
        Index("ix_settlement_groups_method", "method"),
    )

    def __repr__(self) -> str:
        return (
            f"<SettlementGroup {self.method} {self.status} n={self.member_count} "
            f"bank={self.bank_amount_minor} members={self.members_total_minor}>"
        )
