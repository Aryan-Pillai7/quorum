"""Source adapters: raw CSV row -> NormalizedRow, or a RowError explaining why not.

Each adapter declares its columns explicitly. Column mappings are the part of an
ingestion pipeline most likely to drift silently, so a missing column is an error rather
than a None that flows downstream and turns into a phantom discrepancy.

Adapters are pure: they take a dict of strings and return a value. No database, no clock,
no I/O. That makes every parsing decision testable in isolation and reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from app.core.errors import MoneyError, UnsupportedCurrencyError
from app.core.money import to_minor_units
from app.models.enums import Direction, QuarantineReason, SourceSystem

# Settlement and bank files are published in IST. Parsed as IST and stored as UTC, so an
# IST settlement date and a UTC bank value date describe the same instant rather than a
# phantom 5h30m date discrepancy.
IST_OFFSET = timedelta(hours=5, minutes=30)

ACCEPTED_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
)


@dataclass(frozen=True)
class RowError:
    """Why one row could not be normalized. Becomes a QuarantinedRow."""

    reason: QuarantineReason
    detail: str


@dataclass(frozen=True)
class NormalizedRow:
    """One source row in Quorum's internal representation."""

    external_id: str
    amount_minor: int
    currency: str
    direction: Direction
    occurred_at: datetime
    counterparty_ref: str | None = None
    order_ref: str | None = None
    gross_amount_minor: int | None = None
    fee_minor: int | None = None
    description: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _require(row: dict[str, str], column: str) -> tuple[str | None, RowError | None]:
    value = (row.get(column) or "").strip()
    if not value:
        return None, RowError(
            QuarantineReason.MISSING_REQUIRED_FIELD, f"column {column!r} is empty or absent"
        )
    return value, None


def _parse_amount(value: str, currency: str, column: str) -> tuple[int | None, RowError | None]:
    try:
        return to_minor_units(value, currency), None
    except UnsupportedCurrencyError as exc:
        return None, RowError(QuarantineReason.UNSUPPORTED_CURRENCY, f"{column}: {exc.message}")
    except MoneyError as exc:
        return None, RowError(QuarantineReason.INVALID_AMOUNT, f"{column}: {exc.message}")


def _parse_timestamp(value: str, column: str) -> tuple[datetime | None, RowError | None]:
    """Parse an IST-published timestamp into an aware UTC datetime."""
    for fmt in ACCEPTED_DATE_FORMATS:
        try:
            naive = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return (naive - IST_OFFSET).replace(tzinfo=UTC), None
    return None, RowError(
        QuarantineReason.INVALID_DATE,
        f"{column}: {value!r} matches none of the accepted formats "
        f"{list(ACCEPTED_DATE_FORMATS)}",
    )


class SourceAdapter:
    """Base adapter. Subclasses declare columns and implement `normalize`."""

    source: ClassVar[SourceSystem]
    required_columns: ClassVar[tuple[str, ...]]

    @classmethod
    def missing_columns(cls, header: list[str]) -> list[str]:
        """Columns the file must have. Checked once per file, before any row is parsed.

        A settlement file whose columns moved should fail as a file, not produce 500
        individually quarantined rows that bury the actual cause.
        """
        present = {h.strip() for h in header}
        return [c for c in cls.required_columns if c not in present]

    @classmethod
    def normalize(cls, row: dict[str, str]) -> tuple[NormalizedRow | None, RowError | None]:
        raise NotImplementedError


class PSPSettlementAdapter(SourceAdapter):
    """Processor settlement report.

    `amount_minor` is the NET settled amount -- what the processor says it actually paid
    out -- so it is directly comparable with the bank credit. Gross and fee are kept
    alongside so a gross/net gap can be attributed to a fee rather than merely observed.
    """

    source = SourceSystem.PSP_SETTLEMENT
    required_columns = (
        "payment_id",
        "order_id",
        "rrn",
        "settlement_utr",
        "settlement_date",
        "gross_amount",
        "mdr_fee",
        "gst_on_fee",
        "net_amount",
        "currency",
    )

    @classmethod
    def normalize(cls, row: dict[str, str]) -> tuple[NormalizedRow | None, RowError | None]:
        currency = (row.get("currency") or "").strip().upper()
        if not currency:
            return None, RowError(
                QuarantineReason.MISSING_REQUIRED_FIELD, "column 'currency' is empty or absent"
            )

        for column in ("payment_id", "order_id", "settlement_utr", "settlement_date"):
            _, err = _require(row, column)
            if err:
                return None, err

        net, err = _parse_amount(row["net_amount"], currency, "net_amount")
        if err:
            return None, err
        gross, err = _parse_amount(row["gross_amount"], currency, "gross_amount")
        if err:
            return None, err
        mdr, err = _parse_amount(row["mdr_fee"], currency, "mdr_fee")
        if err:
            return None, err
        gst, err = _parse_amount(row["gst_on_fee"], currency, "gst_on_fee")
        if err:
            return None, err
        occurred_at, err = _parse_timestamp(row["settlement_date"].strip(), "settlement_date")
        if err:
            return None, err

        return (
            NormalizedRow(
                external_id=row["payment_id"].strip(),
                amount_minor=net,
                gross_amount_minor=gross,
                fee_minor=mdr + gst,
                currency=currency,
                direction=Direction.CREDIT,
                occurred_at=occurred_at,
                counterparty_ref=(row.get("settlement_utr") or "").strip() or None,
                order_ref=row["order_id"].strip(),
                description=(row.get("rrn") or "").strip() or None,
                raw=dict(row),
            ),
            None,
        )


class BankStatementAdapter(SourceAdapter):
    """Bank statement.

    States neither gross nor fee -- it only knows what landed. A row must be a credit or
    a debit, not both and not neither.
    """

    source = SourceSystem.BANK_STATEMENT
    required_columns = (
        "txn_id",
        "value_date",
        "utr",
        "credit_amount",
        "debit_amount",
        "currency",
    )

    @classmethod
    def normalize(cls, row: dict[str, str]) -> tuple[NormalizedRow | None, RowError | None]:
        currency = (row.get("currency") or "").strip().upper()
        if not currency:
            return None, RowError(
                QuarantineReason.MISSING_REQUIRED_FIELD, "column 'currency' is empty or absent"
            )

        txn_id, err = _require(row, "txn_id")
        if err:
            return None, err
        value_date, err = _require(row, "value_date")
        if err:
            return None, err

        credit_raw = (row.get("credit_amount") or "").strip()
        debit_raw = (row.get("debit_amount") or "").strip()
        credit_set = credit_raw not in ("", "0", "0.00")
        debit_set = debit_raw not in ("", "0", "0.00")

        # Both or neither means the row does not say what happened. Guessing here would
        # invent money in one direction or the other.
        if credit_set == debit_set:
            return None, RowError(
                QuarantineReason.MALFORMED_ROW,
                f"exactly one of credit_amount/debit_amount must be set, got "
                f"credit={credit_raw!r} debit={debit_raw!r}",
            )

        column = "credit_amount" if credit_set else "debit_amount"
        amount, err = _parse_amount(credit_raw if credit_set else debit_raw, currency, column)
        if err:
            return None, err

        occurred_at, err = _parse_timestamp(value_date, "value_date")
        if err:
            return None, err

        return (
            NormalizedRow(
                external_id=txn_id,
                amount_minor=amount,
                currency=currency,
                direction=Direction.CREDIT if credit_set else Direction.DEBIT,
                occurred_at=occurred_at,
                counterparty_ref=(row.get("utr") or "").strip() or None,
                order_ref=None,  # a bank statement carries no commerce reference
                description=(row.get("narration") or "").strip() or None,
                raw=dict(row),
            ),
            None,
        )


class InternalLedgerAdapter(SourceAdapter):
    """Internal ledger export.

    Books gross and the fee it *expects* under the contracted rate. `amount_minor` is
    normalized to the expected NET so it is directly comparable with the other two
    sources; the gap between expected and actual fee is what makes MDR variance
    detectable as its own finding rather than as an unexplained amount mismatch.
    """

    source = SourceSystem.INTERNAL_LEDGER
    required_columns = (
        "entry_id",
        "order_id",
        "booked_at",
        "gross_amount",
        "expected_fee",
        "currency",
    )

    @classmethod
    def normalize(cls, row: dict[str, str]) -> tuple[NormalizedRow | None, RowError | None]:
        currency = (row.get("currency") or "").strip().upper()
        if not currency:
            return None, RowError(
                QuarantineReason.MISSING_REQUIRED_FIELD, "column 'currency' is empty or absent"
            )

        for column in ("entry_id", "order_id", "booked_at"):
            _, err = _require(row, column)
            if err:
                return None, err

        gross, err = _parse_amount(row["gross_amount"], currency, "gross_amount")
        if err:
            return None, err
        expected_fee, err = _parse_amount(row["expected_fee"], currency, "expected_fee")
        if err:
            return None, err
        occurred_at, err = _parse_timestamp(row["booked_at"].strip(), "booked_at")
        if err:
            return None, err

        return (
            NormalizedRow(
                external_id=row["entry_id"].strip(),
                amount_minor=gross - expected_fee,
                gross_amount_minor=gross,
                fee_minor=expected_fee,
                currency=currency,
                direction=Direction.CREDIT,
                occurred_at=occurred_at,
                counterparty_ref=None,  # a ledger entry carries no bank-network reference
                order_ref=row["order_id"].strip(),
                description=(row.get("entry_type") or "").strip() or None,
                raw=dict(row),
            ),
            None,
        )


ADAPTERS: dict[SourceSystem, type[SourceAdapter]] = {
    SourceSystem.PSP_SETTLEMENT: PSPSettlementAdapter,
    SourceSystem.BANK_STATEMENT: BankStatementAdapter,
    SourceSystem.INTERNAL_LEDGER: InternalLedgerAdapter,
}
