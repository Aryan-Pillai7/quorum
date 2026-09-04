"""Trust score reads.

This is the endpoint that makes the gate observable. `decide_gate` is exercised here on
every request, so the automation policy is inspectable before Phase 3 wires it into the
write path -- rather than sitting unused until the moment it first matters.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db
from app.config import Settings, get_settings
from app.models import DiscrepancyCategory, TrustScore
from app.schemas.trust import TrustCategoryListResponse, TrustCategoryResponse
from app.services.trust import gate_for_trust_row

router = APIRouter(prefix="/trust", tags=["trust"])


@router.get(
    "/categories",
    response_model=TrustCategoryListResponse,
    summary="Discrepancy taxonomy with current trust scores and gate decisions",
)
def list_trust_categories(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TrustCategoryListResponse:
    """Every discrepancy category, its trust score, and what the gate currently permits.

    Read straight from Postgres. The Redis cache is deliberately not consulted here:
    this endpoint exists to show the true state, and a cache would only add a way for it
    to be subtly wrong.
    """
    categories = (
        db.execute(
            select(DiscrepancyCategory)
            .options(selectinload(DiscrepancyCategory.trust_score))
            .order_by(DiscrepancyCategory.code)
        )
        .scalars()
        .all()
    )

    return TrustCategoryListResponse(
        categories=[_to_response(c, c.trust_score, settings) for c in categories],
        total=len(categories),
    )


def _to_response(
    category: DiscrepancyCategory, trust: TrustScore | None, settings: Settings
) -> TrustCategoryResponse:
    # A category with no trust row falls back to configured defaults rather than being
    # omitted: an unscored category still has a gate decision, and it is HUMAN_REVIEW.
    score = trust.score if trust else 0
    sample_size = trust.sample_size if trust else 0
    correct_count = trust.correct_count if trust else 0
    auto_threshold = trust.auto_apply_threshold if trust else settings.default_auto_apply_threshold
    review_threshold = trust.review_threshold if trust else settings.default_review_threshold
    min_sample = trust.min_sample_size if trust else settings.default_min_sample_size

    evaluation, _decay = gate_for_trust_row(
        score=score,
        sample_size=sample_size,
        correct_count=correct_count,
        auto_apply_threshold=auto_threshold,
        review_threshold=review_threshold,
        min_sample_size=min_sample,
        category_auto_resolvable=category.auto_resolvable,
        last_audit_at=trust.last_evaluated_at if trust else None,
        grace_days=settings.trust_decay_grace_days,
        decay_days=settings.trust_decay_days,
    )

    return TrustCategoryResponse(
        code=category.code,
        display_name=category.display_name,
        description=category.description,
        severity=category.severity,
        tolerance_minor=category.tolerance_minor,
        auto_resolvable=category.auto_resolvable,
        score=evaluation.score,
        sample_size=sample_size,
        correct_count=correct_count,
        min_sample_size=min_sample,
        auto_apply_threshold=auto_threshold,
        review_threshold=review_threshold,
        is_cold_start=evaluation.is_cold_start,
        gate_decision=evaluation.decision.value,
        gate_reason=evaluation.reason,
    )
