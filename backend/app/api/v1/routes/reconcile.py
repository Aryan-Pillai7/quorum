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
            db, limit_per_category=limit_per_category, settings=settings
        )

        for item in run.explained:
            # Every finding gets an audit row, explained or not: the gate decision is
            # itself an auditable event, and it happened whether or not a model replied.
            audit.record(
                db,
                action="agent.explained" if item.explanation else "agent.gated_only",
                entity_type="discrepancy",
                entity_id=item.discrepancy_id,
                actor_type=ActorType.AGENT if item.explanation else ActorType.SYSTEM,
                actor_id=run.model if item.explanation else None,
                payload={
                    "category_code": item.category_code,
                    "rule_id": item.rule_id,
                    "match_key": item.match_key,
                    "delta_minor": item.delta_minor,
                    "explanation": item.explanation,
                    "corrective_action": item.corrective_action,
                    "model_confidence": item.model_confidence,
                    "explanation_status": item.explanation_status,
                    "gate_decision": item.gate_decision,
                    "gate_reason": item.gate_reason,
                    "is_cold_start": item.is_cold_start,
                    "observations_needed": item.observations_needed,
                    "model": run.model,
                    "advisory_only": True,
                },
            )

        for batch in run.telemetry:
            audit.record(
                db,
                action="agent.batch",
                entity_type="discrepancy_category",
                entity_id=batch.category_code,
                actor_type=ActorType.AGENT,
                actor_id=run.model,
                payload={
                    "batch_size": batch.batch_size,
                    "latency_ms": batch.latency_ms,
                    "prompt_tokens": batch.prompt_tokens,
                    "output_tokens": batch.output_tokens,
                    "attempts": batch.attempts,
                    "explained": batch.explained,
                    "failed": batch.failed,
                    "error": batch.error,
                },
            )

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
        }

        narrated = _narrated_examples(run)

    return {
        "matching": {
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


def _narrated_examples(run) -> list[dict[str, Any]]:
    """One example per reason a finding is held back, for the 30-second demo walkthrough.

    The two reasons are genuinely different and the distinction is the pitch:
    a category can be barred by policy no matter how good its score gets, or it can be
    eligible in principle and simply not have earned it yet.
    """
    examples: list[dict[str, Any]] = []
    seen_reasons: set[str] = set()

    for item in run.explained:
        if item.explanation is None:
            continue
        reason = "policy_ceiling" if "not eligible" in item.gate_reason else "cold_start"
        if reason in seen_reasons:
            continue
        seen_reasons.add(reason)
        examples.append(
            {
                "why_held_back": reason,
                "discrepancy_id": item.discrepancy_id,
                "match_key": item.match_key,
                "category_code": item.category_code,
                "rule_id": item.rule_id,
                "delta_minor": item.delta_minor,
                "deterministic_summary": item.deterministic_summary,
                "agent_explanation": item.explanation,
                "agent_corrective_action": item.corrective_action,
                "model_confidence": item.model_confidence,
                "gate_decision": item.gate_decision,
                "gate_reason": item.gate_reason,
                "observations_needed": item.observations_needed,
            }
        )
        if len(seen_reasons) == 2:
            break
    return examples


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
