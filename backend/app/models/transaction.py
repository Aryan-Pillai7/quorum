"""Transaction -- one normalized row from any of the three sources."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Enum, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid_pk
from app.models.enums import Direction, SourceSystem, TransactionStatus


class Transaction(Base, TimestampMixin):
    """A single financial line item as one source reported it.

    The same real-world payment appears here up to three times -- once per source --
    and it is precisely those rows that reconciliation tries to bring together. Rows
    are never edited to make them agree; disagreement is recorded, not erased.
    """

    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = uuid_pk()

    source: Mapped[SourceSystem] = mapped_column(
        Enum(SourceSystem, native_enum=False, length=32), nullable=False
    )

    # The identifier this source uses for the row: PSP payment id, bank txn id,
    # ledger entry id. Unique per source, which is what makes re-ingesting a file safe.
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # The value the three sources are expected to share: UTR / RRN / bank reference.
    # Nullable because bank statements frequently omit it -- that absence is exactly
    # what forces the fallback amount+date matching pass in Phase 2.
    counterparty_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    direction: Mapped[Direction] = mapped_column(
        Enum(Direction, native_enum=False, length=16), nullable=False
    )

    # When the money moved according to this source. Timezone-aware: a settlement file
    # in IST and a bank statement in UTC that differ by 5h30m are the same instant, and
    # a naive timestamp would turn that into a phantom date discrepancy.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus, native_enum=False, length=16),
        nullable=False,
        default=TransactionStatus.UNMATCHED,
        server_default=TransactionStatus.UNMATCHED.value,
    )

    # The source row exactly as received. Never parsed for logic -- kept so any
    # normalization decision can be re-checked against the original.
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")

    __table_args__ = (
        # Idempotent ingestion depends on this: re-uploading the same settlement file
        # conflicts instead of duplicating.
        UniqueConstraint("source", "external_id", name="uq_transactions_source_external_id"),
        Index("ix_transactions_counterparty_ref", "counterparty_ref"),
        Index("ix_transactions_source_occurred_at", "source", "occurred_at"),
        Index("ix_transactions_status", "status"),
        # Amount+date fallback matching scans this; without it that pass is a seq scan.
        Index("ix_transactions_amount_minor_occurred_at", "amount_minor", "occurred_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Transaction {self.source} {self.external_id} "
            f"{self.amount_minor} {self.currency} {self.status}>"
        )
