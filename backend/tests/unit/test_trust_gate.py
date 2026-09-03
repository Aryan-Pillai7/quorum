"""The trust gate: what the AI layer is permitted to do.

This is the safety boundary, so the tests are about policy, not plumbing. Each case
names the rule it pins down.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.enums import GateDecision
from app.services.trust import decide_gate

# A category that is eligible for automation and has plenty of evidence behind it.
WARM = {
    "auto_apply_threshold": 0.90,
    "review_threshold": 0.60,
    "min_sample_size": 50,
    "category_auto_resolvable": True,
}


def test_high_score_with_enough_evidence_auto_applies():
    result = decide_gate(score=0.97, sample_size=500, correct_count=485, **WARM)
    assert result.decision is GateDecision.AUTO_APPLY
    assert result.requires_human is False
    assert result.is_cold_start is False


def test_score_between_thresholds_goes_to_human_review():
    result = decide_gate(score=0.75, sample_size=500, correct_count=375, **WARM)
    assert result.decision is GateDecision.HUMAN_REVIEW
    assert result.requires_human is True


def test_score_below_review_threshold_is_blocked():
    result = decide_gate(score=0.30, sample_size=500, correct_count=150, **WARM)
    assert result.decision is GateDecision.BLOCK


@pytest.mark.parametrize("score", [0.90, 0.9000, 1.0])
def test_auto_apply_threshold_is_inclusive(score):
    assert decide_gate(score=score, sample_size=500, **WARM).decision is GateDecision.AUTO_APPLY


def test_just_below_auto_apply_threshold_does_not_auto_apply():
    """0.8999 must not round its way into automation."""
    result = decide_gate(score=0.8999, sample_size=500, **WARM)
    assert result.decision is GateDecision.HUMAN_REVIEW


def test_perfect_score_on_tiny_sample_never_auto_applies():
    """1/1 is not evidence. This is the single most important case in this file."""
    result = decide_gate(score=1.0, sample_size=1, correct_count=1, **WARM)
    assert result.decision is GateDecision.HUMAN_REVIEW
    assert result.is_cold_start is True
    assert "cold start" in result.reason


def test_cold_start_at_one_observation_short_of_the_floor():
    """The boundary is exclusive: min_sample_size observations are required, not fewer."""
    assert decide_gate(score=0.99, sample_size=49, **WARM).is_cold_start is True
    assert decide_gate(score=0.99, sample_size=50, **WARM).is_cold_start is False


def test_cold_start_routes_to_review_not_block():
    """A category with no history should route to a person, not halt the pipeline."""
    result = decide_gate(score=0.0, sample_size=0, correct_count=0, **WARM)
    assert result.decision is GateDecision.HUMAN_REVIEW
    assert result.decision is not GateDecision.BLOCK


def test_non_auto_resolvable_category_never_automates_at_any_score():
    """A policy ceiling, independent of confidence. MISSING_IN_BANK stays human-reviewed."""
    result = decide_gate(
        score=1.0,
        sample_size=100_000,
        correct_count=100_000,
        auto_apply_threshold=0.90,
        review_threshold=0.60,
        min_sample_size=50,
        category_auto_resolvable=False,
    )
    assert result.decision is GateDecision.HUMAN_REVIEW
    assert "not eligible for automation" in result.reason


def test_policy_ceiling_outranks_a_low_score():
    """Ineligibility short-circuits before scoring, so it cannot be downgraded to BLOCK."""
    result = decide_gate(
        score=0.01,
        sample_size=100_000,
        auto_apply_threshold=0.90,
        review_threshold=0.60,
        min_sample_size=50,
        category_auto_resolvable=False,
    )
    assert result.decision is GateDecision.HUMAN_REVIEW


def test_evaluation_carries_the_evidence_behind_the_decision():
    """Audit records quote this, so the numbers must survive the call."""
    result = decide_gate(score=Decimal("0.9250"), sample_size=200, correct_count=185, **WARM)
    assert result.score == Decimal("0.9250")
    assert result.sample_size == 200
    assert "200" in result.reason


def test_incoherent_thresholds_are_rejected():
    """review >= auto would leave no band in which a human is asked."""
    with pytest.raises(ValueError, match="no human-review band exists"):
        decide_gate(
            score=0.95,
            sample_size=100,
            auto_apply_threshold=0.60,
            review_threshold=0.90,
            min_sample_size=50,
            category_auto_resolvable=True,
        )


def test_correct_count_exceeding_sample_size_is_rejected():
    with pytest.raises(ValueError, match="must be between 0 and sample_size"):
        decide_gate(score=0.9, sample_size=10, correct_count=11, **WARM)


def test_negative_sample_size_is_rejected():
    with pytest.raises(ValueError, match="must be non-negative"):
        decide_gate(score=0.9, sample_size=-1, **WARM)
