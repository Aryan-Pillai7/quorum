"""Ingestion batches and quarantined rows.

Every transaction traces back to the file it came from, and every row that could not be
ingested is kept with the reason it failed. Nothing is silently dropped: a reconciliation
tool that quietly discards rows produces a match rate that means nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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
from app.models.enums import BatchStatus, QuarantineReason, SourceSystem


class IngestionBatch(Base, TimestampMixin):
    """One ingestion run of one file from one source."""

    __tablename__ = "ingestion_batches"

    id: Mapped[uuid.UUID] = uuid_pk()

    source: Mapped[SourceSystem] = mapped_column(
        Enum(SourceSystem, native_enum=False, create_constraint=True, name="source", length=32),
        nullable=False,
    )

    filename: Mapped[str] = mapped_column(String(512), nullable=False)

    # SHA-256 of the file bytes. Re-uploading an identical file is detectable before a
    # single row is parsed, which is cheaper and clearer than discovering it row by row
    # through unique-constraint violations.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[BatchStatus] = mapped_column(
        Enum(BatchStatus, native_enum=False, create_constraint=True, name="status", length=16),
        nullable=False,
        default=BatchStatus.PENDING,
        server_default=BatchStatus.PENDING.value,
    )

    # These three must add up. A CHECK enforces it, because a batch whose counts do not
    # reconcile makes every downstream number unreliable.
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    ingested_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    quarantined_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    quarantine = relationship(
        "QuarantinedRow", back_populates="batch", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "ingested_rows + quarantined_rows = total_rows", name="row_counts_reconcile"
        ),
        CheckConstraint(
            "total_rows >= 0 AND ingested_rows >= 0 AND quarantined_rows >= 0",
            name="row_counts_non_negative",
        ),
        Index("ix_ingestion_batches_source_started_at", "source", "started_at"),
        Index("ix_ingestion_batches_content_hash", "content_hash"),
    )

    def __repr__(self) -> str:
        return (
            f"<IngestionBatch {self.source} {self.filename} {self.status} "
            f"{self.ingested_rows}/{self.total_rows}>"
        )


class QuarantinedRow(Base):
    """A source row that could not be normalized, kept verbatim with its reason.

    No TimestampMixin: like an audit row, this is written once and never updated.
    """

    __tablename__ = "quarantined_rows"

    id: Mapped[uuid.UUID] = uuid_pk()

    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingestion_batches.id", ondelete="CASCADE"), nullable=False
    )

    # 1-based line number within the source file, so the operator can open the CSV and
    # look straight at the offending line.
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)

    reason: Mapped[QuarantineReason] = mapped_column(
        Enum(
            QuarantineReason, native_enum=False, create_constraint=True, name="reason", length=32
        ),
        nullable=False,
    )

    # Which field failed and what it contained. Without this a reason like
    # INVALID_AMOUNT is a shrug rather than a lead.
    detail: Mapped[str] = mapped_column(Text, nullable=False)

    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )

    batch = relationship("IngestionBatch", back_populates="quarantine")

    __table_args__ = (
        Index("ix_quarantined_rows_batch_id", "batch_id"),
        Index("ix_quarantined_rows_reason", "reason"),
    )

    def __repr__(self) -> str:
        return f"<QuarantinedRow batch={self.batch_id} row={self.row_number} {self.reason}>"
