"""Trust decay on silence (ADR-0030). Pure function, no I/O, no clock.

The property this protects: a category that earned automation and then went quiet must
stop automating. Nothing in the recalibration loop notices silence -- without decay a
score sits at whatever it last earned indefinitely, authorising automation on evidence
that may be months old.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.enums import GateDecision
from app.services.trust import (
    DEFAULT_DECAY_DAYS,
    DEFAULT_DECAY_GRACE_DAYS,
    evaluate_decay,
    gate_for_trust_row,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

# TIMING_DIFFERENCE as the Phase 6 demo leaves it: 30 audited observations, score 0.9988.
EARNED = {"score": Decimal("0.9988"), "sample_size": 30, "review_threshold": Decimal("0.50")}

# review_threshold deliberately lives only in EARNED: it is both the gate's lower band
# and the decay floor, so duplicating it here would let the two drift apart in a test.
WARM_GATE = {
    "auto_apply_threshold": Decimal("0.85"),
    "min_sample_size": 30,
    "category_auto_resolvable": True,
}


def decay(days_quiet: float, **overrides):
    args = {**EARNED, "last_audit_at": NOW - timedelta(days=days_quiet), "as_of": NOW}
    return evaluate_decay(**{**args, **overrides})


# --- the grace period -----------------------------------------------------------------


@pytest.mark.parametrize("days", [0, 1, 7, 13, DEFAULT_DECAY_GRACE_DAYS])
def test_nothing_decays_inside_the_grace_period(days):
    """A quiet fortnight is normal, not evidence of anything."""
    result = decay(days)
    assert result.is_decaying is False
    assert result.effective_score == Decimal("0.9988")


def test_decay_begins_after_the_grace_period():
    assert decay(DEFAULT_DECAY_GRACE_DAYS + 0.5).is_decaying is True


# --- the curve ------------------------------------------------------------------------


def test_the_score_reaches_the_floor_and_stops():
    """Linear decay arrives on a specific day, which is the point of choosing it."""
    full = DEFAULT_DECAY_GRACE_DAYS + DEFAULT_DECAY_DAYS
    assert decay(full).effective_score == Decimal("0.50")
    assert decay(full + 100).effective_score == Decimal("0.50")
    assert decay(full + 1000).effective_score == Decimal("0.50")


def test_the_floor_is_the_review_threshold_not_zero():
    """Losing automation is the goal; halting the pipeline is not.

    Decaying to zero would drop the category to BLOCK, which stops surfacing suggestions
    entirely -- a heavier consequence than "nobody checked recently" warrants.
    """
    result = decay(500)
    assert result.effective_score == Decimal("0.50")
    assert result.floor == Decimal("0.50")


def test_decay_is_monotonic_in_time():
    """More silence can only ever mean less trust."""
    scores = [decay(d).effective_score for d in range(0, 60, 3)]
    assert scores == sorted(scores, reverse=True)


def test_decay_is_proportional_at_the_halfway_point():
    halfway = DEFAULT_DECAY_GRACE_DAYS + DEFAULT_DECAY_DAYS / 2
    expected = Decimal("0.9988") - (Decimal("0.9988") - Decimal("0.50")) / 2
    assert float(decay(halfway).effective_score) == pytest.approx(float(expected), abs=1e-4)


# --- decay never helps ----------------------------------------------------------------


@pytest.mark.parametrize("days", [0, 15, 30, 45, 90, 365])
def test_decay_never_raises_a_score(days):
    """The load-bearing property. Silence must never make a category more automatable."""
    assert decay(days).effective_score <= Decimal("0.9988")


def test_a_score_already_below_the_floor_is_left_alone():
    """Decay pulls toward the floor, never past it, and never upward toward it."""
    result = decay(90, score=Decimal("0.20"))
    assert result.is_decaying is False
    assert result.effective_score == Decimal("0.20")


def test_a_score_exactly_at_the_floor_does_not_move():
    result = decay(90, score=Decimal("0.50"))
    assert result.effective_score == Decimal("0.50")
    assert result.is_decaying is False


# --- nothing to decay from ------------------------------------------------------------


def test_a_category_with_no_audited_observations_does_not_decay():
    """Cold start and gone-quiet are different situations.

    The gate already handles the first, and conflating them would report a brand-new
    category as degrading.
    """
    result = evaluate_decay(
        score=Decimal("0"),
        sample_size=0,
        last_audit_at=None,
        review_threshold=Decimal("0.50"),
        as_of=NOW,
    )
    assert result.is_decaying is False
    assert result.days_since_audit is None
    assert "nothing to decay from" in result.reason


def test_a_missing_timestamp_does_not_decay_even_with_observations():
    """Defensive: sample_size without a timestamp is inconsistent data, not staleness."""
    result = evaluate_decay(
        score=Decimal("0.99"),
        sample_size=30,
        last_audit_at=None,
        review_threshold=Decimal("0.50"),
        as_of=NOW,
    )
    assert result.is_decaying is False


def test_a_future_timestamp_does_not_produce_negative_staleness():
    result = decay(-5)
    assert result.days_since_audit == 0.0
    assert result.is_decaying is False


# --- what the gate does with it -------------------------------------------------------


def test_an_earned_gate_survives_the_grace_period():
    evaluation, decayed = gate_for_trust_row(
        **EARNED, **WARM_GATE, last_audit_at=NOW - timedelta(days=10), as_of=NOW
    )
    assert evaluation.decision is GateDecision.AUTO_APPLY
    assert decayed.is_decaying is False


def test_silence_eventually_removes_automation():
    """The exit criterion: a real earned score decaying back to HUMAN_REVIEW."""
    evaluation, decayed = gate_for_trust_row(
        **EARNED, **WARM_GATE, last_audit_at=NOW - timedelta(days=30), as_of=NOW
    )
    assert evaluation.decision is GateDecision.HUMAN_REVIEW
    assert decayed.is_decaying is True
    assert decayed.effective_score < Decimal("0.85")


def test_the_gate_reason_says_the_score_was_discounted():
    """A category that used to automate and now does not must be explainable."""
    evaluation, _ = gate_for_trust_row(
        **EARNED, **WARM_GATE, last_audit_at=NOW - timedelta(days=30), as_of=NOW
    )
    assert "decayed" in evaluation.reason
    assert "Silence is not evidence of correctness" in evaluation.reason


def test_decay_never_drops_a_category_to_block():
    """The floor keeps a stale category at HUMAN_REVIEW rather than halting it."""
    evaluation, _ = gate_for_trust_row(
        **EARNED, **WARM_GATE, last_audit_at=NOW - timedelta(days=3650), as_of=NOW
    )
    assert evaluation.decision is GateDecision.HUMAN_REVIEW
    assert evaluation.decision is not GateDecision.BLOCK


# --- composition with the other rules (ADR-0027, policy ceiling) -----------------------


def test_the_high_value_failsafe_still_fires_on_a_fresh_category():
    """Decay composes underneath the amount rule; it must not weaken it."""
    evaluation, decayed = gate_for_trust_row(
        **EARNED,
        **WARM_GATE,
        last_audit_at=NOW - timedelta(days=1),
        as_of=NOW,
        amount_minor=250_000,
        high_value_threshold_minor=200_000,
    )
    assert evaluation.decision is GateDecision.HUMAN_REVIEW
    assert decayed.is_decaying is False
    assert "high-value review threshold" in evaluation.reason


def test_the_policy_ceiling_still_outranks_everything_including_decay():
    evaluation, _ = gate_for_trust_row(
        score=Decimal("0.9988"),
        sample_size=30,
        auto_apply_threshold=Decimal("0.85"),
        review_threshold=Decimal("0.50"),
        min_sample_size=30,
        category_auto_resolvable=False,
        last_audit_at=NOW - timedelta(days=90),
        as_of=NOW,
    )
    assert evaluation.decision is GateDecision.HUMAN_REVIEW
    assert "not eligible for automation" in evaluation.reason


def test_decay_cannot_rescue_a_cold_start_category():
    """Decay only removes automation. It can never grant it."""
    evaluation, _ = gate_for_trust_row(
        score=Decimal("0.99"),
        sample_size=2,
        auto_apply_threshold=Decimal("0.85"),
        review_threshold=Decimal("0.50"),
        min_sample_size=30,
        category_auto_resolvable=True,
        last_audit_at=NOW - timedelta(days=90),
        as_of=NOW,
    )
    assert evaluation.decision is GateDecision.HUMAN_REVIEW
    assert "cold start" in evaluation.reason
