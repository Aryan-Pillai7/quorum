"""The trust gate: what the AI layer is permitted to do, per discrepancy category.

This is the safety boundary of the whole system. Every agent-proposed change passes
through `decide_gate` before it can touch ledger state (Phase 3 wires the write path).

The gate is deliberately a pure function of explicit inputs -- no session, no cache, no
clock. That makes every decision reproducible and testable, and makes it impossible for
a stale cache read to change an outcome without the stale value being visible in the
GateEvaluation that comes back.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models.enums import GateDecision


@dataclass(frozen=True)
class GateEvaluation:
    """A gate decision plus the evidence behind it.

    The reason string is written into audit records, so it explains the decision in
    terms a reviewer can check rather than asserting a verdict.
    """

    decision: GateDecision
    reason: str
    score: Decimal
    sample_size: int
    is_cold_start: bool

    @property
    def requires_human(self) -> bool:
        return self.decision is not GateDecision.AUTO_APPLY


def decide_gate(
    *,
    score: Decimal | float | int,
    sample_size: int,
    correct_count: int | None = None,
    auto_apply_threshold: Decimal | float,
    review_threshold: Decimal | float,
    min_sample_size: int,
    category_auto_resolvable: bool,
    amount_minor: int | None = None,
    high_value_threshold_minor: int | None = None,
) -> GateEvaluation:
    """Decide whether an agent proposal may be applied automatically.

    The checks are ordered from most to least restrictive, and the first one that fires
    wins. That order is the policy:

    1. A category marked not auto-resolvable is never automated, at any score. This is a
       policy ceiling, not a confidence judgement -- MISSING_IN_BANK stays human-reviewed
       even at a perfect score, because being right 100 times does not make the 101st
       misdirected payout acceptable to auto-close.
    2. A large enough amount is always reviewed, whatever the category has earned
       (ADR-0027). Trust is measured per category, so it says how often the system is
       right about that *kind* of problem -- it says nothing about how much a single
       mistake would cost. A category at 0.99 over ten thousand observations is still
       wrong one time in a hundred, and that hundredth case should not be a large payout
       closing itself.
    3. Below `min_sample_size` the score is not evidence. Cold start caps at HUMAN_REVIEW,
       never BLOCK: a new category with no history should not halt the pipeline, it should
       route to a person.
    4. Then, and only then, the score is compared against thresholds.
    """
    score_d = Decimal(str(score))
    auto_d = Decimal(str(auto_apply_threshold))
    review_d = Decimal(str(review_threshold))

    if sample_size < 0:
        raise ValueError(f"sample_size must be non-negative, got {sample_size}")
    if correct_count is not None and not 0 <= correct_count <= sample_size:
        raise ValueError(
            f"correct_count {correct_count} must be between 0 and sample_size {sample_size}"
        )
    if review_d >= auto_d:
        # Mirrors the DB CHECK. Enforced here too, because the gate is also called with
        # settings-derived fallbacks for categories that have no trust row yet.
        raise ValueError(
            f"review_threshold ({review_d}) must be strictly below "
            f"auto_apply_threshold ({auto_d}); otherwise no human-review band exists"
        )
    if min_sample_size < 1:
        raise ValueError(f"min_sample_size must be at least 1, got {min_sample_size}")

    if amount_minor is not None and amount_minor < 0:
        # Callers pass a magnitude; a signed delta would make the comparison below depend
        # on which direction the money went, which is not what the rule is about.
        amount_minor = abs(amount_minor)

    cold_start = sample_size < min_sample_size

    if not category_auto_resolvable:
        return GateEvaluation(
            decision=GateDecision.HUMAN_REVIEW,
            reason="category is not eligible for automation regardless of trust score",
            score=score_d,
            sample_size=sample_size,
            is_cold_start=cold_start,
        )

    if (
        amount_minor is not None
        and high_value_threshold_minor is not None
        and amount_minor >= high_value_threshold_minor
    ):
        return GateEvaluation(
            decision=GateDecision.HUMAN_REVIEW,
            reason=(
                f"amount {amount_minor} minor units is at or above the "
                f"{high_value_threshold_minor} high-value review threshold; large amounts "
                f"are always reviewed regardless of what the category has earned"
            ),
            score=score_d,
            sample_size=sample_size,
            is_cold_start=cold_start,
        )

    if cold_start:
        return GateEvaluation(
            decision=GateDecision.HUMAN_REVIEW,
            reason=(
                f"cold start: {sample_size} of {min_sample_size} observations needed before "
                f"the score is treated as evidence"
            ),
            score=score_d,
            sample_size=sample_size,
            is_cold_start=True,
        )

    if score_d >= auto_d:
        decision, reason = (
            GateDecision.AUTO_APPLY,
            f"score {score_d} >= auto-apply threshold {auto_d} over {sample_size} observations",
        )
    elif score_d >= review_d:
        decision, reason = (
            GateDecision.HUMAN_REVIEW,
            f"score {score_d} is between review threshold {review_d} and "
            f"auto-apply threshold {auto_d}",
        )
    else:
        decision, reason = (
            GateDecision.BLOCK,
            f"score {score_d} < review threshold {review_d} over {sample_size} observations; "
            f"agent proposals for this category are not surfaced as suggestions",
        )

    return GateEvaluation(
        decision=decision,
        reason=reason,
        score=score_d,
        sample_size=sample_size,
        is_cold_start=False,
    )
