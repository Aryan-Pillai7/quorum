"""Audit selection, EMA recalibration, and the high-value fail-safe. Pure logic, no DB."""

from __future__ import annotations

import pytest

from app.models.enums import GateDecision
from app.services.approvals import _ema
from app.services.trust import decide_gate

WARM = {
    "auto_apply_threshold": 0.85,
    "review_threshold": 0.50,
    "min_sample_size": 30,
    "category_auto_resolvable": True,
}


# --- EMA ------------------------------------------------------------------------------


def test_the_ema_moves_toward_the_outcome():
    assert _ema(0.0, 1.0, 0.2) == 0.2
    assert _ema(1.0, 0.0, 0.2) == 0.8


def test_a_perfect_run_from_cold_start_crosses_the_threshold_at_nine():
    """Documents the actual trajectory rather than leaving it to be discovered live."""
    score = 0.0
    crossed_at = None
    for n in range(1, 31):
        score = _ema(score, 1.0, 0.2)
        if crossed_at is None and score >= 0.85:
            crossed_at = n
    assert crossed_at == 9
    assert score > 0.99


def test_the_ema_forgets_old_evidence_faster_than_a_running_ratio():
    """The reason for choosing EMA (ADR-0026).

    After a long good run, one bad outcome should register. A running ratio over hundreds
    of observations would barely move, which is wrong when a processor changes something
    and the old evidence stops describing reality.
    """
    score = 0.0
    for _ in range(200):
        score = _ema(score, 1.0, 0.2)
    ratio_after_one_failure = 200 / 201  # what a running ratio would give
    ema_after_one_failure = _ema(score, 0.0, 0.2)
    assert ema_after_one_failure < ratio_after_one_failure
    assert ema_after_one_failure == pytest.approx(0.8, abs=0.01)


def test_a_bad_run_pulls_a_trusted_category_back_below_the_threshold():
    score = 0.999
    for _ in range(5):
        score = _ema(score, 0.0, 0.2)
    assert score < 0.85


# --- the high-value fail-safe (ADR-0027) ----------------------------------------------


def test_a_high_value_transaction_is_reviewed_even_in_a_fully_trusted_category():
    """The exit criterion's fail-safe, at the exact state the demo reaches.

    TIMING_DIFFERENCE after 30 audited-correct approvals: score 0.9988, sample_size 30,
    gate AUTO_APPLY. A large amount in that category must still reach a human.
    """
    result = decide_gate(
        score=0.9988, sample_size=30, **WARM,
        amount_minor=250_000,
        high_value_threshold_minor=200_000,
    )
    assert result.decision is GateDecision.HUMAN_REVIEW
    assert "high-value review threshold" in result.reason


def test_the_same_category_auto_applies_below_the_threshold():
    """Proves the fail-safe is about the amount, not a blanket refusal."""
    result = decide_gate(
        score=0.9988, sample_size=30, **WARM,
        amount_minor=199_999,
        high_value_threshold_minor=200_000,
    )
    assert result.decision is GateDecision.AUTO_APPLY


def test_the_threshold_is_inclusive():
    result = decide_gate(
        score=0.9988, sample_size=30, **WARM,
        amount_minor=200_000,
        high_value_threshold_minor=200_000,
    )
    assert result.decision is GateDecision.HUMAN_REVIEW


def test_the_fail_safe_uses_magnitude_not_direction():
    """A shortfall of 2,500 is as reviewable as a surplus of 2,500."""
    for amount in (250_000, -250_000):
        result = decide_gate(
            score=0.9988, sample_size=30, **WARM,
            amount_minor=amount,
            high_value_threshold_minor=200_000,
        )
        assert result.decision is GateDecision.HUMAN_REVIEW, amount


def test_a_perfect_score_over_a_huge_sample_does_not_defeat_the_fail_safe():
    """Being right 9,999 times in 10,000 says nothing about the cost of the 10,000th."""
    result = decide_gate(
        score=0.9999, sample_size=10_000, correct_count=9_999, **WARM,
        amount_minor=10_000_000,
        high_value_threshold_minor=200_000,
    )
    assert result.decision is GateDecision.HUMAN_REVIEW


def test_the_policy_ceiling_still_outranks_everything():
    result = decide_gate(
        score=1.0, sample_size=10_000,
        auto_apply_threshold=0.85, review_threshold=0.50, min_sample_size=30,
        category_auto_resolvable=False,
        amount_minor=1, high_value_threshold_minor=200_000,
    )
    assert result.decision is GateDecision.HUMAN_REVIEW
    assert "not eligible for automation" in result.reason


def test_omitting_the_amount_leaves_the_gate_unchanged():
    """Callers that do not know an amount must not be silently blocked."""
    assert decide_gate(score=0.9988, sample_size=30, **WARM).decision is GateDecision.AUTO_APPLY
