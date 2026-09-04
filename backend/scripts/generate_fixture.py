"""Generate the reconciliation fixture dataset.

Deterministic: a fixed seed produces byte-identical CSVs, so the checked-in files can be
regenerated and diffed. Discrepancies are planted by construction, which is what makes
expected outcomes exactly known rather than eyeballed.

    python scripts/generate_fixture.py

Read ADR-0015 before quoting any number derived from this dataset. It is synthetic. A
match rate measured on it says whether the engine implements the rules as written; it
says nothing about whether those rules describe real settlement files.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

SEED = 20260304
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "recon_2026_03"

CURRENCY = "INR"
BASE_DATE = date(2026, 3, 2)
SETTLEMENT_TIME = time(10, 30, 0)

# Contracted rate the internal ledger books against: 2% MDR plus 18% GST on the fee.
MDR_RATE = Decimal("0.02")
GST_RATE = Decimal("0.18")


@dataclass
class Case:
    """One payment and the outcome the engine is expected to reach for it."""

    order_id: str
    expected_categories: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Dataset:
    settlement: list[dict[str, str]] = field(default_factory=list)
    bank: list[dict[str, str]] = field(default_factory=list)
    ledger: list[dict[str, str]] = field(default_factory=list)
    cases: list[Case] = field(default_factory=list)
    aggregations: list[dict] = field(default_factory=list)


def rupees(amount: Decimal) -> str:
    return str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def contracted_fee(gross: Decimal) -> tuple[Decimal, Decimal]:
    """(mdr, gst) under the contracted rate, each rounded to paise."""
    mdr = (gross * MDR_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    gst = (mdr * GST_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return mdr, gst


def _stamp(day: date, at: time = SETTLEMENT_TIME) -> str:
    return datetime.combine(day, at).strftime("%Y-%m-%d %H:%M:%S")


def _settlement_row(
    n: int, order_id: str, day: date, gross: Decimal, mdr: Decimal, gst: Decimal, utr: str
) -> dict[str, str]:
    return {
        "payment_id": f"pay_{n:05d}",
        "order_id": order_id,
        "rrn": f"RRN{n:09d}",
        "settlement_utr": utr,
        "settlement_date": _stamp(day),
        "gross_amount": rupees(gross),
        "mdr_fee": rupees(mdr),
        "gst_on_fee": rupees(gst),
        "net_amount": rupees(gross - mdr - gst),
        "currency": CURRENCY,
    }


def _bank_row(n: int, day: date, utr: str, credit: Decimal) -> dict[str, str]:
    return {
        "txn_id": f"bnk_{n:05d}",
        "value_date": _stamp(day, time(0, 0, 0)),
        "utr": utr,
        "narration": f"NEFT SETTLEMENT {utr}",
        "credit_amount": rupees(credit),
        "debit_amount": "",
        "currency": CURRENCY,
    }


def _ledger_row(
    n: int, order_id: str, day: date, gross: Decimal, expected_fee: Decimal
) -> dict[str, str]:
    return {
        "entry_id": f"led_{n:05d}",
        "order_id": order_id,
        "booked_at": _stamp(day),
        "gross_amount": rupees(gross),
        "expected_fee": rupees(expected_fee),
        "currency": CURRENCY,
        "entry_type": "SALE",
    }


def build() -> Dataset:  # noqa: PLR0915 - a flat script of planted cases reads better linear
    rng = random.Random(SEED)
    data = Dataset()
    n = 0

    def next_id() -> int:
        nonlocal n
        n += 1
        return n

    def gross_amount() -> Decimal:
        return Decimal(rng.randrange(10_000, 5_000_00)) / Decimal(100)

    def settle_day() -> date:
        return BASE_DATE + timedelta(days=rng.randrange(0, 20))

    # --- 380 clean three-way matches ---------------------------------------------------
    for _ in range(380):
        i = next_id()
        order = f"ord_{i:05d}"
        day = settle_day()
        gross = gross_amount()
        mdr, gst = contracted_fee(gross)
        utr = f"UTR{i:09d}"
        net = gross - mdr - gst

        data.settlement.append(_settlement_row(i, order, day, gross, mdr, gst, utr))
        data.bank.append(_bank_row(i, day, utr, net))
        data.ledger.append(_ledger_row(i, order, day, gross, mdr + gst))
        data.cases.append(Case(order, [], "clean three-way match"))

    # --- 25 timing differences: bank credits 1-2 business days later -------------------
    for _ in range(25):
        i = next_id()
        order = f"ord_{i:05d}"
        day = settle_day()
        gross = gross_amount()
        mdr, gst = contracted_fee(gross)
        utr = f"UTR{i:09d}"
        net = gross - mdr - gst
        lag = rng.choice([1, 2])

        data.settlement.append(_settlement_row(i, order, day, gross, mdr, gst, utr))
        data.bank.append(_bank_row(i, day + timedelta(days=lag), utr, net))
        data.ledger.append(_ledger_row(i, order, day, gross, mdr + gst))
        data.cases.append(Case(order, ["TIMING_DIFFERENCE"], f"bank credit lags {lag} day(s)"))

    # --- 20 MDR fee variances: processor charged a different rate ----------------------
    for _ in range(20):
        i = next_id()
        order = f"ord_{i:05d}"
        day = settle_day()
        gross = gross_amount()
        expected_mdr, expected_gst = contracted_fee(gross)
        # Actual rate differs enough to clear the 2.00 INR variance tolerance.
        actual_mdr = (expected_mdr * Decimal("1.5")).quantize(Decimal("0.01"))
        actual_gst = (actual_mdr * GST_RATE).quantize(Decimal("0.01"))
        utr = f"UTR{i:09d}"
        net = gross - actual_mdr - actual_gst

        data.settlement.append(_settlement_row(i, order, day, gross, actual_mdr, actual_gst, utr))
        data.bank.append(_bank_row(i, day, utr, net))
        data.ledger.append(_ledger_row(i, order, day, gross, expected_mdr + expected_gst))
        data.cases.append(Case(order, ["MDR_FEE_VARIANCE"], "processor charged above contract"))

    # --- 12 missing in bank: settled and booked, no money arrived ----------------------
    for _ in range(12):
        i = next_id()
        order = f"ord_{i:05d}"
        day = settle_day()
        gross = gross_amount()
        mdr, gst = contracted_fee(gross)

        data.settlement.append(
            _settlement_row(i, order, day, gross, mdr, gst, f"UTR{i:09d}")
        )
        data.ledger.append(_ledger_row(i, order, day, gross, mdr + gst))
        data.cases.append(Case(order, ["MISSING_IN_BANK"], "no bank credit for this UTR"))

    # --- 10 missing in ledger: settled and credited, never booked ----------------------
    for _ in range(10):
        i = next_id()
        order = f"ord_{i:05d}"
        day = settle_day()
        gross = gross_amount()
        mdr, gst = contracted_fee(gross)
        utr = f"UTR{i:09d}"

        data.settlement.append(_settlement_row(i, order, day, gross, mdr, gst, utr))
        data.bank.append(_bank_row(i, day, utr, gross - mdr - gst))
        data.cases.append(Case(order, ["MISSING_IN_LEDGER"], "no internal ledger entry"))

    # --- 8 missing in PSP: bank and ledger agree, no settlement row --------------------
    # These pair only by amount+date, because with no processor row bank and ledger share
    # no reference field at all (ADR-0013).
    for _ in range(8):
        i = next_id()
        order = f"ord_{i:05d}"
        day = settle_day()
        gross = gross_amount()
        mdr, gst = contracted_fee(gross)
        net = gross - mdr - gst

        data.bank.append(_bank_row(i, day, f"UTR{i:09d}", net))
        data.ledger.append(_ledger_row(i, order, day, gross, mdr + gst))
        data.cases.append(Case(order, ["MISSING_IN_PSP"], "no processor settlement row"))

    # --- 10 partial captures -----------------------------------------------------------
    for _ in range(10):
        i = next_id()
        order = f"ord_{i:05d}"
        day = settle_day()
        authorised = gross_amount()
        captured = (authorised * Decimal("0.6")).quantize(Decimal("0.01"))
        mdr, gst = contracted_fee(captured)
        expected_mdr, expected_gst = contracted_fee(authorised)
        utr = f"UTR{i:09d}"

        data.settlement.append(_settlement_row(i, order, day, captured, mdr, gst, utr))
        data.bank.append(_bank_row(i, day, utr, captured - mdr - gst))
        data.ledger.append(
            _ledger_row(i, order, day, authorised, expected_mdr + expected_gst)
        )
        data.cases.append(Case(order, ["PARTIAL_CAPTURE"], "captured 60% of authorised"))

    # --- 6 routing splits: one order settled across two acquirers ----------------------
    for _ in range(6):
        i = next_id()
        order = f"ord_{i:05d}"
        day = settle_day()
        gross = gross_amount()
        part_a = (gross / 2).quantize(Decimal("0.01"))
        part_b = gross - part_a
        mdr_a, gst_a = contracted_fee(part_a)
        mdr_b, gst_b = contracted_fee(part_b)
        expected_fee = mdr_a + gst_a + mdr_b + gst_b

        utr_a, utr_b = f"UTR{i:09d}A", f"UTR{i:09d}B"
        data.settlement.append(_settlement_row(i, order, day, part_a, mdr_a, gst_a, utr_a))
        j = next_id()
        data.settlement.append(_settlement_row(j, order, day, part_b, mdr_b, gst_b, utr_b))
        data.bank.append(_bank_row(i, day, utr_a, part_a - mdr_a - gst_a))
        data.bank.append(_bank_row(j, day, utr_b, part_b - mdr_b - gst_b))
        data.ledger.append(_ledger_row(i, order, day, gross, expected_fee))
        data.cases.append(Case(order, ["ROUTING_SPLIT"], "settled across two acquirers"))

    # --- 5 duplicate settlements: the same order paid out twice in full ----------------
    for _ in range(5):
        i = next_id()
        order = f"ord_{i:05d}"
        day = settle_day()
        gross = gross_amount()
        mdr, gst = contracted_fee(gross)
        net = gross - mdr - gst

        utr_a = f"UTR{i:09d}"
        data.settlement.append(_settlement_row(i, order, day, gross, mdr, gst, utr_a))
        j = next_id()
        utr_b = f"UTR{j:09d}"
        data.settlement.append(_settlement_row(j, order, day, gross, mdr, gst, utr_b))
        data.bank.append(_bank_row(i, day, utr_a, net))
        data.bank.append(_bank_row(j, day, utr_b, net))
        data.ledger.append(_ledger_row(i, order, day, gross, mdr + gst))
        data.cases.append(Case(order, ["DUPLICATE_ENTRY"], "order settled twice in full"))

    # --- 8 rounding differences: bank short by a few paise ------------------------------
    for _ in range(8):
        i = next_id()
        order = f"ord_{i:05d}"
        day = settle_day()
        gross = gross_amount()
        mdr, gst = contracted_fee(gross)
        utr = f"UTR{i:09d}"
        net = gross - mdr - gst
        drift = Decimal(rng.choice([-50, -25, 25, 50])) / Decimal(100)

        data.settlement.append(_settlement_row(i, order, day, gross, mdr, gst, utr))
        data.bank.append(_bank_row(i, day, utr, net + drift))
        data.ledger.append(_ledger_row(i, order, day, gross, mdr + gst))
        data.cases.append(Case(order, ["ROUNDING_DIFFERENCE"], f"bank off by {drift}"))

    # --- 8 compound: late AND overcharged ----------------------------------------------
    # The case multi-label exists for. A single-category schema would have to discard one
    # of two true statements here.
    for _ in range(8):
        i = next_id()
        order = f"ord_{i:05d}"
        day = settle_day()
        gross = gross_amount()
        expected_mdr, expected_gst = contracted_fee(gross)
        actual_mdr = (expected_mdr * Decimal("1.5")).quantize(Decimal("0.01"))
        actual_gst = (actual_mdr * GST_RATE).quantize(Decimal("0.01"))
        utr = f"UTR{i:09d}"

        data.settlement.append(_settlement_row(i, order, day, gross, actual_mdr, actual_gst, utr))
        data.bank.append(
            _bank_row(i, day + timedelta(days=1), utr, gross - actual_mdr - actual_gst)
        )
        data.ledger.append(_ledger_row(i, order, day, gross, expected_mdr + expected_gst))
        data.cases.append(
            Case(
                order,
                ["MDR_FEE_VARIANCE", "TIMING_DIFFERENCE"],
                "overcharged and credited a day late -- the multi-label case",
            )
        )

    # --- Phase 4: aggregated payouts (ADR-0019) -----------------------------------------
    # 10 batches settling under one payout UTR each, 3-6 payments per batch. This is what
    # aggregation actually looks like: the bank shows one credit carrying the payout UTR,
    # and every settlement row in the batch carries it too.
    for batch_no in range(10):
        size = 3 + (batch_no % 4)
        day = settle_day()
        payout_utr = f"UTRPAYOUT{batch_no:04d}"
        payout_total = Decimal("0.00")
        members: list[str] = []

        for _ in range(size):
            i = next_id()
            order = f"ord_{i:05d}"
            gross = gross_amount()
            mdr, gst = contracted_fee(gross)
            net = gross - mdr - gst
            payout_total += net
            members.append(f"pay_{i:05d}")

            data.settlement.append(_settlement_row(i, order, day, gross, mdr, gst, payout_utr))
            data.ledger.append(_ledger_row(i, order, day, gross, mdr + gst))
            data.cases.append(Case(order, [], f"member of aggregated payout {payout_utr}"))

        # One bank credit for the whole batch.
        b = next_id()
        data.bank.append(_bank_row(b, day, payout_utr, payout_total))
        data.aggregations.append(
            {
                "payout_utr": payout_utr,
                "expected_method": "SHARED_REFERENCE",
                "expected_status": "RESOLVED",
                "member_count": size,
                "members": sorted(members),
                "total_minor": int(payout_total * 100),
            }
        )

    # 4 payouts where the bank credit carries a reference NO settlement row has, so the
    # shared-reference pass cannot apply and the subset-sum search is the only route.
    for batch_no in range(4):
        size = 3
        day = settle_day()
        payout_total = Decimal("0.00")
        members = []

        for _ in range(size):
            i = next_id()
            order = f"ord_{i:05d}"
            gross = gross_amount()
            mdr, gst = contracted_fee(gross)
            net = gross - mdr - gst
            payout_total += net
            members.append(f"pay_{i:05d}")

            # Each row carries its own unmatched reference, so nothing pairs 1:1 either.
            data.settlement.append(
                _settlement_row(i, order, day, gross, mdr, gst, f"UTRORPHAN{i:07d}")
            )
            data.ledger.append(_ledger_row(i, order, day, gross, mdr + gst))
            data.cases.append(Case(order, [], f"member of unreferenced payout {batch_no}"))

        b = next_id()
        data.bank.append(_bank_row(b, day, f"UTRNOMATCH{batch_no:04d}", payout_total))
        data.aggregations.append(
            {
                "payout_utr": f"UTRNOMATCH{batch_no:04d}",
                "expected_method": "SUBSET_SUM",
                "expected_status": "RESOLVED",
                "member_count": size,
                "members": sorted(members),
                "total_minor": int(payout_total * 100),
            }
        )

    # 2 deliberately ambiguous payouts. Amounts are chosen so that two different sets of
    # settlement rows sum to the credit exactly -- {A} and {B, C} where A = B + C. The
    # engine must refuse to choose rather than pick one.
    for batch_no in range(2):
        day = settle_day()
        part_b = Decimal(rng.randrange(20_000, 80_000)) / Decimal(100)
        part_c = Decimal(rng.randrange(20_000, 80_000)) / Decimal(100)
        whole_a = part_b + part_c

        ambiguous_members = []
        for net_target in (whole_a, part_b, part_c):
            i = next_id()
            order = f"ord_{i:05d}"
            # Work backwards from the desired NET so the sums line up exactly.
            gross = (net_target / (Decimal(1) - MDR_RATE * (Decimal(1) + GST_RATE))).quantize(
                Decimal("0.01")
            )
            mdr, gst = contracted_fee(gross)
            actual_net = gross - mdr - gst
            adjustment = net_target - actual_net
            gross += adjustment  # nudge gross so net lands exactly on target
            mdr, gst = contracted_fee(gross)
            if gross - mdr - gst != net_target:
                gross = net_target + mdr + gst  # exact by construction

            ambiguous_members.append(f"pay_{i:05d}")
            data.settlement.append(
                _settlement_row(i, order, day, gross, mdr, gst, f"UTRAMBIG{i:07d}")
            )
            data.ledger.append(_ledger_row(i, order, day, gross, mdr + gst))
            data.cases.append(Case(order, [], f"member of ambiguous payout {batch_no}"))

        b = next_id()
        data.bank.append(_bank_row(b, day, f"UTRAMBIGUOUS{batch_no:04d}", whole_a))
        data.aggregations.append(
            {
                "payout_utr": f"UTRAMBIGUOUS{batch_no:04d}",
                "expected_method": "SUBSET_SUM",
                "expected_status": "AMBIGUOUS",
                "member_count": 0,
                "members": [],
                "total_minor": int(whole_a * 100),
                "note": "two distinct sets sum to the credit; the engine must not choose",
            }
        )

    # --- 7 malformed rows, to be quarantined rather than ingested ----------------------
    quarantine_expectations: list[dict[str, str]] = []

    i = next_id()
    data.settlement.append(
        {
            **_settlement_row(
                i, f"ord_{i:05d}", BASE_DATE, Decimal("100.00"), Decimal("2.00"),
                Decimal("0.36"), f"UTR{i:09d}",
            ),
            "settlement_date": "31-02-2026",  # a date that does not exist
        }
    )
    quarantine_expectations.append({"source": "PSP_SETTLEMENT", "reason": "INVALID_DATE"})

    i = next_id()
    data.settlement.append(
        {
            **_settlement_row(
                i, f"ord_{i:05d}", BASE_DATE, Decimal("100.00"), Decimal("2.00"),
                Decimal("0.36"), f"UTR{i:09d}",
            ),
            "net_amount": "not-a-number",
        }
    )
    quarantine_expectations.append({"source": "PSP_SETTLEMENT", "reason": "INVALID_AMOUNT"})

    i = next_id()
    data.settlement.append(
        {
            **_settlement_row(
                i, f"ord_{i:05d}", BASE_DATE, Decimal("100.00"), Decimal("2.00"),
                Decimal("0.36"), f"UTR{i:09d}",
            ),
            "order_id": "",
        }
    )
    quarantine_expectations.append(
        {"source": "PSP_SETTLEMENT", "reason": "MISSING_REQUIRED_FIELD"}
    )

    i = next_id()
    data.settlement.append(
        {
            **_settlement_row(
                i, f"ord_{i:05d}", BASE_DATE, Decimal("100.00"), Decimal("2.00"),
                Decimal("0.36"), f"UTR{i:09d}",
            ),
            "currency": "XYZ",
        }
    )
    quarantine_expectations.append(
        {"source": "PSP_SETTLEMENT", "reason": "UNSUPPORTED_CURRENCY"}
    )

    # Excess precision: 100.005 INR cannot be represented in paise, and money.py refuses
    # to round it rather than silently dropping half a paisa.
    i = next_id()
    row = _settlement_row(
        i, f"ord_{i:05d}", BASE_DATE, Decimal("100.00"), Decimal("2.00"),
        Decimal("0.36"), f"UTR{i:09d}",
    )
    row["gross_amount"] = "100.005"
    data.settlement.append(row)
    quarantine_expectations.append({"source": "PSP_SETTLEMENT", "reason": "INVALID_AMOUNT"})

    i = next_id()
    data.bank.append(
        {
            **_bank_row(i, BASE_DATE, f"UTR{i:09d}", Decimal("500.00")),
            "debit_amount": "500.00",  # credit and debit both set
        }
    )
    quarantine_expectations.append({"source": "BANK_STATEMENT", "reason": "MALFORMED_ROW"})

    i = next_id()
    data.ledger.append(
        {
            **_ledger_row(i, f"ord_{i:05d}", BASE_DATE, Decimal("100.00"), Decimal("2.36")),
            "booked_at": "",
        }
    )
    quarantine_expectations.append(
        {"source": "INTERNAL_LEDGER", "reason": "MISSING_REQUIRED_FIELD"}
    )

    data.quarantine_expectations = quarantine_expectations  # type: ignore[attr-defined]
    return data


def write(data: Dataset) -> dict[str, object]:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    files = {
        "settlement.csv": data.settlement,
        "bank.csv": data.bank,
        "ledger.csv": data.ledger,
    }
    for name, rows in files.items():
        path = FIXTURE_DIR / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    expected_by_category: dict[str, int] = {}
    for case in data.cases:
        for code in case.expected_categories:
            expected_by_category[code] = expected_by_category.get(code, 0) + 1

    quarantine = getattr(data, "quarantine_expectations", [])
    manifest = {
        "dataset": "recon_2026_03",
        "seed": SEED,
        "generated_by": "scripts/generate_fixture.py",
        "synthetic": True,
        "caveat": (
            "Synthetic data with discrepancies planted by construction. A match rate "
            "measured here shows the engine implements these rules; it is not evidence "
            "about real settlement files. See ADR-0015."
        ),
        "row_counts": {
            "settlement_csv_rows": len(data.settlement),
            "bank_csv_rows": len(data.bank),
            "ledger_csv_rows": len(data.ledger),
        },
        "expected_quarantined": {
            "total": len(quarantine),
            "by_source": {
                source: sum(1 for q in quarantine if q["source"] == source)
                for source in sorted({q["source"] for q in quarantine})
            },
            "by_reason": {
                reason: sum(1 for q in quarantine if q["reason"] == reason)
                for reason in sorted({q["reason"] for q in quarantine})
            },
        },
        "expected_aggregations": {
            "total": len(data.aggregations),
            "by_expected_status": {
                status: sum(
                    1 for a in data.aggregations if a["expected_status"] == status
                )
                for status in sorted({a["expected_status"] for a in data.aggregations})
            },
            "by_expected_method": {
                method: sum(
                    1 for a in data.aggregations if a["expected_method"] == method
                )
                for method in sorted({a["expected_method"] for a in data.aggregations})
            },
            "resolved_member_total": sum(
                a["member_count"] for a in data.aggregations
                if a["expected_status"] == "RESOLVED"
            ),
            "groups": data.aggregations,
        },
        "expected_cases": {
            "total": len(data.cases),
            "clean": sum(1 for c in data.cases if not c.expected_categories),
            "with_discrepancy": sum(1 for c in data.cases if c.expected_categories),
            "by_category": dict(sorted(expected_by_category.items())),
        },
    }
    (FIXTURE_DIR / "expected.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    manifest = write(build())
    print(json.dumps(manifest, indent=2))
