"""Category drill-down: the findings behind a dashboard row.

Two endpoints with deliberately different costs, and the difference is visible in their
shapes:

- `GET  /v1/categories/{code}/findings` reads stored rows. Explanations come back inline
  because they are already in the audit trail (ADR-0029). No model is called.
- `POST /v1/categories/{code}/explain` spends API quota. It requires the operator token
  and batches the whole category into one call by reusing the Phase 3 service, so this
  never becomes a per-finding call path.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_operator_token
from app.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.models import Discrepancy, DiscrepancyCategory, TrustScore
from app.services import reporting
from app.services.agent import explain_discrepancies
from app.services.agent.explain import persist_explanation_run

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("/{category_code}/findings", summary="The individual findings behind a category")
def category_findings(
    category_code: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    """Every finding in a category, with its deterministic evidence and stored explanation.

    Explanations are included inline rather than fetched per row. Measured on the current
    fixture, they add about 7.5 KB to a 27 KB category payload -- a second round-trip to
    save that would buy nothing and would put a loading state on an action whose whole
    point is that the answer already exists (ADR-0029).

    Nothing here calls a model.
    """
    category = db.get(DiscrepancyCategory, category_code)
    if category is None:
        raise NotFoundError(f"no discrepancy category {category_code!r}")

    findings = reporting.discrepancy_detail(db, limit=limit, category_code=category_code)

    total = db.execute(
        select(func.count())
        .select_from(Discrepancy)
        .where(Discrepancy.category_code == category_code)
    ).scalar_one()

    trust = db.execute(
        select(TrustScore).where(TrustScore.category_code == category_code)
    ).scalar_one_or_none()

    views = reporting.category_trust_views(db, settings=settings, only_with_findings=False)
    view = next((v for v in views if v.code == category_code), None)

    explained = sum(1 for f in findings if f["has_explanation"])

    return {
        "category": {
            "code": category.code,
            "display_name": category.display_name,
            "description": category.description,
            "severity": category.severity,
            "tolerance_minor": category.tolerance_minor,
            "auto_resolvable": category.auto_resolvable,
        },
        "trust": {
            "score": float(trust.score) if trust else 0.0,
            "sample_size": trust.sample_size if trust else 0,
            "min_sample_size": trust.min_sample_size if trust else 0,
            "gate_decision": view.gate_decision if view else "HUMAN_REVIEW",
            "gate_reason": view.gate_reason if view else "",
        },
        "counts": {
            "total_findings": total,
            "returned": len(findings),
            "explained": explained,
            "unexplained": len(findings) - explained,
        },
        "findings": findings,
        "note": (
            "Deterministic evidence is computed by the matching engine -- pure field "
            "comparison, no model involved (ADR-0004). Explanations shown here were "
            "generated in an earlier batch and read from the audit trail; opening this "
            "view calls no model."
        ),
    }


@router.post(
    "/{category_code}/explain",
    summary="Generate explanations for this category's unexplained findings",
)
def explain_category(
    category_code: str,
    _token: Annotated[str, Depends(require_operator_token)],
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """One batched call covering every unexplained finding in this category.

    Reuses the Phase 3 service rather than adding a second generation path: the only
    difference from a reconcile-time run is which categories it covers. Findings that
    already have an explanation are skipped, so re-pressing this costs nothing.

    Behind the operator token because it spends daily API quota, which is a real
    operational cost rather than a read.
    """
    category = db.get(DiscrepancyCategory, category_code)
    if category is None:
        raise NotFoundError(f"no discrepancy category {category_code!r}")

    run = explain_discrepancies(
        db,
        settings=settings,
        category_codes=[category_code],
        skip_explained=True,
    )
    persist_explanation_run(db, run)
    db.commit()

    batch = run.telemetry[0] if run.telemetry else None
    return {
        "category_code": category_code,
        "agent_available": run.agent_available,
        "agent_status": run.agent_status,
        "model": run.model,
        "already_explained_skipped": run.skipped_already_explained,
        "explained": run.explained_count,
        "unexplained": run.unexplained_count,
        "api_calls": len(run.telemetry),
        "batch": (
            {
                "batch_size": batch.batch_size,
                "latency_ms": batch.latency_ms,
                "prompt_tokens": batch.prompt_tokens,
                "output_tokens": batch.output_tokens,
                "attempts": batch.attempts,
                "error": batch.error,
            }
            if batch
            else None
        ),
        "note": (
            "One API call for the whole category, never one per finding (ADR-0018). "
            "Findings that already had an explanation were skipped, so repeating this "
            "costs no quota."
        ),
    }
