"""Aggregated payout detection (ADR-0019).

Pure functions, no database. The tests that matter most here are the ones about what the
engine does when it does NOT know: silently picking one of several valid groupings would
attribute money to the wrong payments, which is the specific failure a reconciliation tool
exists to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import GroupMethod, GroupStatus
from app.services.matching.aggregation import (
    AGGREGATION_WINDOW_DAYS,
    MAX_CANDIDATES,
    BankCredit,
    CandidateRow,
    bound_candidates,
    detect_aggregation,
    find_subsets_summing_to,
    group_by_shared_reference,
    group_by_subset_sum,
)

AT = datetime(2026, 3, 5, 5, 0, tzinfo=UTC)


def row(external_id: str, amount: int, *, ref: str | None = None, at: datetime = AT,
        currency: str = "INR") -> CandidateRow:
    return CandidateRow(
        transaction_id=external_id,
        external_id=external_id,
        amount_minor=amount,
        occurred_at=at,
        currency=currency,
        counterparty_ref=ref,
    )


def credit(amount: int, *, ref: str | None = "UTR_PAYOUT", at: datetime = AT,
           currency: str = "INR") -> BankCredit:
    return BankCredit(
        transaction_id="bnk_1",
        external_id="bnk_1",
        amount_minor=amount,
        occurred_at=at,
        currency=currency,
        counterparty_ref=ref,
    )


# --- shared reference: the case aggregation actually looks like -----------------------


def test_rows_under_one_payout_reference_resolve():
    members = [row("pay_1", 30_000, ref="UTR_PAYOUT"), row("pay_2", 70_000, ref="UTR_PAYOUT")]
    result = group_by_shared_reference(credit(100_000), members, tolerance_minor=0)
    assert result.status is GroupStatus.RESOLVED
    assert result.method is GroupMethod.SHARED_REFERENCE
    assert [m.external_id for m in result.members] == ["pay_1", "pay_2"]
    assert result.delta_minor == 0


def test_a_single_row_under_the_reference_is_not_an_aggregation():
    """One row sharing the payout reference is an ordinary 1:1 match."""
    result = group_by_shared_reference(
        credit(100_000), [row("pay_1", 100_000, ref="UTR_PAYOUT")], tolerance_minor=0
    )
    assert result is None


def test_a_credit_with_no_reference_has_no_shared_reference_shape():
    result = group_by_shared_reference(
        credit(100_000, ref=None), [row("pay_1", 50_000), row("pay_2", 50_000)],
        tolerance_minor=0,
    )
    assert result is None


def test_rows_sharing_the_reference_that_do_not_sum_are_not_silently_regrouped():
    """They carry the payout's own reference, so they belong together and simply disagree.

    Going off to find some other set that happens to add up would be inventing a grouping
    the data does not support.
    """
    members = [row("pay_1", 30_000, ref="UTR_PAYOUT"), row("pay_2", 30_000, ref="UTR_PAYOUT")]
    result = group_by_shared_reference(credit(100_000), members, tolerance_minor=100)
    assert result.status is GroupStatus.INCONCLUSIVE
    assert [m.external_id for m in result.members] == ["pay_1", "pay_2"]
    assert result.delta_minor == -40_000


def test_shared_reference_respects_tolerance():
    members = [row("pay_1", 30_000, ref="UTR_PAYOUT"), row("pay_2", 69_950, ref="UTR_PAYOUT")]
    result = group_by_shared_reference(credit(100_000), members, tolerance_minor=100)
    assert result.status is GroupStatus.RESOLVED


# --- subset-sum ----------------------------------------------------------------------


def test_a_unique_subset_resolves():
    candidates = [row("pay_1", 30_000), row("pay_2", 20_000), row("pay_3", 50_000),
                  row("pay_4", 77_000)]
    result = group_by_subset_sum(credit(100_000), candidates, tolerance_minor=0)
    assert result.status is GroupStatus.RESOLVED
    assert result.method is GroupMethod.SUBSET_SUM
    assert [m.external_id for m in result.members] == ["pay_1", "pay_2", "pay_3"]
    assert result.solution_count == 1


def test_two_distinct_sets_are_ambiguous_and_no_group_is_formed():
    """The load-bearing test. {300} and {100,200} both explain a 300 credit."""
    candidates = [row("pay_1", 30_000), row("pay_2", 10_000), row("pay_3", 20_000)]
    result = group_by_subset_sum(credit(30_000), candidates, tolerance_minor=0)
    assert result.status is GroupStatus.AMBIGUOUS
    assert result.members == [], "an ambiguous result must claim no members"
    assert len(result.competing_solutions) == 2
    assert "refusing to choose" in result.reason


def test_equal_amounts_are_distinct_payments_and_so_are_ambiguous():
    """Two rows of the same value are two different payments.

    Which one the credit covers is precisely what is unknown, so this is a real ambiguity
    rather than a cosmetic one.
    """
    candidates = [row("pay_1", 10_000), row("pay_2", 10_000), row("pay_3", 55_000)]
    result = group_by_subset_sum(credit(10_000), candidates, tolerance_minor=0)
    assert result.status is GroupStatus.AMBIGUOUS
    assert sorted(sorted(s) for s in result.competing_solutions) == [["pay_1"], ["pay_2"]]


def test_no_subset_summing_is_inconclusive_not_a_denial():
    candidates = [row("pay_1", 33_333), row("pay_2", 44_444)]
    result = group_by_subset_sum(credit(99_999), candidates, tolerance_minor=0)
    assert result.status is GroupStatus.INCONCLUSIVE
    assert result.members == []


def test_a_single_row_solution_is_not_claimed_as_an_aggregation():
    candidates = [row("pay_1", 100_000), row("pay_2", 7_000)]
    result = group_by_subset_sum(credit(100_000), candidates, tolerance_minor=0)
    assert result.status is GroupStatus.INCONCLUSIVE
    assert "1:1 match" in result.reason


def test_too_many_candidates_reports_not_searched_rather_than_not_found():
    """'Not attempted' and 'searched and found nothing' are different claims."""
    candidates = [row(f"pay_{i:03d}", 1_000 + i) for i in range(MAX_CANDIDATES + 5)]
    result = group_by_subset_sum(credit(500_000), candidates, tolerance_minor=0)
    assert result.status is GroupStatus.INCONCLUSIVE
    assert "was not attempted" in result.reason
    assert result.nodes_explored == 0


def test_fewer_than_two_candidates_cannot_aggregate():
    result = group_by_subset_sum(credit(100_000), [row("pay_1", 100_000)], tolerance_minor=0)
    assert result.status is GroupStatus.INCONCLUSIVE
    assert "at least two" in result.reason


# --- search bounds -------------------------------------------------------------------


def test_the_node_budget_reports_an_unfinished_search():
    """A truncated search must never be read as 'no solution exists'."""
    candidates = [row(f"pay_{i:03d}", 100_000 + i) for i in range(40)]
    solutions, nodes, exhausted = find_subsets_summing_to(
        candidates, 2_000_000, tolerance_minor=0, max_nodes=500
    )
    assert exhausted is False
    assert nodes > 500


def test_a_completed_search_reports_itself_exhausted():
    candidates = [row("pay_1", 10_000), row("pay_2", 20_000)]
    _solutions, _nodes, exhausted = find_subsets_summing_to(candidates, 30_000)
    assert exhausted is True


def test_the_search_stops_at_the_second_solution():
    """Uniqueness is the only question; counting past two is wasted work."""
    candidates = [row(f"pay_{i}", 10_000) for i in range(10)]
    solutions, _nodes, _exhausted = find_subsets_summing_to(
        candidates, 10_000, tolerance_minor=0
    )
    assert len(solutions) == 2


def test_candidates_outside_the_window_are_excluded():
    far = AT + timedelta(days=AGGREGATION_WINDOW_DAYS + 1)
    bounded = bound_candidates(credit(100_000), [row("pay_1", 50_000, at=far)])
    assert bounded == []


def test_candidates_in_another_currency_are_excluded():
    bounded = bound_candidates(
        credit(100_000), [row("pay_1", 50_000, currency="USD")]
    )
    assert bounded == []


def test_a_row_larger_than_the_credit_cannot_be_part_of_it():
    bounded = bound_candidates(credit(100_000), [row("pay_1", 100_001)])
    assert bounded == []


def test_bounding_is_deterministic():
    """Stable ordering is what makes the whole search reproducible run to run."""
    candidates = [row("pay_b", 10_000), row("pay_a", 10_000), row("pay_c", 90_000)]
    first = [c.external_id for c in bound_candidates(credit(100_000), candidates)]
    second = [c.external_id for c in bound_candidates(credit(100_000), list(reversed(candidates)))]
    assert first == second == ["pay_c", "pay_a", "pay_b"]


# --- dispatch ------------------------------------------------------------------------


def test_shared_reference_is_preferred_over_search():
    """Exact beats a search whenever the shape applies."""
    candidates = [
        row("pay_1", 30_000, ref="UTR_PAYOUT"),
        row("pay_2", 70_000, ref="UTR_PAYOUT"),
        row("pay_3", 100_000, ref="UTR_OTHER"),
    ]
    result = detect_aggregation(credit(100_000), candidates, tolerance_minor=0)
    assert result.method is GroupMethod.SHARED_REFERENCE
    assert result.nodes_explored == 0


def test_search_runs_when_no_row_carries_the_credits_reference():
    candidates = [row("pay_1", 40_000, ref="UTR_X"), row("pay_2", 60_000, ref="UTR_Y")]
    result = detect_aggregation(credit(100_000), candidates, tolerance_minor=0)
    assert result.method is GroupMethod.SUBSET_SUM
    assert result.status is GroupStatus.RESOLVED


@pytest.mark.parametrize("status", [GroupStatus.AMBIGUOUS, GroupStatus.INCONCLUSIVE])
def test_only_resolved_results_carry_members(status):
    """Nothing but a RESOLVED group may claim settlement rows."""
    if status is GroupStatus.AMBIGUOUS:
        candidates = [row("pay_1", 30_000), row("pay_2", 10_000), row("pay_3", 20_000)]
        result = group_by_subset_sum(credit(30_000), candidates, tolerance_minor=0)
    else:
        result = group_by_subset_sum(
            credit(99_999), [row("pay_1", 1), row("pay_2", 2)], tolerance_minor=0
        )
    assert result.status is status
    assert result.members == []


def test_evidence_names_every_grouped_row_and_the_bounds_used():
    """Traceability: the output must state which rows were grouped and under what limits."""
    candidates = [row("pay_1", 30_000, ref="UTR_PAYOUT"), row("pay_2", 70_000, ref="UTR_PAYOUT")]
    evidence = detect_aggregation(credit(100_000), candidates, tolerance_minor=0).to_evidence()
    assert evidence["members"] == ["pay_1", "pay_2"]
    assert evidence["bank_amount_minor"] == 100_000
    assert evidence["members_total_minor"] == 100_000
    assert evidence["bounds"]["max_candidates"] == MAX_CANDIDATES
    assert evidence["reason"]
