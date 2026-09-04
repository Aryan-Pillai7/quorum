"""Approval -- a human's verdict on an agent-drafted correction, and its audit.

Two distinct events live on one row because they are two decisions about the same thing:

- the **approval** clears the finding operationally, immediately;
- the **audit** is a second human confirming the first was right, and is the only thing
  that moves a trust score (ADR-0025).

Keeping them apart is what stops the system from learning from its own unexamined output.
An approval nobody checked is an operational fact, not evidence.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, uuid_pk
from app.models.enums import ApprovalDecision, AuditStatus


class Approval(Base, TimestampMixin):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = uuid_pk()

    # One approval per finding. A second attempt is a conflict, not a duplicate row:
    # two approvals on one finding would double-count as evidence.
    discrepancy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("discrepancies.id", ondelete="RESTRICT"), nullable=False
    )

    # Denormalized from the discrepancy so the audit-selection rule and trust update can
    # work without a join, and so the row still says which category it counted toward
    # even if the finding is later reclassified.
    category_code: Mapped[str] = mapped_column(
        ForeignKey("discrepancy_categories.code", ondelete="RESTRICT"), nullable=False
    )

    decision: Mapped[ApprovalDecision] = mapped_column(
        Enum(
            ApprovalDecision, native_enum=False, create_constraint=True,
            name="decision", length=24,
        ),
        nullable=False,
    )

    # The agent's draft, snapshotted at approval time. The audit trail must show what the
    # human was actually shown, not what the model would say if asked again today.
    proposed_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What was actually approved. Differs from proposed_action only on EDITED_APPROVED.
    final_action: Mapped[str] = mapped_column(Text, nullable=False)

    approver_id: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    audit_status: Mapped[AuditStatus] = mapped_column(
        Enum(
            AuditStatus, native_enum=False, create_constraint=True,
            name="audit_status", length=16,
        ),
        nullable=False,
        default=AuditStatus.NOT_SELECTED,
        server_default=AuditStatus.NOT_SELECTED.value,
    )
    # Why this approval was or was not selected for audit. Recorded so the sampling rule
    # is inspectable after the fact rather than being a black box.
    audit_selection_reason: Mapped[str] = mapped_column(Text, nullable=False)

    auditor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    audited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    audit_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    discrepancy = relationship("Discrepancy")

    __table_args__ = (
        UniqueConstraint("discrepancy_id", name="uq_approvals_discrepancy_id"),
        # An audited verdict must name who reached it and when. An anonymous audit is not
        # evidence, and this is the row that moves a trust score.
        CheckConstraint(
            "audit_status NOT IN ('CORRECT', 'INCORRECT') "
            "OR (auditor_id IS NOT NULL AND audited_at IS NOT NULL)",
            name="audited_rows_name_their_auditor",
        ),
        Index("ix_approvals_category_code", "category_code"),
        Index("ix_approvals_audit_status", "audit_status"),
    )

    @property
    def counts_toward_trust(self) -> bool:
        """Only an audited verdict is evidence."""
        return self.audit_status in (AuditStatus.CORRECT, AuditStatus.INCORRECT)

    def __repr__(self) -> str:
        return (
            f"<Approval {self.category_code} {self.decision} audit={self.audit_status}>"
        )
