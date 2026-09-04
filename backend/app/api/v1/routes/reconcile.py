"""Reconciliation and dashboard endpoints.

`POST /v1/reconcile` is the whole pipeline in one call: match deterministically, explain
with the agent, gate every finding, and write the audit trail. The response reports what
actually happened, including the parts that failed.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import Settings, get_settings
from app.models import ActorType
from app.services import audit, reporting
from app.services.agent import explain as explain_service
from app.services.agent import explain_discrepancies
from app.services.matching import reconcile

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reconciliation"])


@router.post("/reconcile", summary="Run reconciliation, explain findings, and gate them")
def run_reconciliation(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    explain: bool = Query(
        default=True,
        description="Generate agent explanations. Set false for a deterministic-only run.",
    ),
    limit_per_category: int | None = Query(
        default=None,
        ge=1,
        description="Cap explanations per category. Useful for a fast demo run.",
    ),
    force_reexplain: bool = Query(
        default=False,
        description=(
            "Re-generate explanations that already exist. Off by default: an unchanged "
            "finding does not get a better explanation on a second pass, and re-running "
            "costs API quota."
        ),
    ),
) -> dict[str, Any]:
    """Match, explain, gate, audit.

    The deterministic matching runs first and completes regardless of the agent layer.
    If the agent is unavailable the response says so explicitly and still returns every
    finding with its gate decision -- reconciliation does not depend on a model replying.
    """
    result = reconcile(db)

    audit.record(
        db,
        action="reconcile.run",
        entity_type="reconciliation_run",
        entity_id="latest",
        actor_type=ActorType.SYSTEM,
        payload={
            "psp_rows": result.psp_rows,
            "bank_rows": result.bank_rows,
            "ledger_rows": result.ledger_rows,
            "match_records": result.match_records,
            "full_matches": result.full_matches,
            "partial_matches": result.partial_matches,
            "broken_matches": result.broken_matches,
            "discrepancy_findings": result.discrepancies,
            "findings_by_category": result.findings_by_category,
        },
    )
    db.commit()

    explanation_payload: dict[str, Any] = {
        "requested": explain,
        "available": False,
        "status": "explanations were not requested for this run",
        "model": None,
        "batches": [],
        "explained": 0,
        "unexplained": 0,
    }
    narrated: list[dict[str, Any]] = []

    if explain:
        run = explain_discrepancies(
            db,
            limit_per_category=limit_per_category,
            settings=settings,
            skip_explained=not force_reexplain,
        )

        explain_service.persist_explanation_run(db, run)

        db.commit()

        explanation_payload = {
            "requested": True,
            "available": run.agent_available,
            "status": run.agent_status,
            "model": run.model,
            "batches": [
                {
                    "category_code": b.category_code,
                    "batch_size": b.batch_size,
                    "latency_ms": b.latency_ms,
                    "prompt_tokens": b.prompt_tokens,
                    "output_tokens": b.output_tokens,
                    "attempts": b.attempts,
                    "explained": b.explained,
                    "failed": b.failed,
                    "error": b.error,
                }
                for b in run.telemetry
            ],
            "batch_count": len(run.telemetry),
            "total_latency_ms": run.total_latency_ms,
            "explained": run.explained_count,
            "unexplained": run.unexplained_count,
            "skipped_already_explained": run.skipped_already_explained,
        }


    # This run's deltas are often zero on a demo re-run, because everything is already
    # matched. The current state of the book is what the dashboard shows, so return both
    # rather than letting a row of zeros imply nothing was reconciled.
    state = reporting.dashboard_summary(db, settings=settings)
    # From the audit trail, so a cached re-run still has a story to tell.
    narrated = reporting.narrated_examples(db)

    return {
        "state": state["totals"],
        "this_run": {
            "psp_rows": result.psp_rows,
            "bank_rows": result.bank_rows,
            "ledger_rows": result.ledger_rows,
            "match_records": result.match_records,
            "full_matches": result.full_matches,
            "partial_matches": result.partial_matches,
            "broken_matches": result.broken_matches,
            "discrepancy_findings": result.discrepancies,
            "findings_by_category": result.findings_by_category,
            "note": (
                "Deltas for THIS run only. Zeros mean everything was already matched by "
                "an earlier run, not that there is nothing to reconcile -- see `state`."
            ),
        },
        "agent": explanation_payload,
        "gating": {
            "note": (
                "Every agent output is advisory. Nothing is auto-applied in Phase 3, and "
                "no category can reach AUTO_APPLY while its sample_size is below "
                "min_sample_size, whatever the model's own confidence says."
            ),
            "narrated_examples": narrated,
        },
        "audit": {
            "recorded": True,
            "note": (
                "Append-only by convention and code review. No hash chain and no database "
                "trigger blocking UPDATE or DELETE -- an audit trail, not a "
                "tamper-evident one (ADR-0008)."
            ),
        },
    }


@router.get("/dashboard", summary="Everything the dashboard renders, from stored rows")
def dashboard(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    return reporting.dashboard_summary(db, settings=settings)


@router.get("/discrepancies", summary="Findings with their agent explanation and gate decision")
def discrepancies(
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    category_code: str | None = Query(default=None),
) -> dict[str, Any]:
    rows = reporting.discrepancy_detail(db, limit=limit, category_code=category_code)
    return {"total": len(rows), "discrepancies": rows}


@router.get("/narrated", summary="Worked examples for the demo walkthrough")
def narrated(db: Session = Depends(get_db)) -> dict[str, Any]:
    """One explained finding per reason it is held back from automation."""
    return {"examples": reporting.narrated_examples(db)}
