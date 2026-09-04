"""Read-side aggregation for the dashboard.

Every figure here is computed from stored rows, never cached or estimated, and each one
carries its own definition in the response so a number on a screen cannot drift away from
what it actually means.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    AuditEvent,
    Discrepancy,
    DiscrepancyCategory,
    MatchRecord,
    MatchStatus,
    Transaction,
    TrustScore,
)
from app.services.trust import decide_gate

# What "at risk" means, stated once and echoed into the API response so the number on the
# dashboard always travels with its definition.
AT_RISK_DEFINITION = (
    "Absolute sum of the amount deltas on discrepancies the trust gate currently routes "
    "to a human. It is the money whose disposition is unresolved, not a loss estimate."
)


@dataclass(frozen=True)
class CategoryTrustView:
    code: str
    display_name: str
    severity: str
    auto_resolvable: bool
    score: float
    sample_size: int
    min_sample_size: int
    observations_needed: int
    is_cold_start: bool
    gate_decision: str
    gate_reason: str
    open_findings: int
    at_risk_minor: int


def _gate_for(category: DiscrepancyCategory, trust: TrustScore | None, settings: Settings):
    return decide_gate(
        score=trust.score if trust else 0,
        sample_size=trust.sample_size if trust else 0,
        correct_count=trust.correct_count if trust else 0,
        auto_apply_threshold=(
            trust.auto_apply_threshold if trust else settings.default_auto_apply_threshold
        ),
        review_threshold=trust.review_threshold if trust else settings.default_review_threshold,
        min_sample_size=trust.min_sample_size if trust else settings.default_min_sample_size,
        category_auto_resolvable=category.auto_resolvable,
    )


def category_trust_views(
    session: Session, *, settings: Settings | None = None, only_with_findings: bool = True
) -> list[CategoryTrustView]:
    """Per-category trust, gate decision, and the findings currently sitting behind it."""
    settings = settings or get_settings()

    counts = dict(
        session.execute(
            select(Discrepancy.category_code, func.count()).group_by(Discrepancy.category_code)
        ).all()
    )
    at_risk = dict(
        session.execute(
            select(Discrepancy.category_code, func.sum(func.abs(Discrepancy.delta_minor)))
            .group_by(Discrepancy.category_code)
        ).all()
    )

    rows = session.execute(
        select(DiscrepancyCategory, TrustScore)
        .outerjoin(TrustScore, TrustScore.category_code == DiscrepancyCategory.code)
        .order_by(DiscrepancyCategory.code)
    ).all()

    views: list[CategoryTrustView] = []
    for category, trust in rows:
        findings = counts.get(category.code, 0)
        if only_with_findings and findings == 0:
            continue
        gate = _gate_for(category, trust, settings)
        minimum = trust.min_sample_size if trust else settings.default_min_sample_size
        have = trust.sample_size if trust else 0
        views.append(
            CategoryTrustView(
                code=category.code,
                display_name=category.display_name,
                severity=category.severity,
                auto_resolvable=category.auto_resolvable,
                score=float(trust.score) if trust else 0.0,
                sample_size=have,
                min_sample_size=minimum,
                observations_needed=max(0, minimum - have),
                is_cold_start=gate.is_cold_start,
                gate_decision=gate.decision.value,
                gate_reason=gate.reason,
                open_findings=findings,
                at_risk_minor=int(at_risk.get(category.code) or 0),
            )
        )
    return views


def dashboard_summary(session: Session, *, settings: Settings | None = None) -> dict[str, Any]:
    """Everything the dashboard renders, in one query set."""
    settings = settings or get_settings()
    views = category_trust_views(session, settings=settings)

    status_counts = dict(
        session.execute(
            select(MatchRecord.status, func.count()).group_by(MatchRecord.status)
        ).all()
    )
    total_matches = sum(status_counts.values())
    full_matches = status_counts.get(MatchStatus.FULL, 0)

    transactions = session.execute(select(func.count()).select_from(Transaction)).scalar_one()
    findings = session.execute(select(func.count()).select_from(Discrepancy)).scalar_one()

    awaiting_review = [v for v in views if v.gate_decision == "HUMAN_REVIEW"]
    auto_eligible = [v for v in views if v.gate_decision == "AUTO_APPLY"]

    explained = session.execute(
        select(func.count(func.distinct(AuditEvent.entity_id))).where(
            AuditEvent.action == "agent.explained"
        )
    ).scalar_one()

    return {
        "totals": {
            "transactions": transactions,
            "match_records": total_matches,
            "full_matches": full_matches,
            "discrepancy_findings": findings,
            "explained_findings": explained,
        },
        "at_risk": {
            "definition": AT_RISK_DEFINITION,
            "awaiting_human_review_minor": sum(v.at_risk_minor for v in awaiting_review),
            "auto_eligible_minor": sum(v.at_risk_minor for v in auto_eligible),
            "findings_awaiting_review": sum(v.open_findings for v in awaiting_review),
            "currency": "INR",
        },
        "categories": [
            {
                "code": v.code,
                "display_name": v.display_name,
                "severity": v.severity,
                "score": v.score,
                "sample_size": v.sample_size,
                "min_sample_size": v.min_sample_size,
                "observations_needed": v.observations_needed,
                "is_cold_start": v.is_cold_start,
                "auto_resolvable": v.auto_resolvable,
                "gate_decision": v.gate_decision,
                "gate_reason": v.gate_reason,
                "open_findings": v.open_findings,
                "at_risk_minor": v.at_risk_minor,
            }
            for v in sorted(views, key=lambda v: v.open_findings, reverse=True)
        ],
        "caveats": [
            (
                "Every trust score is 0.0 at sample_size 0. No agent proposal has been "
                "confirmed or overridden by a human yet, so nothing has been scored. "
                "These are cold-start seeds, not measured accuracy."
            ),
            (
                "Because of that, no category can reach AUTO_APPLY: the gate refuses "
                "automation below min_sample_size regardless of score."
            ),
        ],
    }


def latest_explanations(session: Session, discrepancy_ids: list[str]) -> dict[str, dict]:
    """Most recent agent explanation per discrepancy, read from the audit trail.

    Explanations live in `audit_events` rather than on the discrepancy row: they are
    advisory commentary attached to a finding, not part of the finding itself, and
    keeping them out of the deterministic record means a model can never appear to have
    changed what the rules concluded.
    """
    if not discrepancy_ids:
        return {}

    rows = session.execute(
        select(AuditEvent.entity_id, AuditEvent.payload, AuditEvent.occurred_at)
        .where(
            AuditEvent.action == "agent.explained",
            AuditEvent.entity_id.in_(discrepancy_ids),
        )
        .order_by(AuditEvent.entity_id, AuditEvent.occurred_at.desc())
    ).all()

    latest: dict[str, dict] = {}
    for entity_id, payload, _occurred_at in rows:
        latest.setdefault(entity_id, payload)
    return latest


def discrepancy_detail(
    session: Session, *, limit: int = 50, category_code: str | None = None
) -> list[dict[str, Any]]:
    """Per-discrepancy rows for the dashboard drill-down and the narrated demo case."""
    query = (
        select(Discrepancy, MatchRecord, DiscrepancyCategory)
        .join(MatchRecord, MatchRecord.id == Discrepancy.match_record_id)
        .join(DiscrepancyCategory, DiscrepancyCategory.code == Discrepancy.category_code)
        .order_by(func.abs(Discrepancy.delta_minor).desc())
        .limit(limit)
    )
    if category_code:
        query = query.where(Discrepancy.category_code == category_code)

    rows = session.execute(query).all()
    explanations = latest_explanations(session, [str(d.id) for d, _m, _c in rows])

    detail: list[dict[str, Any]] = []
    for discrepancy, match, category in rows:
        payload = explanations.get(str(discrepancy.id), {})
        detail.append(
            {
                "discrepancy_id": str(discrepancy.id),
                "match_key": match.match_key,
                "category_code": discrepancy.category_code,
                "category_name": category.display_name,
                "severity": category.severity,
                "rule_id": discrepancy.rule_id,
                "delta_minor": discrepancy.delta_minor,
                "deterministic_summary": discrepancy.summary,
                "evidence": discrepancy.evidence,
                "match_status": match.status.value,
                "match_strategy": match.strategy.value,
                "explanation": payload.get("explanation"),
                "corrective_action": payload.get("corrective_action"),
                "model_confidence": payload.get("model_confidence"),
                "gate_decision": payload.get("gate_decision"),
                "gate_reason": payload.get("gate_reason"),
                "explained_by_model": payload.get("model"),
            }
        )
    return detail


def narrated_examples(session: Session, *, limit: int = 2) -> list[dict[str, Any]]:
    """One explained finding per reason it is being held back, for the demo walkthrough.

    Read from the audit trail rather than from a run, so the examples survive a cached
    re-run where nothing new was explained.

    The two reasons are genuinely different, and the distinction is the whole pitch:
    a category can be barred by policy no matter how good its score ever gets, or it can
    be eligible in principle and simply not have earned it yet.
    """
    rows = session.execute(
        select(AuditEvent.entity_id, AuditEvent.payload, AuditEvent.occurred_at)
        .where(AuditEvent.action == "agent.explained")
        .order_by(AuditEvent.occurred_at.desc())
    ).all()

    examples: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entity_id, payload, _occurred in rows:
        if not payload.get("explanation"):
            continue
        reason = (
            "policy_ceiling"
            if "not eligible" in (payload.get("gate_reason") or "")
            else "cold_start"
        )
        if reason in seen:
            continue
        seen.add(reason)
        examples.append(
            {
                "why_held_back": reason,
                "why_held_back_plain": (
                    "This category is never auto-applied at any trust score. It is a "
                    "policy ceiling, not a confidence judgement."
                    if reason == "policy_ceiling"
                    else (
                        "This category could be automated in principle, but has not yet "
                        "been observed enough times for its score to count as evidence."
                    )
                ),
                "discrepancy_id": entity_id,
                "match_key": payload.get("match_key"),
                "category_code": payload.get("category_code"),
                "rule_id": payload.get("rule_id"),
                "delta_minor": payload.get("delta_minor"),
                "agent_explanation": payload.get("explanation"),
                "agent_corrective_action": payload.get("corrective_action"),
                "model_confidence": payload.get("model_confidence"),
                "model": payload.get("model"),
                "gate_decision": payload.get("gate_decision"),
                "gate_reason": payload.get("gate_reason"),
                "observations_needed": payload.get("observations_needed"),
                "advisory_only": True,
            }
        )
        if len(examples) >= limit:
            break
    return examples
