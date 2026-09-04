"""Explanation agent: batch by category, validate hard, gate everything.

The order matters and is the whole point of this module:

    deterministic finding -> model explanation -> validation -> trust gate -> advisory

The model never decides anything. It restates a finding the rules already made, and its
output is advisory until the trust gate says otherwise. In Phase 3 the gate says otherwise
for nothing at all, because every category is at zero observations -- and the API reports
that plainly rather than implying automation is happening.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Discrepancy, DiscrepancyCategory, MatchRecord, TrustScore
from app.services.agent.client import (
    AgentUnavailableError,
    GeminiClient,
    agent_availability,
    parse_json_payload,
)
from app.services.agent.prompts import (
    SYSTEM_INSTRUCTION,
    DiscrepancyPromptItem,
    build_batch_prompt,
)
from app.services.agent.schema import RESPONSE_SCHEMA, AgentBatchResponse, AgentExplanation
from app.services.trust import GateEvaluation, decide_gate

logger = logging.getLogger(__name__)


@dataclass
class ExplainedDiscrepancy:
    """A deterministic finding, optionally explained, always gated."""

    discrepancy_id: str
    match_key: str
    category_code: str
    rule_id: str
    delta_minor: int
    deterministic_summary: str
    evidence: dict[str, Any]

    gate_decision: str
    gate_reason: str
    is_cold_start: bool
    observations_needed: int

    explanation: str | None = None
    corrective_action: str | None = None
    model_confidence: str | None = None
    explanation_status: str = "not_requested"


@dataclass
class BatchTelemetry:
    """Per-category call accounting. Logged and returned, never estimated."""

    category_code: str
    batch_size: int
    latency_ms: float
    prompt_tokens: int | None
    output_tokens: int | None
    attempts: int
    explained: int
    failed: int
    error: str | None = None


@dataclass
class ExplanationRun:
    agent_available: bool
    agent_status: str
    model: str | None
    explained: list[ExplainedDiscrepancy] = field(default_factory=list)
    telemetry: list[BatchTelemetry] = field(default_factory=list)

    @property
    def total_latency_ms(self) -> float:
        return round(sum(t.latency_ms for t in self.telemetry), 1)

    @property
    def explained_count(self) -> int:
        return sum(1 for e in self.explained if e.explanation is not None)

    @property
    def unexplained_count(self) -> int:
        return len(self.explained) - self.explained_count


def validate_batch_response(
    payload: dict[str, Any], requested_ids: set[str]
) -> tuple[dict[str, AgentExplanation], list[str]]:
    """Validate a model response against the ids that were actually requested.

    Returns (accepted by id, problems). Two failure modes are caught here, and both are
    counted rather than smoothed over:

    - an id the model invented, or returned twice: dropped. Attaching an explanation to
      the wrong transaction is worse than attaching none.
    - an id that was requested and never came back: reported as unexplained.

    Called from `explain_discrepancies` on every batch. It is the only path by which model
    output reaches a response.
    """
    problems: list[str] = []
    accepted: dict[str, AgentExplanation] = {}

    try:
        parsed = AgentBatchResponse.model_validate(payload)
    except ValidationError as exc:
        return {}, [f"response failed schema validation: {exc.error_count()} error(s)"]

    for item in parsed.explanations:
        if item.discrepancy_id not in requested_ids:
            problems.append(f"model returned unknown id {item.discrepancy_id!r}; dropped")
            continue
        if item.discrepancy_id in accepted:
            problems.append(f"model returned duplicate id {item.discrepancy_id!r}; kept first")
            continue
        accepted[item.discrepancy_id] = item

    for missing in sorted(requested_ids - set(accepted)):
        problems.append(f"no explanation returned for {missing}")

    return accepted, problems


def _gate_for(category: DiscrepancyCategory, trust: TrustScore | None, settings: Settings):
    """Trust gate evaluation for a category, with the fallback for an unscored one."""
    return decide_gate(
        score=trust.score if trust else 0,
        sample_size=trust.sample_size if trust else 0,
        correct_count=trust.correct_count if trust else 0,
        auto_apply_threshold=(
            trust.auto_apply_threshold if trust else settings.default_auto_apply_threshold
        ),
        review_threshold=(
            trust.review_threshold if trust else settings.default_review_threshold
        ),
        min_sample_size=trust.min_sample_size if trust else settings.default_min_sample_size,
        category_auto_resolvable=category.auto_resolvable,
    )


def _observations_needed(trust: TrustScore | None, settings: Settings) -> int:
    minimum = trust.min_sample_size if trust else settings.default_min_sample_size
    have = trust.sample_size if trust else 0
    return max(0, minimum - have)


def explain_discrepancies(
    session: Session,
    *,
    limit_per_category: int | None = None,
    settings: Settings | None = None,
) -> ExplanationRun:
    """Explain every discrepancy in the database, batched one call per category.

    Runs the trust gate for every discrepancy regardless of whether the agent layer is
    available, so gating is never contingent on the model answering.
    """
    settings = settings or get_settings()
    available, status = agent_availability(settings)

    rows = session.execute(
        select(Discrepancy, MatchRecord, DiscrepancyCategory, TrustScore)
        .join(MatchRecord, MatchRecord.id == Discrepancy.match_record_id)
        .join(DiscrepancyCategory, DiscrepancyCategory.code == Discrepancy.category_code)
        .outerjoin(TrustScore, TrustScore.category_code == Discrepancy.category_code)
        .order_by(Discrepancy.category_code, MatchRecord.match_key)
    ).all()

    by_category: dict[str, list[tuple]] = {}
    for discrepancy, match, category, trust in rows:
        by_category.setdefault(category.code, []).append((discrepancy, match, category, trust))

    run = ExplanationRun(
        agent_available=available,
        agent_status=status,
        model=settings.gemini_model if available else None,
    )

    client: GeminiClient | None = None
    if available:
        try:
            client = GeminiClient(settings)
        except AgentUnavailableError as exc:
            run.agent_available = False
            run.agent_status = exc.message
            client = None

    for batch_index, (category_code, entries) in enumerate(sorted(by_category.items())):
        # Pace after the first batch: the free-tier limit is per minute, and nine batches
        # in quick succession trips it.
        if batch_index and client is not None and settings.gemini_inter_batch_seconds:
            time.sleep(settings.gemini_inter_batch_seconds)

        batch = entries[:limit_per_category] if limit_per_category else entries

        # The gate runs first and unconditionally. Whether a finding may be acted on is a
        # property of Quorum's measured trust in the category, not of the model replying.
        gated: dict[str, tuple] = {}
        for discrepancy, match, category, trust in batch:
            evaluation: GateEvaluation = _gate_for(category, trust, settings)
            gated[str(discrepancy.id)] = (discrepancy, match, category, trust, evaluation)

        items = [
            DiscrepancyPromptItem(
                discrepancy_id=str(discrepancy.id),
                rule_id=discrepancy.rule_id,
                summary=discrepancy.summary,
                evidence=discrepancy.evidence,
                delta_minor=discrepancy.delta_minor,
                match_key=match.match_key,
            )
            for discrepancy, match, _category, _trust, _gate in gated.values()
        ]

        accepted: dict[str, AgentExplanation] = {}
        telemetry: BatchTelemetry | None = None

        if client is not None and items:
            category_row = batch[0][2]
            prompt = build_batch_prompt(
                category_code=category_code,
                category_display_name=category_row.display_name,
                category_description=category_row.description,
                items=items,
            )
            started = time.perf_counter()
            try:
                call = client.generate_json(
                    system_instruction=SYSTEM_INSTRUCTION,
                    prompt=prompt,
                    schema=RESPONSE_SCHEMA,
                )
                payload = parse_json_payload(call.text)
                accepted, problems = validate_batch_response(payload, set(gated))
                if problems:
                    logger.warning(
                        "agent batch had validation problems",
                        extra={"category": category_code, "problems": problems[:10]},
                    )
                telemetry = BatchTelemetry(
                    category_code=category_code,
                    batch_size=len(items),
                    latency_ms=call.latency_ms,
                    prompt_tokens=call.prompt_tokens,
                    output_tokens=call.output_tokens,
                    attempts=call.attempts,
                    explained=len(accepted),
                    failed=len(items) - len(accepted),
                )
            except (AgentUnavailableError, ValueError) as exc:
                # One category failing must not lose the other categories' explanations,
                # and must not lose any gate decisions at all.
                message = getattr(exc, "message", str(exc))
                telemetry = BatchTelemetry(
                    category_code=category_code,
                    batch_size=len(items),
                    latency_ms=round((time.perf_counter() - started) * 1000, 1),
                    prompt_tokens=None,
                    output_tokens=None,
                    attempts=settings.gemini_max_retries,
                    explained=0,
                    failed=len(items),
                    error=message[:300],
                )
                logger.error(
                    "agent batch failed",
                    extra={"category": category_code, "error": message[:300]},
                )

        if telemetry is not None:
            run.telemetry.append(telemetry)
            logger.info(
                "agent batch completed",
                extra={
                    "category": telemetry.category_code,
                    "batch_size": telemetry.batch_size,
                    "latency_ms": telemetry.latency_ms,
                    "explained": telemetry.explained,
                    "failed": telemetry.failed,
                },
            )

        for discrepancy_id, (discrepancy, match, _category, trust, gate) in gated.items():
            explanation = accepted.get(discrepancy_id)
            if explanation is not None:
                explanation_status = "explained"
            elif client is None:
                explanation_status = "agent_unavailable"
            else:
                explanation_status = "explanation_failed"

            run.explained.append(
                ExplainedDiscrepancy(
                    discrepancy_id=discrepancy_id,
                    match_key=match.match_key,
                    category_code=discrepancy.category_code,
                    rule_id=discrepancy.rule_id,
                    delta_minor=discrepancy.delta_minor,
                    deterministic_summary=discrepancy.summary,
                    evidence=discrepancy.evidence,
                    gate_decision=gate.decision.value,
                    gate_reason=gate.reason,
                    is_cold_start=gate.is_cold_start,
                    observations_needed=_observations_needed(trust, settings),
                    explanation=explanation.explanation if explanation else None,
                    corrective_action=explanation.corrective_action if explanation else None,
                    model_confidence=explanation.model_confidence if explanation else None,
                    explanation_status=explanation_status,
                )
            )

    return run
