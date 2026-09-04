"""Audit record writing and hash-chain verification.

Every state change and every agent call funnels through `record()`. That was already true
before Phase 5 -- ADR-0008 said so and the codebase held to it -- which is why adding the
hash chain was a change to one function rather than a hunt through the codebase.

## What the chain does and does not do (ADR-0022)

Each row stores `entry_hash = sha256(its own content || prev_hash)`. So:

- **Detects** a retroactive edit to any chained row: its hash no longer matches its
  content, and every hash after it no longer links.
- **Detects** a deleted or inserted row: the chain's linkage breaks at that point.
- **Does NOT prevent** any of it. Someone with write access to the database can edit a row
  and recompute every hash from that point forward, and verification would pass.
- **Does NOT cover** rows written before the chain existed, which keep NULL hashes.

There is no signing key and no external anchor, so the chain is evidence against silent
alteration, not against a determined operator. The README says exactly this rather than
letting "tamper-evident" do more work than it has earned.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import ActorType, AuditEvent

logger = logging.getLogger(__name__)

# Serializes appends. Two transactions reading the same chain tip would otherwise both
# link to it and fork the chain, which verification would report as corruption when the
# real cause was concurrency. Held to the end of the caller's transaction.
CHAIN_LOCK_KEY = 8_675_309


def compute_entry_hash(
    *,
    event_id: uuid.UUID | str,
    occurred_at: datetime,
    actor_type: str,
    actor_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any],
    prev_hash: str | None,
) -> str:
    """Hash one entry's content together with its predecessor's hash.

    Pure, so the chain can be reasoned about and tested without a database.

    Serialization is canonical -- sorted keys, no incidental whitespace -- because a hash
    over a non-canonical encoding would change when nothing about the event did, and a
    verification that fails for cosmetic reasons trains people to ignore it.
    """
    canonical = json.dumps(
        {
            "id": str(event_id),
            "occurred_at": occurred_at.astimezone(UTC).isoformat(),
            "actor_type": str(actor_type),
            "actor_id": actor_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": payload,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _chain_tip(session: Session) -> str | None:
    """The most recent entry hash, or None when the chain has not started yet."""
    return session.execute(
        select(AuditEvent.entry_hash)
        .where(AuditEvent.entry_hash.is_not(None))
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    ).scalar_one_or_none()


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
    """Append one hash-chained audit event. Does not commit -- the caller owns the
    transaction.

    Not committing here is deliberate: an audit row must land in the same transaction as
    the change it describes, or the trail can disagree with the data it is auditing.
    """
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAIN_LOCK_KEY})

    # Make this transaction's own earlier appends visible to the tip query, so a batch of
    # events written in one transaction chains to itself rather than all linking to the
    # last committed row.
    session.flush()

    prev_hash = _chain_tip(session)
    event_id = uuid.uuid4()
    occurred_at = datetime.now(UTC)
    resolved_payload = payload or {}

    entry_hash = compute_entry_hash(
        event_id=event_id,
        occurred_at=occurred_at,
        actor_type=actor_type.value,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=resolved_payload,
        prev_hash=prev_hash,
    )

    event = AuditEvent(
        id=event_id,
        occurred_at=occurred_at,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=resolved_payload,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    session.add(event)
    logger.info(
        "audit event recorded",
        extra={
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor_type": actor_type.value,
            "entry_hash": entry_hash[:12],
        },
    )
    return event


@dataclass
class ChainProblem:
    seq: int
    event_id: str
    kind: str
    detail: str


@dataclass
class ChainReport:
    """Result of walking the chain. Every count is stated so the verdict has a denominator."""

    total_events: int
    chained_events: int
    unchained_legacy_events: int
    verified_events: int
    problems: list[ChainProblem] = field(default_factory=list)

    @property
    def intact(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "intact": self.intact,
            "total_events": self.total_events,
            "chained_events": self.chained_events,
            "unchained_legacy_events": self.unchained_legacy_events,
            "verified_events": self.verified_events,
            "problems": [
                {
                    "seq": p.seq,
                    "event_id": p.event_id,
                    "kind": p.kind,
                    "detail": p.detail,
                }
                for p in self.problems
            ],
            "caveat": (
                "Detects alteration of chained rows. Does not prevent it: anyone with "
                "database write access can recompute the chain forward from a tampered "
                "row. No signing key, no external anchor."
            ),
        }


def verify_chain(session: Session) -> ChainReport:
    """Walk the chain in sequence order and recompute every hash.

    Two independent checks per row, because they catch different tampering:

    - the row's own hash must match its content -- catches an edited row;
    - its `prev_hash` must equal the previous row's `entry_hash` -- catches a deleted or
      inserted row, which leaves each surviving row internally valid but unlinked.
    """
    rows = session.execute(
        select(AuditEvent).order_by(AuditEvent.seq.asc())
    ).scalars().all()

    problems: list[ChainProblem] = []
    verified = 0
    chained = 0
    legacy = 0
    expected_prev: str | None = None
    started = False

    for row in rows:
        if row.entry_hash is None:
            legacy += 1
            continue

        chained += 1
        if not started:
            # The chain begins at the first hashed row. Whatever it links to predates the
            # chain, so its prev_hash is not checked -- only everything after it.
            started = True
        elif row.prev_hash != expected_prev:
            problems.append(
                ChainProblem(
                    seq=row.seq,
                    event_id=str(row.id),
                    kind="broken_link",
                    detail=(
                        f"prev_hash {(row.prev_hash or 'None')[:16]}... does not match the "
                        f"preceding entry_hash {(expected_prev or 'None')[:16]}...; a row "
                        f"was deleted, inserted, or reordered here"
                    ),
                )
            )

        recomputed = compute_entry_hash(
            event_id=row.id,
            occurred_at=row.occurred_at,
            actor_type=row.actor_type.value,
            actor_id=row.actor_id,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            payload=row.payload,
            prev_hash=row.prev_hash,
        )
        if recomputed != row.entry_hash:
            problems.append(
                ChainProblem(
                    seq=row.seq,
                    event_id=str(row.id),
                    kind="content_altered",
                    detail=(
                        f"stored hash {row.entry_hash[:16]}... does not match a hash of "
                        f"this row's content ({recomputed[:16]}...); the row was edited "
                        f"after it was written"
                    ),
                )
            )
        else:
            verified += 1

        expected_prev = row.entry_hash

    report = ChainReport(
        total_events=len(rows),
        chained_events=chained,
        unchained_legacy_events=legacy,
        verified_events=verified,
        problems=problems,
    )
    if report.intact:
        logger.info(
            "audit chain verified",
            extra={"chained_events": chained, "legacy_events": legacy},
        )
    else:
        logger.error(
            "audit chain verification FAILED",
            extra={"problems": len(problems), "first_seq": problems[0].seq},
        )
    return report
