"""Approval and audit endpoints.

The two writes are separate endpoints because they are separate decisions by separate
people. Approving clears a finding; auditing an approval is what makes it evidence and
moves a trust score (ADR-0025). Collapsing them into one call would let the system learn
from its own unexamined output.

Both require the operator token (ADR-0028). Reads stay open for the dashboard.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_operator_token
from app.config import Settings, get_settings
from app.models import Approval, ApprovalDecision, AuditStatus
from app.services import approvals as approval_service

router = APIRouter(prefix="/approvals", tags=["approvals"])


class ApprovalRequest(BaseModel):
    discrepancy_id: uuid.UUID
    decision: ApprovalDecision
    approver_id: str = Field(min_length=1, max_length=128)
    final_action: str | None = Field(
        default=None,
        description=(
            "Required for EDITED_APPROVED. Ignored otherwise -- the agent's draft is "
            "recorded either way, so the trail shows what the approver was shown."
        ),
    )


class AuditRequest(BaseModel):
    was_correct: bool = Field(
        description="Whether the approved action was in fact the right call."
    )
    auditor_id: str = Field(min_length=1, max_length=128)
    note: str | None = None


@router.post("", summary="Approve, edit-and-approve, or reject a drafted correction")
def create_approval(
    body: ApprovalRequest,
    _token: Annotated[str, Depends(require_operator_token)],
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Clears the finding operationally. Moves no trust score.

    The response says so explicitly, because "approved" reading as "the system learned
    from this" is exactly the confusion ADR-0025 exists to prevent.
    """
    approval = approval_service.approve(
        db,
        discrepancy_id=body.discrepancy_id,
        decision=body.decision,
        approver_id=body.approver_id,
        final_action=body.final_action,
        settings=settings,
    )
    return {
        "approval_id": str(approval.id),
        "discrepancy_id": str(approval.discrepancy_id),
        "category_code": approval.category_code,
        "decision": approval.decision.value,
        "final_action": approval.final_action,
        "audit_status": approval.audit_status.value,
        "selected_for_audit": approval.audit_status is AuditStatus.PENDING,
        "audit_selection_reason": approval.audit_selection_reason,
        "affects_trust": False,
        "note": (
            "Recorded and cleared operationally. Trust scores are unchanged: only an "
            "audited approval counts as evidence (ADR-0025)."
        ),
    }


@router.post("/{approval_id}/audit", summary="Verify an approval and recalibrate trust")
def audit_approval(
    approval_id: uuid.UUID,
    body: AuditRequest,
    _token: Annotated[str, Depends(require_operator_token)],
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """The only endpoint that moves a trust score."""
    result = approval_service.audit_approval(
        db,
        approval_id=approval_id,
        was_correct=body.was_correct,
        auditor_id=body.auditor_id,
        note=body.note,
        settings=settings,
    )
    return {
        "approval_id": str(approval_id),
        "category_code": result.category_code,
        "was_correct": body.was_correct,
        "trust": {
            "previous_score": result.previous_score,
            "new_score": result.new_score,
            "sample_size": result.sample_size,
            "correct_count": result.correct_count,
            "ema_alpha": settings.trust_ema_alpha,
        },
        "gate": {
            "previous": result.previous_gate,
            "current": result.new_gate,
            "changed": result.gate_changed,
        },
        "cache": {
            "invalidated": result.cache_invalidated,
            "error": result.cache_error,
            "note": (
                "Gating reads Postgres directly, so a failed invalidation cannot make "
                "the system more permissive (ADR-0023)."
            ),
        },
        "affects_trust": True,
    }


@router.get("", summary="Approvals and their audit state")
def list_approvals(
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    category_code: str | None = Query(default=None),
    audit_status: AuditStatus | None = Query(default=None),
) -> dict[str, Any]:
    query = select(Approval).order_by(Approval.approved_at.desc()).limit(limit)
    if category_code:
        query = query.where(Approval.category_code == category_code)
    if audit_status:
        query = query.where(Approval.audit_status == audit_status)

    rows = db.execute(query).scalars().all()
    counted = sum(1 for r in rows if r.counts_toward_trust)
    return {
        "total": len(rows),
        "counting_toward_trust": counted,
        "approvals": [
            {
                "approval_id": str(r.id),
                "discrepancy_id": str(r.discrepancy_id),
                "category_code": r.category_code,
                "decision": r.decision.value,
                "approver_id": r.approver_id,
                "approved_at": r.approved_at.isoformat(),
                "audit_status": r.audit_status.value,
                "audit_selection_reason": r.audit_selection_reason,
                "auditor_id": r.auditor_id,
                "counts_toward_trust": r.counts_toward_trust,
            }
            for r in rows
        ],
    }
