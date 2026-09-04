"""Deterministic discrepancy classification.

SEALED (ADR-0004): no agent layer, no network, no clock, no database. Every function here
is a pure function of its arguments, which is what makes a classification reproducible
from the stored inputs alone, forever, with no API key present.

Each rule returns a Finding carrying the field comparison that produced it. "Rule R06
fired because psp.fee_minor=2360 and ledger.fee_minor=1800, beyond the 200 tolerance" is
checkable by hand. "The engine flagged it" is not.

Rules are evaluated in the order given by `RULE_ORDER`: structural facts first (a leg is
missing, the order settled twice), then rules that *explain* an amount gap, then the
residual. A gap explained by an earlier rule is not re-reported as AMOUNT_MISMATCH.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.models.discrepancy import NOVEL_CATEGORY_CODE
from app.models.enums import MatchStrategy

# Category tolerances are read from the seeded taxonomy rather than hardcoded here, so
# tuning a tolerance is a data change. These are the fallbacks when a category row is
# absent, which should not happen but must not crash classification if it does.
DEFAULT_TOLERANCES: dict[str, int] = {
    "ROUNDING_DIFFERENCE": 100,
    "MDR_FEE_VARIANCE": 200,
}

# How many days a bank credit may lag the processor settlement date before the lag stops
# being ordinary settlement timing and becomes something else. A judgement call, not a
# calibrated value -- stated as such in the phase report.
DEFAULT_TIMING_WINDOW_DAYS = 2

# Settlement "day" is a business concept in IST, not a UTC calendar day. Timestamps are
# stored in UTC (correctly), but a bank value date of 2026-03-06 00:00 IST is
# 2026-03-05 18:30 UTC -- so comparing UTC dates would report a same-day credit for a
# payment settled the previous business day, and a genuine one-day lag as zero.
IST = timezone(timedelta(hours=5, minutes=30))


def business_date(moment: datetime):
    """The IST calendar date a timestamp falls on. Settlement windows are IST days."""
    return moment.astimezone(IST).date()


@dataclass(frozen=True)
class LegView:
    """Source-agnostic view of one leg, carrying only what the rules compare."""

    external_id: str
    amount_minor: int
    occurred_at: datetime
    gross_amount_minor: int | None = None
    fee_minor: int | None = None
    counterparty_ref: str | None = None
    order_ref: str | None = None


@dataclass(frozen=True)
class MatchContext:
    """Everything the rules are allowed to see about one candidate match."""

    psp: LegView | None
    bank: LegView | None
    ledger: LegView | None
    strategy: MatchStrategy
    tolerances: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_TOLERANCES))
    timing_window_days: int = DEFAULT_TIMING_WINDOW_DAYS

    # Set when several processor rows share one order reference.
    sibling_psp_ids: tuple[str, ...] = ()
    sibling_sum_minor: int | None = None

    # The ledger entry for this order, whether or not it is attached to *this* record.
    # A transaction may belong to at most one match (ADR-0005), so in a routing split the
    # ledger leg attaches to one processor row and the siblings have `ledger=None` -- but
    # the ledger entry plainly exists, and reporting those siblings as MISSING_IN_LEDGER
    # would be false. Group rules read this; leg-level rules keep using `ledger`.
    order_group_ledger: LegView | None = None

    def tolerance(self, category_code: str) -> int:
        return self.tolerances.get(category_code, DEFAULT_TOLERANCES.get(category_code, 0))

    @property
    def leg_count(self) -> int:
        return sum(1 for leg in (self.psp, self.bank, self.ledger) if leg is not None)

    @property
    def observed_minor(self) -> int | None:
        """What actually landed, preferring the bank as the most authoritative source."""
        if self.bank is not None:
            return self.bank.amount_minor
        if self.psp is not None:
            return self.psp.amount_minor
        return None

    @property
    def expected_minor(self) -> int | None:
        """What the books expected to land."""
        return self.ledger.amount_minor if self.ledger is not None else None

    @property
    def delta_minor(self) -> int:
        """Observed minus expected. Positive means more money landed than expected."""
        observed, expected = self.observed_minor, self.expected_minor
        if observed is None or expected is None:
            return 0
        return observed - expected


@dataclass(frozen=True)
class Finding:
    """One classified discrepancy, with the comparison that produced it."""

    rule_id: str
    category_code: str
    delta_minor: int
    summary: str
    evidence: dict[str, Any]

    # True when this finding accounts for the amount gap, so the residual rule does not
    # also report the same rupees as an unexplained mismatch.
    explains_delta: bool = False


Rule = Callable[[MatchContext], Finding | None]


# --------------------------------------------------------------------------------------
# Structural rules: which legs are present at all.
# --------------------------------------------------------------------------------------


def r01_missing_in_bank(ctx: MatchContext) -> Finding | None:
    """Processor and ledger agree a payment settled, but no money reached the bank."""
    if ctx.bank is not None or ctx.psp is None or ctx.ledger is None:
        return None
    return Finding(
        rule_id="R01_missing_in_bank",
        category_code="MISSING_IN_BANK",
        delta_minor=-ctx.ledger.amount_minor,
        summary=(
            f"processor row {ctx.psp.external_id} and ledger entry "
            f"{ctx.ledger.external_id} both record a settlement, but no bank credit "
            f"matches UTR {ctx.psp.counterparty_ref!r}"
        ),
        evidence={
            "compared": "psp.counterparty_ref -> bank.counterparty_ref",
            "psp_counterparty_ref": ctx.psp.counterparty_ref,
            "bank_leg": None,
            "expected_minor": ctx.ledger.amount_minor,
        },
        explains_delta=True,
    )


def r02_missing_in_ledger(ctx: MatchContext) -> Finding | None:
    """Money moved and the processor recorded it, but the books never did."""
    if ctx.ledger is not None or ctx.psp is None:
        return None
    # The order's ledger entry exists but attached to a sibling record in a split or
    # duplicate group. Absent from this record is not absent from the books.
    if ctx.order_group_ledger is not None:
        return None
    return Finding(
        rule_id="R02_missing_in_ledger",
        category_code="MISSING_IN_LEDGER",
        delta_minor=ctx.psp.amount_minor,
        summary=(
            f"processor row {ctx.psp.external_id} (order {ctx.psp.order_ref!r}) has no "
            f"matching internal ledger entry"
        ),
        evidence={
            "compared": "psp.order_ref -> ledger.order_ref",
            "psp_order_ref": ctx.psp.order_ref,
            "ledger_leg": None,
        },
        explains_delta=True,
    )


def r03_missing_in_psp(ctx: MatchContext) -> Finding | None:
    """Bank and ledger both know about it; the settlement report does not.

    Matched only by the weaker amount+date fallback, because with the processor row
    absent the bank and ledger share no reference field at all (ADR-0013).
    """
    if ctx.psp is not None or ctx.bank is None or ctx.ledger is None:
        return None
    return Finding(
        rule_id="R03_missing_in_psp",
        category_code="MISSING_IN_PSP",
        delta_minor=0,
        summary=(
            f"bank credit {ctx.bank.external_id} and ledger entry "
            f"{ctx.ledger.external_id} pair on amount and date, but no processor "
            f"settlement row covers them"
        ),
        evidence={
            "compared": "bank.amount_minor + occurred_at <-> ledger.amount_minor + occurred_at",
            "psp_leg": None,
            "note": "no shared reference field exists between bank and ledger",
        },
        explains_delta=True,
    )


def r04_duplicate_settlement(ctx: MatchContext) -> Finding | None:
    """The same order settled more than once for approximately the full amount."""
    if not ctx.sibling_psp_ids or ctx.psp is None:
        return None
    ledger = ctx.ledger or ctx.order_group_ledger
    if ledger is None:
        return None
    # Each sibling is close to the full expected amount, so this is repetition rather
    # than a split of one amount across acquirers.
    if abs(ctx.psp.amount_minor - ledger.amount_minor) > ctx.tolerance("ROUNDING_DIFFERENCE"):
        return None
    return Finding(
        rule_id="R04_duplicate_settlement",
        category_code="DUPLICATE_ENTRY",
        delta_minor=ctx.psp.amount_minor,
        summary=(
            f"order {ctx.psp.order_ref!r} settled {len(ctx.sibling_psp_ids) + 1} times, "
            f"each for approximately the full ledger amount "
            f"{ledger.amount_minor} minor units"
        ),
        evidence={
            "compared": "count of psp rows sharing order_ref, each vs ledger.amount_minor",
            "order_ref": ctx.psp.order_ref,
            "this_psp_row": ctx.psp.external_id,
            "sibling_psp_rows": list(ctx.sibling_psp_ids),
            "each_amount_minor": ctx.psp.amount_minor,
            "ledger_amount_minor": ledger.amount_minor,
        },
        explains_delta=True,
    )


def r05_routing_split(ctx: MatchContext) -> Finding | None:
    """One order settled across several processor rows that sum to the ledger amount."""
    if not ctx.sibling_psp_ids or ctx.psp is None or ctx.sibling_sum_minor is None:
        return None
    ledger = ctx.ledger or ctx.order_group_ledger
    if ledger is None:
        return None
    # Distinguished from duplicate settlement by the parts summing to the whole rather
    # than each part being the whole.
    if abs(ctx.sibling_sum_minor - ledger.amount_minor) > ctx.tolerance("ROUNDING_DIFFERENCE"):
        return None
    return Finding(
        rule_id="R05_routing_split",
        category_code="ROUTING_SPLIT",
        delta_minor=0,
        summary=(
            f"order {ctx.psp.order_ref!r} settled across "
            f"{len(ctx.sibling_psp_ids) + 1} processor rows summing to "
            f"{ctx.sibling_sum_minor} minor units, matching the ledger amount"
        ),
        evidence={
            "compared": "sum(psp.amount_minor over shared order_ref) vs ledger.amount_minor",
            "order_ref": ctx.psp.order_ref,
            "parts": [ctx.psp.external_id, *ctx.sibling_psp_ids],
            "sum_minor": ctx.sibling_sum_minor,
            "ledger_amount_minor": ledger.amount_minor,
        },
        explains_delta=True,
    )


# --------------------------------------------------------------------------------------
# Rules that explain an amount or timing gap.
# --------------------------------------------------------------------------------------


def r06_mdr_fee_variance(ctx: MatchContext) -> Finding | None:
    """The processor deducted a different fee than the contracted rate implies."""
    if ctx.psp is None or ctx.ledger is None:
        return None
    if ctx.psp.fee_minor is None or ctx.ledger.fee_minor is None:
        return None

    # MDR is a *rate*, so variance means the rate changed -- not that the absolute fee
    # differs. Comparing absolute fees reports a false variance whenever the captured
    # amount differs from the authorised one, which is exactly what a partial capture or
    # a routing split looks like. Compare the processor's fee against the fee the
    # ledger's own effective rate implies for the amount actually captured.
    expected_fee = ctx.ledger.fee_minor
    basis = "ledger.fee_minor (absolute; gross unavailable on one side)"
    if (
        ctx.psp.gross_amount_minor is not None
        and ctx.ledger.gross_amount_minor
        and ctx.ledger.gross_amount_minor > 0
    ):
        # Integer arithmetic with round-half-up, so the expectation is exact and has no
        # float in it (ADR-0002).
        numerator = ctx.ledger.fee_minor * ctx.psp.gross_amount_minor
        expected_fee = (numerator + ctx.ledger.gross_amount_minor // 2) // (
            ctx.ledger.gross_amount_minor
        )
        basis = "ledger effective rate applied to psp.gross_amount_minor"

    variance = ctx.psp.fee_minor - expected_fee
    if abs(variance) <= ctx.tolerance("MDR_FEE_VARIANCE"):
        return None
    return Finding(
        rule_id="R06_mdr_fee_variance",
        category_code="MDR_FEE_VARIANCE",
        # A larger fee than expected means less money landed, hence the sign flip.
        delta_minor=-variance,
        summary=(
            f"processor deducted {ctx.psp.fee_minor} minor units in fees where the "
            f"ledger rate implies {expected_fee} for the captured amount, a variance "
            f"of {variance}"
        ),
        evidence={
            "compared": "psp.fee_minor vs fee implied by the ledger rate",
            "basis": basis,
            "psp_fee_minor": ctx.psp.fee_minor,
            "psp_gross_minor": ctx.psp.gross_amount_minor,
            "ledger_fee_minor": ctx.ledger.fee_minor,
            "ledger_gross_minor": ctx.ledger.gross_amount_minor,
            "expected_fee_minor": expected_fee,
            "variance_minor": variance,
            "tolerance_minor": ctx.tolerance("MDR_FEE_VARIANCE"),
        },
        explains_delta=True,
    )


def r07_partial_capture(ctx: MatchContext) -> Finding | None:
    """Less was captured than the ledger authorised."""
    if ctx.psp is None or ctx.ledger is None:
        return None
    if ctx.psp.gross_amount_minor is None or ctx.ledger.gross_amount_minor is None:
        return None
    # A routing split's parts are each smaller than the ledger gross by construction.
    # That is the split, not an under-capture, and R05 already reports it.
    if r05_routing_split(ctx) is not None:
        return None
    shortfall = ctx.ledger.gross_amount_minor - ctx.psp.gross_amount_minor
    if shortfall <= ctx.tolerance("ROUNDING_DIFFERENCE"):
        return None
    return Finding(
        rule_id="R07_partial_capture",
        category_code="PARTIAL_CAPTURE",
        delta_minor=-shortfall,
        summary=(
            f"captured gross {ctx.psp.gross_amount_minor} minor units against an "
            f"authorised {ctx.ledger.gross_amount_minor}, short by {shortfall}"
        ),
        evidence={
            "compared": "psp.gross_amount_minor vs ledger.gross_amount_minor",
            "captured_gross_minor": ctx.psp.gross_amount_minor,
            "authorised_gross_minor": ctx.ledger.gross_amount_minor,
            "shortfall_minor": shortfall,
        },
        explains_delta=True,
    )


def r08_timing_difference(ctx: MatchContext) -> Finding | None:
    """Amounts agree; the bank credit lands in a later settlement window."""
    if ctx.psp is None or ctx.bank is None:
        return None
    lag_days = (business_date(ctx.bank.occurred_at) - business_date(ctx.psp.occurred_at)).days
    if lag_days <= 0 or lag_days > ctx.timing_window_days:
        return None
    # Deliberately NOT gated on the amounts agreeing. A credit that arrives two days
    # late is late whether or not it is also short -- those are independent observations
    # about the same payment. Suppressing the timing fact because an amount fact exists
    # is precisely the single-label compromise ADR-0012 removed.
    return Finding(
        rule_id="R08_timing_difference",
        category_code="TIMING_DIFFERENCE",
        delta_minor=0,
        summary=(
            f"bank credited {lag_days} day(s) after the processor settlement date "
            f"(within the {ctx.timing_window_days}-day window)"
        ),
        evidence={
            "compared": "IST business date of bank.occurred_at vs psp.occurred_at",
            "psp_settled_at": ctx.psp.occurred_at.isoformat(),
            "bank_value_date": ctx.bank.occurred_at.isoformat(),
            "lag_days": lag_days,
            "window_days": ctx.timing_window_days,
        },
    )


def r09_reference_mismatch(ctx: MatchContext) -> Finding | None:
    """Legs paired on amount and date although both carry references that disagree."""
    if ctx.strategy is not MatchStrategy.AMOUNT_DATE_WINDOW:
        return None
    if ctx.psp is None or ctx.bank is None:
        return None
    if not ctx.psp.counterparty_ref or not ctx.bank.counterparty_ref:
        return None
    if ctx.psp.counterparty_ref == ctx.bank.counterparty_ref:
        return None
    return Finding(
        rule_id="R09_reference_mismatch",
        category_code="REFERENCE_MISMATCH",
        delta_minor=0,
        summary=(
            f"paired on amount and date only: processor UTR "
            f"{ctx.psp.counterparty_ref!r} does not equal bank UTR "
            f"{ctx.bank.counterparty_ref!r}"
        ),
        evidence={
            "compared": "psp.counterparty_ref vs bank.counterparty_ref",
            "psp_counterparty_ref": ctx.psp.counterparty_ref,
            "bank_counterparty_ref": ctx.bank.counterparty_ref,
            "fallback_strategy": ctx.strategy.value,
        },
    )


def r10_rounding_difference(ctx: MatchContext) -> Finding | None:
    """A sub-rupee gap consistent with rounding at one of the sources."""
    if ctx.observed_minor is None or ctx.expected_minor is None:
        return None
    delta = ctx.delta_minor
    if delta == 0 or abs(delta) > ctx.tolerance("ROUNDING_DIFFERENCE"):
        return None
    return Finding(
        rule_id="R10_rounding_difference",
        category_code="ROUNDING_DIFFERENCE",
        delta_minor=delta,
        summary=(
            f"observed {ctx.observed_minor} against expected {ctx.expected_minor}, a "
            f"difference of {delta} minor units, within the rounding tolerance"
        ),
        evidence={
            "compared": "observed (bank, else psp) vs ledger.amount_minor",
            "observed_minor": ctx.observed_minor,
            "expected_minor": ctx.expected_minor,
            "delta_minor": delta,
            "tolerance_minor": ctx.tolerance("ROUNDING_DIFFERENCE"),
        },
        explains_delta=True,
    )


# Evaluated in this order. Structural facts first, then explanations of an amount gap,
# then presentational findings that do not themselves account for money.
RULE_ORDER: Sequence[Rule] = (
    r01_missing_in_bank,
    r02_missing_in_ledger,
    r03_missing_in_psp,
    r04_duplicate_settlement,
    r05_routing_split,
    r06_mdr_fee_variance,
    r07_partial_capture,
    r10_rounding_difference,
    r08_timing_difference,
    r09_reference_mismatch,
)


def classify(ctx: MatchContext) -> list[Finding]:
    """Every finding that applies to this match, in rule order.

    Multi-label by construction (ADR-0012): a settlement can be both late and short on
    fee, and reporting only the first would discard a true statement.

    A residual gap that no rule explains becomes AMOUNT_MISMATCH if the legs are all
    present, and `__novel__` otherwise -- the difference being whether the shape itself
    is understood, not merely the amount.
    """
    findings = [finding for rule in RULE_ORDER if (finding := rule(ctx)) is not None]

    delta = ctx.delta_minor
    explained = any(f.explains_delta for f in findings)

    if delta != 0 and not explained:
        if ctx.leg_count == 3:
            findings.append(
                Finding(
                    rule_id="R11_amount_mismatch",
                    category_code="AMOUNT_MISMATCH",
                    delta_minor=delta,
                    summary=(
                        f"all three legs present but observed {ctx.observed_minor} "
                        f"against expected {ctx.expected_minor}, a difference of "
                        f"{delta} minor units that no rule explains"
                    ),
                    evidence={
                        "compared": "observed (bank, else psp) vs ledger.amount_minor",
                        "observed_minor": ctx.observed_minor,
                        "expected_minor": ctx.expected_minor,
                        "delta_minor": delta,
                        "rules_evaluated": [rule.__name__ for rule in RULE_ORDER],
                    },
                    explains_delta=True,
                )
            )
        else:
            findings.append(
                Finding(
                    rule_id="R12_novel",
                    category_code=NOVEL_CATEGORY_CODE,
                    delta_minor=delta,
                    summary=(
                        f"unexplained difference of {delta} minor units across "
                        f"{ctx.leg_count} of 3 legs; no deterministic rule profile fits"
                    ),
                    evidence={
                        "observed_minor": ctx.observed_minor,
                        "expected_minor": ctx.expected_minor,
                        "delta_minor": delta,
                        "legs_present": {
                            "psp": ctx.psp is not None,
                            "bank": ctx.bank is not None,
                            "ledger": ctx.ledger is not None,
                        },
                        "rules_evaluated": [rule.__name__ for rule in RULE_ORDER],
                    },
                    explains_delta=True,
                )
            )

    return findings
