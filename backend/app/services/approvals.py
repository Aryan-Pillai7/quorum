"""The approval loop: a human acts, a second human checks, and only then trust moves.

Three ideas, each of which exists to stop a specific failure:

1. **Approving is not evidence.** An approval clears a finding operationally the moment
   it is made. It affects no trust score. A system that learned from its own unexamined
   approvals would be measuring how often people click accept, not how often it is right.

2. **Auditing is evidence.** A second person confirms the first was correct, and that --
   and only that -- recalibrates the category (ADR-0025).

3. **What gets audited is not left to chance where it matters.** Any approval whose audit
   could change what the category is allowed to do is audited at 100%. The rest are
   sampled at a baseline rate so the measurement stays unbiased (ADR-0025).

Recalibration is an exponential moving average rather than a running ratio (ADR-0026), and
it goes through the same write-then-invalidate ordering as every other trust write, using
`trust_store` rather than a second path (ADR-0023).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cache import redis as cache
from app.config import Settings, get_settings
from app.core.errors import QuorumError
from app.models import (
    ActorType,
    Approval,
    ApprovalDecision,
    AuditStatus,
    Discrepancy,
    DiscrepancyCategory,
    TrustScore,
)
from app.services import audit
from app.services.trust import GateDecision, gate_for_trust_row
from app.services.trust_store import cache_key, read_trust_authoritative

logger = logging.getLogger(__name__)


class ApprovalError(QuorumError):
    code = "approval_error"
    http_status = 409


class NotFoundError(QuorumError):
    code = "not_found"
    http_status = 404


@dataclass(frozen=True)
class AuditSelection:
    selected: bool
    reason: str


@dataclass(frozen=True)
class RecalibrationResult:
    """What an audit did to a category's trust, and to what the gate now permits."""

    category_code: str
    previous_score: float
    new_score: float
    sample_size: int
    correct_count: int
    previous_gate: str
    new_gate: str
    cache_invalidated: bool
    cache_error: str | None = None

    @property
    def gate_changed(self) -> bool:
        return self.previous_gate != self.new_gate


def _gate_for_row(row: TrustScore, category: DiscrepancyCategory) -> GateDecision:
    """The gate decision for a category as it stands. No amount, so this is the
    category-level verdict rather than a verdict about one transaction."""
    evaluation, _decay = gate_for_trust_row(
        score=row.score,
        sample_size=row.sample_size,
        correct_count=row.correct_count,
        auto_apply_threshold=row.auto_apply_threshold,
        review_threshold=row.review_threshold,
        min_sample_size=row.min_sample_size,
        category_auto_resolvable=category.auto_resolvable,
        last_audit_at=row.last_evaluated_at,
    )
    return evaluation.decision


def _ema(previous: float, outcome: float, alpha: float) -> float:
    return round(alpha * outcome + (1.0 - alpha) * previous, 4)


def select_for_audit(
    session: Session,
    *,
    approval_id: uuid.UUID,
    category_code: str,
    settings: Settings | None = None,
) -> AuditSelection:
    """Decide whether this approval gets audited.

    Two rules, in order:

    1. **Would auditing it change what the category is allowed to do?** Simulated both
       ways -- as if the audit found it correct, and as if it found it wrong. If either
       outcome moves the gate, it is audited. This is the case where being wrong is most
       expensive, so it is never left to a sample.

    2. Otherwise, a baseline sample. The draw is a hash of the approval id, not a random
       number: the same approval always gets the same answer, so a caller cannot retry
       until it escapes the sample, and the decision is reproducible during an
       investigation.
    """
    settings = settings or get_settings()

    row = read_trust_authoritative(session, category_code)
    category = session.get(DiscrepancyCategory, category_code)
    if row is None or category is None:
        return AuditSelection(True, "category has no trust row yet; audited to establish one")

    current = _gate_for_row(row, category)
    alpha = settings.trust_ema_alpha

    for outcome, label in ((1.0, "correct"), (0.0, "incorrect")):
        hypothetical = TrustScore(
            category_code=row.category_code,
            score=Decimal(str(_ema(float(row.score), outcome, alpha))),
            sample_size=row.sample_size + 1,
            correct_count=row.correct_count + int(outcome),
            auto_apply_threshold=row.auto_apply_threshold,
            review_threshold=row.review_threshold,
            min_sample_size=row.min_sample_size,
        )
        if _gate_for_row(hypothetical, category) is not current:
            return AuditSelection(
                True,
                f"auditing this approval could move {category_code} out of {current.value} "
                f"if the verdict is {label}; gate-moving approvals are always audited",
            )

    # Deterministic draw in [0, 1) from the approval id.
    digest = hashlib.sha256(str(approval_id).encode("utf-8")).hexdigest()
    draw = int(digest[:8], 16) / 0xFFFFFFFF
    if draw < settings.audit_baseline_rate:
        return AuditSelection(
            True,
            f"baseline sample: draw {draw:.4f} below rate {settings.audit_baseline_rate}",
        )
    return AuditSelection(
        False,
        f"baseline sample: draw {draw:.4f} at or above rate "
        f"{settings.audit_baseline_rate}; cleared operationally but not counted as evidence",
    )


def approve(
    session: Session,
    *,
    discrepancy_id: uuid.UUID,
    decision: ApprovalDecision,
    approver_id: str,
    final_action: str | None = None,
    settings: Settings | None = None,
) -> Approval:
    """Record a human verdict on an agent-drafted correction.

    Clears the finding operationally. Moves no trust score -- that needs an audit.
    """
    settings = settings or get_settings()

    discrepancy = session.get(Discrepancy, discrepancy_id)
    if discrepancy is None:
        raise NotFoundError(f"no discrepancy {discrepancy_id}")

    existing = session.execute(
        select(Approval).where(Approval.discrepancy_id == discrepancy_id)
    ).scalar_one_or_none()
    if existing is not None:
        raise ApprovalError(
            f"discrepancy {discrepancy_id} was already approved by "
            f"{existing.approver_id}; a second approval would double-count as evidence",
            details={"approval_id": str(existing.id)},
        )

    proposed = _proposed_action_for(session, discrepancy_id)
    if decision is ApprovalDecision.EDITED_APPROVED and not (final_action or "").strip():
        raise ApprovalError("EDITED_APPROVED requires the edited action text")
    resolved_action = (final_action or "").strip() or proposed or "(no action recorded)"

    approval_id = uuid.uuid4()
    selection = select_for_audit(
        session,
        approval_id=approval_id,
        category_code=discrepancy.category_code,
        settings=settings,
    )

    approval = Approval(
        id=approval_id,
        discrepancy_id=discrepancy_id,
        category_code=discrepancy.category_code,
        decision=decision,
        proposed_action=proposed,
        final_action=resolved_action,
        approver_id=approver_id,
        approved_at=datetime.now(UTC),
        audit_status=AuditStatus.PENDING if selection.selected else AuditStatus.NOT_SELECTED,
        audit_selection_reason=selection.reason,
    )
    session.add(approval)

    audit.record(
        session,
        action="approval.recorded",
        entity_type="approval",
        entity_id=str(approval_id),
        actor_type=ActorType.USER,
        actor_id=approver_id,
        payload={
            "discrepancy_id": str(discrepancy_id),
            "category_code": discrepancy.category_code,
            "decision": decision.value,
            "edited": decision is ApprovalDecision.EDITED_APPROVED,
            "selected_for_audit": selection.selected,
            "audit_selection_reason": selection.reason,
            "affects_trust": False,
        },
    )
    session.commit()

    logger.info(
        "approval recorded",
        extra={
            "approval_id": str(approval_id),
            "category": discrepancy.category_code,
            "decision": decision.value,
            "selected_for_audit": selection.selected,
        },
    )
    return approval


def _proposed_action_for(session: Session, discrepancy_id: uuid.UUID) -> str | None:
    """The agent's drafted action, from the audit trail where it was recorded."""
    from app.services.reporting import latest_explanations

    payload = latest_explanations(session, [str(discrepancy_id)]).get(str(discrepancy_id), {})
    return payload.get("corrective_action")


def audit_approval(
    session: Session,
    *,
    approval_id: uuid.UUID,
    was_correct: bool,
    auditor_id: str,
    note: str | None = None,
    settings: Settings | None = None,
) -> RecalibrationResult:
    """Verify an approval, and recalibrate the category on the result.

    This is the only path that moves a trust score. Postgres is written and committed
    before the cache key is dropped, matching ADR-0023's ordering, and gating still reads
    Postgres directly -- no second gating path is created here.
    """
    settings = settings or get_settings()

    approval = session.get(Approval, approval_id)
    if approval is None:
        raise NotFoundError(f"no approval {approval_id}")
    if approval.audit_status is AuditStatus.NOT_SELECTED:
        raise ApprovalError(
            f"approval {approval_id} was not selected for audit, so auditing it would "
            f"bias the sample. Selection reason: {approval.audit_selection_reason}"
        )
    if approval.counts_toward_trust:
        raise ApprovalError(
            f"approval {approval_id} was already audited by {approval.auditor_id}; "
            f"re-auditing would count one observation twice"
        )

    row = read_trust_authoritative(session, approval.category_code)
    category = session.get(DiscrepancyCategory, approval.category_code)
    if row is None or category is None:
        raise NotFoundError(f"no trust score for category {approval.category_code}")

    previous_score = float(row.score)
    previous_gate = _gate_for_row(row, category).value

    approval.audit_status = AuditStatus.CORRECT if was_correct else AuditStatus.INCORRECT
    approval.auditor_id = auditor_id
    approval.audited_at = datetime.now(UTC)
    approval.audit_note = note

    row.sample_size += 1
    if was_correct:
        row.correct_count += 1
    row.score = Decimal(str(_ema(previous_score, 1.0 if was_correct else 0.0,
                                settings.trust_ema_alpha)))
    row.last_evaluated_at = datetime.now(UTC)

    new_gate = _gate_for_row(row, category).value

    audit.record(
        session,
        action="approval.audited",
        entity_type="approval",
        entity_id=str(approval_id),
        actor_type=ActorType.USER,
        actor_id=auditor_id,
        payload={
            "category_code": approval.category_code,
            "was_correct": was_correct,
            "previous_score": previous_score,
            "new_score": float(row.score),
            "sample_size": row.sample_size,
            "correct_count": row.correct_count,
            "previous_gate": previous_gate,
            "new_gate": new_gate,
            "ema_alpha": settings.trust_ema_alpha,
            "affects_trust": True,
            "note": note,
        },
    )

    # Postgres first, committed, then the cache (ADR-0023).
    session.commit()

    invalidated, cache_error = _invalidate(approval.category_code)

    result = RecalibrationResult(
        category_code=approval.category_code,
        previous_score=previous_score,
        new_score=float(row.score),
        sample_size=row.sample_size,
        correct_count=row.correct_count,
        previous_gate=previous_gate,
        new_gate=new_gate,
        cache_invalidated=invalidated,
        cache_error=cache_error,
    )
    if result.gate_changed:
        logger.warning(
            "trust gate state changed",
            extra={
                "category": result.category_code,
                "from": previous_gate,
                "to": new_gate,
                "sample_size": result.sample_size,
                "score": result.new_score,
            },
        )
    return result


def _invalidate(category_code: str) -> tuple[bool, str | None]:
    """Drop the cached score. Never raises: a cache problem must not undo a committed
    write, and gating does not read the cache anyway (ADR-0023)."""
    key = cache_key(category_code)
    try:
        if cache.cache_delete(key):
            return True, None
    except Exception as exc:  # noqa: BLE001 - invalidation must never propagate
        logger.error(
            "trust cache invalidation failed after recalibration; gating is unaffected "
            "because it reads Postgres directly (ADR-0023)",
            extra={"cache_key": key, "error": str(exc)[:200]},
        )
        return False, str(exc)[:200]
    logger.error(
        "trust cache invalidation failed after recalibration; gating is unaffected "
        "because it reads Postgres directly (ADR-0023)",
        extra={"cache_key": key},
    )
    return False, "redis delete did not complete"
