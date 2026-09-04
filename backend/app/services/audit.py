"""Audit record writing.

Every state change and every agent call funnels through here, which is what makes adding
hash chaining later a change to one function rather than a hunt through the codebase
(ADR-0008).

Honest framing, repeated here because it matters: this is an audit *trail*, not a
*tamper-evident* one. Rows are append-only by convention and code review. There is no
hash chain and no database trigger preventing UPDATE or DELETE.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models import ActorType, AuditEvent

logger = logging.getLogger(__name__)


def record(
    session: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one audit event. Does not commit -- the caller owns the transaction.

    Not committing here is deliberate: an audit row must land in the same transaction as
    the change it describes, or the trail can disagree with the data it is auditing.
    """
    event = AuditEvent(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload or {},
    )
    session.add(event)
    logger.info(
        "audit event recorded",
        extra={
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor_type": actor_type.value,
        },
    )
    return event
