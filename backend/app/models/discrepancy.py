"""Discrepancy -- one finding against one match record.

Supersedes the single `MatchRecord.category_code` FK from Phase 1 (ADR-0007 -> ADR-0012).
A settlement row can be late *and* short by a fee variance at the same time; forcing that
into one category would mean choosing which true thing to discard.

Every row records the rule that produced it and the field comparison behind it, so any
outcome can be traced to the exact comparison that caused it without re-running anything.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, uuid_pk
from app.models.enums import DetectedBy

# Code used when the deterministic rules find a real disagreement but no rule profile
# explains it. Deliberately not in UPPER_SNAKE like the taxonomy codes: it is not a
# classification, it is the absence of one, and it should not read as a peer of
# AMOUNT_MISMATCH in a report.
NOVEL_CATEGORY_CODE = "__novel__"


class Discrepancy(Base, TimestampMixin):
    __tablename__ = "discrepancies"

    id: Mapped[uuid.UUID] = uuid_pk()

    match_record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("match_records.id", ondelete="CASCADE"), nullable=False
    )

    category_code: Mapped[str] = mapped_column(
        ForeignKey("discrepancy_categories.code", ondelete="RESTRICT"), nullable=False
    )

    # Identifier of the rule that fired, e.g. "R07_mdr_fee_variance". Stable across runs
    # and quotable in a bug report: "rule R07 fired on this row" is actionable in a way
    # that "the engine flagged it" is not.
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)

    detected_by: Mapped[DetectedBy] = mapped_column(
        Enum(
            DetectedBy, native_enum=False, create_constraint=True, name="detected_by", length=16
        ),
        nullable=False,
        default=DetectedBy.DETERMINISTIC,
        server_default=DetectedBy.DETERMINISTIC.value,
    )

    # Signed amount this finding accounts for, in minor units. Where several findings
    # attach to one match, these are the parts that explain the total delta.
    delta_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    # The comparison itself: fields compared, values seen, tolerance applied. This is what
    # makes a finding checkable by hand rather than merely reported.
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    # Deterministic, human-readable statement of the comparison. Not an explanation and
    # not model output -- Phase 3 narration goes to audit_events, never here.
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    match_record = relationship("MatchRecord", back_populates="discrepancies")
    category = relationship("DiscrepancyCategory")

    __table_args__ = (
        # One finding per rule per match. Re-running the engine must not accumulate
        # duplicate findings for the same cause.
        UniqueConstraint(
            "match_record_id", "rule_id", name="uq_discrepancies_match_record_id_rule_id"
        ),
        Index("ix_discrepancies_match_record_id", "match_record_id"),
        Index("ix_discrepancies_category_code", "category_code"),
        Index("ix_discrepancies_rule_id", "rule_id"),
    )

    def __repr__(self) -> str:
        return f"<Discrepancy {self.category_code} via {self.rule_id} delta={self.delta_minor}>"
