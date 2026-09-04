"""CSV source adapters: normalization and the reasons a row gets quarantined."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.enums import Direction, QuarantineReason, SourceSystem
from app.services.ingestion.adapters import (
    ADAPTERS,
    BankStatementAdapter,
    InternalLedgerAdapter,
    PSPSettlementAdapter,
)


def settlement_row(**overrides) -> dict[str, str]:
    base = {
        "payment_id": "pay_00001",
        "order_id": "ord_00001",
        "rrn": "RRN000000001",
        "settlement_utr": "UTR000000001",
        "settlement_date": "2026-03-05 10:30:00",
        "gross_amount": "1000.00",
        "mdr_fee": "20.00",
        "gst_on_fee": "3.60",
        "net_amount": "976.40",
        "currency": "INR",
    }
    return {**base, **overrides}


def bank_row(**overrides) -> dict[str, str]:
    base = {
        "txn_id": "bnk_00001",
        "value_date": "2026-03-05 00:00:00",
        "utr": "UTR000000001",
        "narration": "NEFT SETTLEMENT",
        "credit_amount": "976.40",
        "debit_amount": "",
        "currency": "INR",
    }
    return {**base, **overrides}


def ledger_row(**overrides) -> dict[str, str]:
    base = {
        "entry_id": "led_00001",
        "order_id": "ord_00001",
        "booked_at": "2026-03-05 10:30:00",
        "gross_amount": "1000.00",
        "expected_fee": "23.60",
        "currency": "INR",
        "entry_type": "SALE",
    }
    return {**base, **overrides}


# --- normalization --------------------------------------------------------------------


def test_settlement_normalizes_to_net_with_gross_and_fee_alongside():
    row, error = PSPSettlementAdapter.normalize(settlement_row())
    assert error is None
    assert row.amount_minor == 97_640  # net, comparable with the bank credit
    assert row.gross_amount_minor == 100_000
    assert row.fee_minor == 2_360  # mdr + gst
    assert row.order_ref == "ord_00001"
    assert row.counterparty_ref == "UTR000000001"
    assert row.direction is Direction.CREDIT


def test_ledger_normalizes_to_expected_net_so_all_three_are_comparable():
    """The point of the normalization: one subtraction makes the sources comparable."""
    psp, _ = PSPSettlementAdapter.normalize(settlement_row())
    ledger, error = InternalLedgerAdapter.normalize(ledger_row())
    assert error is None
    assert ledger.amount_minor == 97_640
    assert ledger.amount_minor == psp.amount_minor
    assert ledger.gross_amount_minor == 100_000
    assert ledger.fee_minor == 2_360


def test_bank_normalizes_a_credit():
    row, error = BankStatementAdapter.normalize(bank_row())
    assert error is None
    assert row.amount_minor == 97_640
    assert row.direction is Direction.CREDIT
    assert row.order_ref is None  # a bank statement carries no commerce reference


def test_bank_normalizes_a_debit():
    row, error = BankStatementAdapter.normalize(
        bank_row(credit_amount="", debit_amount="500.00")
    )
    assert error is None
    assert row.direction is Direction.DEBIT
    assert row.amount_minor == 50_000


def test_timestamps_are_parsed_as_ist_and_stored_as_utc():
    """10:30 IST is 05:00 UTC. Storing the wall clock would invent date discrepancies."""
    row, _ = PSPSettlementAdapter.normalize(settlement_row())
    assert row.occurred_at == datetime(2026, 3, 5, 5, 0, tzinfo=UTC)
    assert row.occurred_at.tzinfo is not None


@pytest.mark.parametrize(
    "date_text",
    ["2026-03-05 10:30:00", "2026-03-05T10:30:00", "2026-03-05", "05/03/2026", "05-03-2026"],
)
def test_accepted_date_formats_all_parse(date_text):
    _, error = PSPSettlementAdapter.normalize(settlement_row(settlement_date=date_text))
    assert error is None


def test_the_original_row_is_preserved_verbatim():
    """Normalization decisions must remain checkable against what actually arrived."""
    source = settlement_row()
    row, _ = PSPSettlementAdapter.normalize(source)
    assert row.raw == source


# --- quarantine reasons ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"settlement_date": "31-02-2026"}, QuarantineReason.INVALID_DATE),
        ({"settlement_date": "yesterday"}, QuarantineReason.INVALID_DATE),
        ({"net_amount": "not-a-number"}, QuarantineReason.INVALID_AMOUNT),
        ({"gross_amount": "100.005"}, QuarantineReason.INVALID_AMOUNT),
        ({"order_id": ""}, QuarantineReason.MISSING_REQUIRED_FIELD),
        ({"payment_id": "   "}, QuarantineReason.MISSING_REQUIRED_FIELD),
        ({"currency": ""}, QuarantineReason.MISSING_REQUIRED_FIELD),
        ({"currency": "XYZ"}, QuarantineReason.UNSUPPORTED_CURRENCY),
    ],
)
def test_settlement_rows_are_quarantined_with_a_specific_reason(overrides, expected_reason):
    row, error = PSPSettlementAdapter.normalize(settlement_row(**overrides))
    assert row is None
    assert error.reason is expected_reason
    assert error.detail  # a reason without a detail is a shrug, not a lead


def test_excess_precision_is_quarantined_rather_than_rounded():
    """100.005 INR cannot be held in paise. Rounding it loses half a paisa silently."""
    _, error = PSPSettlementAdapter.normalize(settlement_row(gross_amount="100.005"))
    assert error.reason is QuarantineReason.INVALID_AMOUNT
    assert "refusing to round" in error.detail


@pytest.mark.parametrize(
    ("credit", "debit"),
    [("976.40", "500.00"), ("", ""), ("0.00", "0.00")],
)
def test_a_bank_row_must_say_exactly_one_of_credit_or_debit(credit, debit):
    """Both or neither means the row does not say what happened. Guessing invents money."""
    row, error = BankStatementAdapter.normalize(
        bank_row(credit_amount=credit, debit_amount=debit)
    )
    assert row is None
    assert error.reason is QuarantineReason.MALFORMED_ROW


def test_ledger_missing_booked_at_is_quarantined():
    row, error = InternalLedgerAdapter.normalize(ledger_row(booked_at=""))
    assert row is None
    assert error.reason is QuarantineReason.MISSING_REQUIRED_FIELD


# --- file-level column checks ---------------------------------------------------------


def test_missing_columns_are_reported_for_the_whole_file():
    """A file whose columns moved should fail as a file, not as 500 quarantined rows."""
    missing = PSPSettlementAdapter.missing_columns(["payment_id", "order_id"])
    assert "settlement_utr" in missing
    assert "net_amount" in missing
    assert "payment_id" not in missing


def test_a_complete_header_reports_nothing_missing():
    assert PSPSettlementAdapter.missing_columns(list(settlement_row().keys())) == []


def test_header_whitespace_is_tolerated():
    header = [f"  {column}  " for column in settlement_row()]
    assert PSPSettlementAdapter.missing_columns(header) == []


def test_every_source_has_an_adapter():
    assert set(ADAPTERS) == set(SourceSystem)
    for source, adapter in ADAPTERS.items():
        assert adapter.source is source
        assert adapter.required_columns
