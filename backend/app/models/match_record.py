"""MatchRecord -- one attempted three-way reconciliation."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, uuid_pk
from app.models.enums import MatchStatus, MatchStrategy
from app.models.transaction import Transaction


class MatchRecord(Base, TimestampMixin):
    """Links up to three transactions -- one per source -- into a single reconciliation.

    Three explicit nullable legs rather than a join table (ADR-0005): "three" is a
    domain constant, and a missing leg is answerable with `WHERE bank_transaction_id
    IS NULL` instead of an aggregation.
    """

    __tablename__ = "match_records"

    id: Mapped[uuid.UUID] = uuid_pk()

    # The value the legs were joined on (UTR, or a synthetic key for amount+date
    # matches). Kept so a match can be re-derived and re-checked by hand.
    match_key: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[MatchStatus] = mapped_column(
        Enum(MatchStatus, native_enum=False, create_constraint=True, name="status", length=16),
        nullable=False,
    )
    strategy: Mapped[MatchStrategy] = mapped_column(
        Enum(MatchStrategy, native_enum=False, create_constraint=True, name="strategy", length=32),
        nullable=False,
    )

    # Deterministic confidence from the rule that fired -- 1.0 for an exact reference
    # match. NOT a model probability: nothing an LLM produces is written here.
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)

    psp_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=True
    )
    bank_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=True
    )
    ledger_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=True
    )

    # Net money disagreement across the legs, in minor units. 0 on a clean match.
    amount_delta_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    # Findings live in the `discrepancies` table, one row per rule that fired
    # (ADR-0012 supersedes ADR-0007). A match can be both late and short on fee, and
    # forcing that into one column would mean discarding one of two true statements.

    # How the legs were joined: which field was compared for each leg, and by which pass.
    # This is the traceability record for the match itself, as distinct from the
    # per-finding evidence on each discrepancy row.
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    # Human-readable summary of the rule outcome. Deterministic text only; agent-written
    # explanations live in audit_events, never here.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    psp_transaction = relationship(Transaction, foreign_keys=[psp_transaction_id])
    bank_transaction = relationship(Transaction, foreign_keys=[bank_transaction_id])
    ledger_transaction = relationship(Transaction, foreign_keys=[ledger_transaction_id])
    discrepancies = relationship(
        "Discrepancy", back_populates="match_record", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # A transaction belongs to at most one match. Postgres permits unlimited NULLs
        # under a UNIQUE index, which is exactly the semantics an optional leg needs.
        Index("uq_match_records_psp_transaction_id", "psp_transaction_id", unique=True),
        Index("uq_match_records_bank_transaction_id", "bank_transaction_id", unique=True),
        Index("uq_match_records_ledger_transaction_id", "ledger_transaction_id", unique=True),
        Index("ix_match_records_match_key", "match_key"),
        Index("ix_match_records_status", "status"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_in_range"),
        # A match with no legs is meaningless and would otherwise be silently insertable.
        CheckConstraint(
            "num_nonnulls(psp_transaction_id, bank_transaction_id, ledger_transaction_id) >= 1",
            name="at_least_one_leg",
        ),
    )

    @property
    def leg_count(self) -> int:
        """How many of the three sources are represented."""
        return sum(
            1
            for leg in (
                self.psp_transaction_id,
                self.bank_transaction_id,
                self.ledger_transaction_id,
            )
            if leg is not None
        )

    def __repr__(self) -> str:
        return (
            f"<MatchRecord {self.match_key} {self.status} "
            f"legs={self.leg_count}/3 delta={self.amount_delta_minor}>"
        )
