"""AuditEvent -- append-only record of everything that changed state.

Append-only by convention in Phase 1, not enforced by a database trigger, and there is
no hash chain. This is an audit *trail*, not a *tamper-evident* one -- see ADR-0008.
The README says the same thing rather than implying a guarantee the schema lacks.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, uuid_pk
from app.models.enums import ActorType


class AuditEvent(Base):
    """One state change, attributed.

    No TimestampMixin: an audit row is never updated, so an `updated_at` column would
    be a column that must always be a lie.
    """

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = uuid_pk()

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    actor_type: Mapped[ActorType] = mapped_column(
        Enum(ActorType, native_enum=False, create_constraint=True, name="actor_type", length=16),
        nullable=False,
    )
    # Which specific actor: a user id, or the model id for an agent action. Keeping the
    # model id here is what makes "which model version decided this?" answerable later.
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    action: Mapped[str] = mapped_column(String(64), nullable=False)

    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # Free-form structured detail: before/after values, prompt hash, token counts,
    # latency, gate decision. JSONB because the useful fields differ per action type and
    # inventing a column per agent metric would be premature.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    __table_args__ = (
        # "What happened to this match record?" is the question actually asked.
        Index("ix_audit_events_entity_type_entity_id", "entity_type", "entity_id"),
        Index("ix_audit_events_occurred_at", "occurred_at"),
        Index("ix_audit_events_actor_type", "actor_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditEvent {self.action} {self.entity_type}:{self.entity_id} "
            f"by {self.actor_type}>"
        )
