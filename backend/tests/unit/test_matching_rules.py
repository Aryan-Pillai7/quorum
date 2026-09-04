"""Deterministic classification rules.

One test per failure mode, plus the cases that were actually wrong when the engine first
ran against the fixture. Those regression tests are marked; each of them corresponds to a
class of false findings that reached the numbers before being caught.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.discrepancy import NOVEL_CATEGORY_CODE
from app.models.enums import MatchStrategy
from app.services.matching.rules import (
    LegView,
    MatchContext,
    business_date,
    classify,
)

# 10:30 IST on 2026-03-05, stored as UTC.
SETTLED_AT = datetime(2026, 3, 5, 5, 0, tzinfo=UTC)

GROSS = 100_000  # 1,000.00 INR in paise
FEE = 2_360  # 2% MDR + 18% GST, in paise
NET = GROSS - FEE


def leg(
    external_id: str,
    amount: int,
    *,
    at: datetime = SETTLED_AT,
    gross: int | None = None,
    fee: int | None = None,
    utr: str | None = "UTR1",
    order: str | None = "ord_1",
) -> LegView:
    return LegView(
        external_id=external_id,
        amount_minor=amount,
        occurred_at=at,
        gross_amount_minor=gross,
        fee_minor=fee,
        counterparty_ref=utr,
        order_ref=order,
    )


def context(**overrides) -> MatchContext:
    base = {
        "psp": leg("pay_1", NET, gross=GROSS, fee=FEE),
        "bank": leg("bnk_1", NET, utr="UTR1", order=None),
        "ledger": leg("led_1", NET, gross=GROSS, fee=FEE, utr=None),
        "strategy": MatchStrategy.EXACT_REFERENCE,
        "tolerances": {"ROUNDING_DIFFERENCE": 100, "MDR_FEE_VARIANCE": 200},
    }
    return MatchContext(**{**base, **overrides})


def categories(ctx: MatchContext) -> list[str]:
    return [f.category_code for f in classify(ctx)]


# --- the clean case -------------------------------------------------------------------


def test_a_clean_three_way_match_produces_no_findings():
    assert classify(context()) == []


# --- structural failure modes ---------------------------------------------------------


def test_missing_in_bank():
    ctx = context(bank=None)
    assert categories(ctx) == ["MISSING_IN_BANK"]


def test_missing_in_ledger():
    ctx = context(ledger=None)
    assert categories(ctx) == ["MISSING_IN_LEDGER"]


def test_missing_in_psp():
    ctx = context(psp=None, strategy=MatchStrategy.AMOUNT_DATE_WINDOW)
    assert categories(ctx) == ["MISSING_IN_PSP"]


def test_duplicate_settlement_when_each_row_is_the_full_amount():
    ctx = context(sibling_psp_ids=("pay_2",), sibling_sum_minor=NET * 2)
    assert "DUPLICATE_ENTRY" in categories(ctx)


def test_routing_split_when_the_parts_sum_to_the_ledger_amount():
    half = NET // 2
    ctx = context(
        psp=leg("pay_1", half, gross=GROSS // 2, fee=FEE // 2),
        bank=leg("bnk_1", half, utr="UTR1", order=None),
        sibling_psp_ids=("pay_2",),
        sibling_sum_minor=NET,
    )
    assert "ROUTING_SPLIT" in categories(ctx)


def test_split_and_duplicate_are_distinguished_by_whether_parts_sum_or_repeat():
    """The same shape -- several rows on one order -- means different things."""
    split = context(
        psp=leg("pay_1", NET // 2, gross=GROSS // 2, fee=FEE // 2),
        bank=leg("bnk_1", NET // 2, utr="UTR1", order=None),
        sibling_psp_ids=("pay_2",),
        sibling_sum_minor=NET,
    )
    duplicate = context(sibling_psp_ids=("pay_2",), sibling_sum_minor=NET * 2)

    assert "ROUTING_SPLIT" in categories(split)
    assert "DUPLICATE_ENTRY" not in categories(split)
    assert "DUPLICATE_ENTRY" in categories(duplicate)
    assert "ROUTING_SPLIT" not in categories(duplicate)


# --- amount and timing failure modes --------------------------------------------------


def test_mdr_fee_variance_when_the_processor_charged_above_contract():
    overcharged = FEE + 1_000
    ctx = context(
        psp=leg("pay_1", GROSS - overcharged, gross=GROSS, fee=overcharged),
        bank=leg("bnk_1", GROSS - overcharged, utr="UTR1", order=None),
    )
    assert "MDR_FEE_VARIANCE" in categories(ctx)


def test_fee_within_tolerance_is_not_a_variance():
    ctx = context(
        psp=leg("pay_1", NET - 150, gross=GROSS, fee=FEE + 150),
        bank=leg("bnk_1", NET - 150, utr="UTR1", order=None),
    )
    assert "MDR_FEE_VARIANCE" not in categories(ctx)


def test_partial_capture():
    captured_gross = 60_000
    captured_fee = 1_416  # the same 2.36% rate applied to the smaller capture
    ctx = context(
        psp=leg("pay_1", captured_gross - captured_fee, gross=captured_gross, fee=captured_fee),
        bank=leg("bnk_1", captured_gross - captured_fee, utr="UTR1", order=None),
    )
    assert "PARTIAL_CAPTURE" in categories(ctx)


def test_timing_difference_within_the_window():
    ctx = context(bank=leg("bnk_1", NET, at=SETTLED_AT + timedelta(days=1), order=None))
    assert "TIMING_DIFFERENCE" in categories(ctx)


def test_a_lag_beyond_the_window_is_not_ordinary_timing():
    ctx = context(bank=leg("bnk_1", NET, at=SETTLED_AT + timedelta(days=9), order=None))
    assert "TIMING_DIFFERENCE" not in categories(ctx)


def test_rounding_difference_within_tolerance():
    ctx = context(bank=leg("bnk_1", NET - 50, utr="UTR1", order=None))
    assert categories(ctx) == ["ROUNDING_DIFFERENCE"]


def test_reference_mismatch_only_when_paired_by_the_fallback():
    ctx = context(
        strategy=MatchStrategy.AMOUNT_DATE_WINDOW,
        bank=leg("bnk_1", NET, utr="UTR-DIFFERENT", order=None),
    )
    assert "REFERENCE_MISMATCH" in categories(ctx)


def test_differing_references_are_not_a_finding_when_matched_by_reference():
    """If the join succeeded on reference, the references cannot disagree."""
    ctx = context(bank=leg("bnk_1", NET, utr="UTR-DIFFERENT", order=None))
    assert "REFERENCE_MISMATCH" not in categories(ctx)


# --- multi-label: the reason ADR-0012 exists ------------------------------------------


def test_a_payment_can_be_both_late_and_overcharged():
    """The compound case. A single-category schema would discard one true statement."""
    overcharged = FEE + 1_000
    ctx = context(
        psp=leg("pay_1", GROSS - overcharged, gross=GROSS, fee=overcharged),
        bank=leg(
            "bnk_1",
            GROSS - overcharged,
            at=SETTLED_AT + timedelta(days=1),
            utr="UTR1",
            order=None,
        ),
    )
    found = set(categories(ctx))
    assert {"MDR_FEE_VARIANCE", "TIMING_DIFFERENCE"} <= found


# --- residual -------------------------------------------------------------------------


def test_unexplained_gap_across_all_three_legs_is_an_amount_mismatch():
    ctx = context(bank=leg("bnk_1", NET - 50_000, utr="UTR1", order=None))
    assert "AMOUNT_MISMATCH" in categories(ctx)


def test_amount_mismatch_is_not_reported_when_a_rule_already_explains_the_gap():
    """The same rupees must not be counted twice, once explained and once not."""
    overcharged = FEE + 5_000
    ctx = context(
        psp=leg("pay_1", GROSS - overcharged, gross=GROSS, fee=overcharged),
        bank=leg("bnk_1", GROSS - overcharged, utr="UTR1", order=None),
    )
    found = categories(ctx)
    assert "MDR_FEE_VARIANCE" in found
    assert "AMOUNT_MISMATCH" not in found


def test_novel_when_the_shape_itself_is_not_understood():
    """A gap with a leg missing, unexplained: not a classification, the absence of one."""
    ctx = context(
        psp=None,
        bank=leg("bnk_1", NET - 50_000, utr="UTR1", order=None),
        strategy=MatchStrategy.AMOUNT_DATE_WINDOW,
    )
    assert NOVEL_CATEGORY_CODE in categories(ctx)


# --- regressions: each of these produced false findings against the fixture ------------


def test_regression_sibling_ledger_is_not_reported_missing():
    """A split sibling has ledger=None because the leg attached to the anchor record.

    Reporting that as MISSING_IN_LEDGER is false: the entry exists. This produced 11
    false findings on the fixture.
    """
    ctx = context(
        ledger=None,
        order_group_ledger=leg("led_1", NET, gross=GROSS, fee=FEE, utr=None),
        sibling_psp_ids=("pay_2",),
        sibling_sum_minor=NET * 2,
    )
    assert "MISSING_IN_LEDGER" not in categories(ctx)


def test_regression_partial_capture_is_not_also_a_fee_variance():
    """MDR is a rate. A smaller capture correctly carries a proportionally smaller fee.

    Comparing absolute fees produced 10 false variance findings on the fixture.
    """
    captured_gross = 60_000
    captured_fee = 1_416  # same effective rate as the ledger's 2_360 on 100_000
    ctx = context(
        psp=leg("pay_1", captured_gross - captured_fee, gross=captured_gross, fee=captured_fee),
        bank=leg("bnk_1", captured_gross - captured_fee, utr="UTR1", order=None),
    )
    found = categories(ctx)
    assert "PARTIAL_CAPTURE" in found
    assert "MDR_FEE_VARIANCE" not in found


def test_regression_routing_split_part_is_not_an_under_capture():
    """Each part of a split is smaller than the ledger gross by construction.

    That produced 6 false PARTIAL_CAPTURE findings on the fixture.
    """
    half_gross, half_fee = GROSS // 2, FEE // 2
    ctx = context(
        psp=leg("pay_1", half_gross - half_fee, gross=half_gross, fee=half_fee),
        bank=leg("bnk_1", half_gross - half_fee, utr="UTR1", order=None),
        sibling_psp_ids=("pay_2",),
        sibling_sum_minor=NET,
    )
    found = categories(ctx)
    assert "ROUTING_SPLIT" in found
    assert "PARTIAL_CAPTURE" not in found


@pytest.mark.parametrize(
    ("utc_moment", "expected_ist_date"),
    [
        (datetime(2026, 3, 5, 5, 0, tzinfo=UTC), "2026-03-05"),  # 10:30 IST
        (datetime(2026, 3, 5, 18, 30, tzinfo=UTC), "2026-03-06"),  # 00:00 IST next day
        (datetime(2026, 3, 5, 18, 29, tzinfo=UTC), "2026-03-05"),  # one minute earlier
    ],
)
def test_regression_business_date_is_ist_not_utc(utc_moment, expected_ist_date):
    """A settlement window is an IST day.

    A bank value date of 2026-03-06 00:00 IST is 2026-03-05 18:30 UTC, so comparing UTC
    dates computed a genuine one-day lag as zero.
    """
    assert business_date(utc_moment).isoformat() == expected_ist_date
