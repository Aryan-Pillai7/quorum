"""AuditEvent -- append-only, hash-chained record of everything that changed state.

Phase 5 (ADR-0022) adds the hash chain ADR-0008 deferred. Each row carries the hash of
its own content plus the previous row's hash, so a retroactive edit, a deletion, or an
insertion anywhere in the chain breaks every hash after it.

Precise about what that buys, because the word "tamper-evident" invites overclaiming:
it DETECTS alteration of stored rows. It does not PREVENT it, and someone with write
access to the database can recompute the whole chain forward from a tampered row. There
is no signing key and no external anchor. See ADR-0022 and the README.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Enum, Identity, Index, String, func
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

    # The chain needs a total order, and timestamps are not one: two events can share a
    # microsecond. This monotonic sequence is what "previous entry" means.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False, unique=True)

    # Hash of this row's content plus prev_hash. Nullable because rows written before
    # Phase 5 have no hash, and back-filling one retroactively would prove nothing about
    # them -- anyone with write access could do the same. Verification reports them as
    # predating the chain rather than pretending they are covered.
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)

    # Set application-side, not by the server default: the timestamp is part of the
    # hashed content, so it has to be known before the row is written.
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
        Index("ix_audit_events_seq", "seq"),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditEvent {self.action} {self.entity_type}:{self.entity_id} "
            f"by {self.actor_type}>"
        )
