"""The trust gate: what the AI layer is permitted to do, per discrepancy category.

This is the safety boundary of the whole system. Every agent-proposed change passes
through `decide_gate` before it can touch ledger state (Phase 3 wires the write path).

The gate is deliberately a pure function of explicit inputs -- no session, no cache, no
clock. That makes every decision reproducible and testable, and makes it impossible for
a stale cache read to change an outcome without the stale value being visible in the
GateEvaluation that comes back.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from app.models.enums import GateDecision

# How long a category may go without an audited observation before its score starts
# decaying. Two weeks is a natural review cadence: a category audited fortnightly stays
# fresh, and one that has gone a full fortnight without a single verified observation has
# genuinely gone quiet. A judgement call, not a calibrated value.
DEFAULT_DECAY_GRACE_DAYS = 14

# How long the decay then takes to reach the floor. Four weeks after the grace period, so
# a category that goes completely silent loses automation about six weeks after its last
# audit -- slow enough not to punish a quiet fortnight, fast enough that stale evidence
# does not keep authorising automation for a quarter.
DEFAULT_DECAY_DAYS = 28


@dataclass(frozen=True)
class DecayEvaluation:
    """How much a category's score has been discounted for going quiet (ADR-0030).

    Trust is a claim about how often the system is currently right. Evidence from six
    weeks ago supports that claim less well than evidence from yesterday, and nothing in
    the recalibration loop notices the difference -- a category that stops receiving
    audits simply keeps whatever score it last earned, forever.
    """

    base_score: Decimal
    effective_score: Decimal
    floor: Decimal
    is_decaying: bool
    days_since_audit: float | None
    reason: str

    @property
    def discount(self) -> Decimal:
        return self.base_score - self.effective_score


def evaluate_decay(
    *,
    score: Decimal | float | int,
    sample_size: int,
    last_audit_at: datetime | None,
    review_threshold: Decimal | float,
    as_of: datetime,
    grace_days: int = DEFAULT_DECAY_GRACE_DAYS,
    decay_days: int = DEFAULT_DECAY_DAYS,
) -> DecayEvaluation:
    """Discount a trust score for time since its last audited observation.

    Pure: same inputs, same answer, no clock and no database. `as_of` is required rather
    than defaulted to now() precisely so this stays reproducible.

    Three properties hold by construction, and each exists to prevent a specific mistake:

    1. **It only ever pulls a score down.** Silence is not evidence of correctness, so it
       must never be able to make a category more automatable than its audits justify.
    2. **It stops at the review threshold, not at zero.** Losing automation is the point;
       halting the pipeline is not. Decaying to BLOCK would stop surfacing suggestions
       entirely, which is a heavier consequence than "nobody checked recently" warrants --
       and it would contradict the Phase 1 rule that a category with no usable evidence
       routes to a human rather than stopping work.
    3. **A category with no audited evidence does not decay.** There is nothing to decay
       from. A brand-new category and a category that earned trust and then went quiet are
       different situations and must not be conflated: the first is cold start, which the
       gate already handles.

    Decay is linear in time rather than exponential. An exponential curve approaches the
    floor without ever arriving, so "when does this category lose automation?" would have
    no answer. Linear reaches the floor on a specific, statable day.
    """
    base = Decimal(str(score))
    floor = Decimal(str(review_threshold))

    if sample_size <= 0 or last_audit_at is None:
        return DecayEvaluation(
            base_score=base,
            effective_score=base,
            floor=floor,
            is_decaying=False,
            days_since_audit=None,
            reason="no audited observations yet, so there is nothing to decay from",
        )

    elapsed = (as_of - last_audit_at).total_seconds() / 86_400.0
    days = round(max(0.0, elapsed), 2)

    if base <= floor:
        return DecayEvaluation(
            base_score=base,
            effective_score=base,
            floor=floor,
            is_decaying=False,
            days_since_audit=days,
            reason=(
                f"score {base} is already at or below the safe floor {floor}; decay only "
                f"pulls downward"
            ),
        )

    if days <= grace_days:
        return DecayEvaluation(
            base_score=base,
            effective_score=base,
            floor=floor,
            is_decaying=False,
            days_since_audit=days,
            reason=(
                f"last audited observation was {days:.1f} days ago, within the "
                f"{grace_days}-day grace period"
            ),
        )

    progress = min(1.0, (days - grace_days) / decay_days) if decay_days > 0 else 1.0
    effective = base - (Decimal(str(progress)) * (base - floor))
    effective = max(floor, round(effective, 4))

    return DecayEvaluation(
        base_score=base,
        effective_score=effective,
        floor=floor,
        is_decaying=True,
        days_since_audit=days,
        reason=(
            f"no audited observation for {days:.1f} days: score discounted from {base} "
            f"to {effective} ({progress * 100:.0f}% of the way to the {floor} floor). "
            f"Silence is not evidence of correctness."
        ),
    )


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


def gate_for_trust_row(
    *,
    score: Decimal | float | int,
    sample_size: int,
    auto_apply_threshold: Decimal | float,
    review_threshold: Decimal | float,
    min_sample_size: int,
    category_auto_resolvable: bool,
    last_audit_at: datetime | None,
    correct_count: int | None = None,
    amount_minor: int | None = None,
    high_value_threshold_minor: int | None = None,
    as_of: datetime | None = None,
    grace_days: int = DEFAULT_DECAY_GRACE_DAYS,
    decay_days: int = DEFAULT_DECAY_DAYS,
) -> tuple[GateEvaluation, DecayEvaluation]:
    """Decay the score for staleness, then gate on the discounted value.

    The single entry point every gating path uses. There are four of them -- the dashboard,
    the agent explanation path, audit selection, and the trust API -- and expecting each to
    remember to apply decay before calling `decide_gate` is exactly how a safety rule ends
    up applied in three places out of four. Composing them here makes forgetting impossible
    rather than merely unlikely.

    Decay composes underneath the other rules rather than alongside them, which is what
    ADR-0027 and the policy ceiling need: both short-circuit before the score is consulted
    at all, so a discounted score cannot make either of them more permissive. Decay can
    only ever remove an AUTO_APPLY that a stale score was still authorising.
    """
    as_of = as_of or datetime.now(UTC)

    decay = evaluate_decay(
        score=score,
        sample_size=sample_size,
        last_audit_at=last_audit_at,
        review_threshold=review_threshold,
        as_of=as_of,
        grace_days=grace_days,
        decay_days=decay_days,
    )

    evaluation = decide_gate(
        score=decay.effective_score,
        sample_size=sample_size,
        correct_count=correct_count,
        auto_apply_threshold=auto_apply_threshold,
        review_threshold=review_threshold,
        min_sample_size=min_sample_size,
        category_auto_resolvable=category_auto_resolvable,
        amount_minor=amount_minor,
        high_value_threshold_minor=high_value_threshold_minor,
    )

    if decay.is_decaying:
        # Say so in the reason. A gate decision that silently used a discounted score would
        # be unexplainable to whoever is looking at a category that used to automate.
        evaluation = replace(
            evaluation, reason=f"{evaluation.reason} [decayed: {decay.reason}]"
        )
    return evaluation, decay
