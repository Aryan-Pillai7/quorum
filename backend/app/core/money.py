"""Money handling. Integer minor units only. See ADR-0002.

Reconciliation is equality comparison on money, so representation is not a detail.
Two rules hold everywhere in Quorum:

1. `float` is never accepted. Not coerced, not rounded -- rejected, loudly.
2. Precision is never silently discarded. "100.005" in a 2-decimal currency raises
   rather than quietly becoming 100.00 or 100.01, because both answers are wrong and
   one of them is off by a paisa that will resurface as an unexplained discrepancy.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.core.errors import MoneyError, UnsupportedCurrencyError

# ISO 4217 minor-unit exponents for the currencies Quorum handles today.
# Deliberately a short, explicit list: an unknown currency is an error, not a guess at 2.
MINOR_UNIT_EXPONENT: dict[str, int] = {
    "INR": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "AED": 2,
    "SGD": 2,
    "JPY": 0,
    "KRW": 0,
    "BHD": 3,
    "KWD": 3,
}

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
# Optional sign, digits with optional comma grouping, optional fractional part.
_AMOUNT_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{2,3})*(?:\.\d+)?$|^[+-]?\d+(?:\.\d+)?$")


def normalize_currency(currency: str) -> str:
    """Uppercase and validate an ISO 4217 alpha code."""
    if not isinstance(currency, str):
        raise UnsupportedCurrencyError(
            f"currency must be a 3-letter string, got {type(currency).__name__}"
        )
    code = currency.strip().upper()
    if not _CURRENCY_RE.match(code):
        raise UnsupportedCurrencyError(f"not a valid ISO 4217 alpha code: {currency!r}")
    return code


def exponent_for(currency: str) -> int:
    """Minor-unit exponent for a currency. Raises on anything not explicitly supported."""
    code = normalize_currency(currency)
    try:
        return MINOR_UNIT_EXPONENT[code]
    except KeyError:
        raise UnsupportedCurrencyError(
            f"{code} has no configured minor-unit exponent",
            details={"currency": code, "supported": sorted(MINOR_UNIT_EXPONENT)},
        ) from None


def to_minor_units(amount: str | int | Decimal, currency: str) -> int:
    """Convert a source amount to integer minor units.

    Accepts str (as it arrives from CSV), int (already major units), or Decimal.
    Rejects float outright -- see ADR-0002.
    """
    exponent = exponent_for(currency)

    if isinstance(amount, bool):  # bool is an int subclass; never a valid amount
        raise MoneyError("amount must not be a bool")
    if isinstance(amount, float):
        raise MoneyError(
            "float amounts are rejected: binary floats cannot represent decimal money "
            "exactly. Pass the original string, an int, or a Decimal.",
            details={"received": repr(amount)},
        )

    if isinstance(amount, int):
        value = Decimal(amount)
    elif isinstance(amount, Decimal):
        value = amount
    elif isinstance(amount, str):
        cleaned = amount.strip().replace(" ", "")
        if not _AMOUNT_RE.match(cleaned):
            raise MoneyError(
                f"unparseable amount: {amount!r}", details={"expected": "e.g. -1,234.56"}
            )
        try:
            value = Decimal(cleaned.replace(",", ""))
        except InvalidOperation:
            raise MoneyError(f"unparseable amount: {amount!r}") from None
    else:
        raise MoneyError(f"amount must be str, int, or Decimal, got {type(amount).__name__}")

    if not value.is_finite():
        raise MoneyError(f"amount is not finite: {amount!r}")

    scaled = value.scaleb(exponent)
    if scaled != scaled.to_integral_value():
        raise MoneyError(
            f"{value} has more precision than {normalize_currency(currency)} supports "
            f"({exponent} decimal places); refusing to round",
            details={"amount": str(value), "currency": normalize_currency(currency)},
        )
    return int(scaled)


def from_minor_units(minor: int, currency: str) -> Decimal:
    """Convert integer minor units back to a major-unit Decimal."""
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise MoneyError(f"minor units must be int, got {type(minor).__name__}")
    exponent = exponent_for(currency)
    return Decimal(minor).scaleb(-exponent)


def format_minor(minor: int, currency: str) -> str:
    """Human-readable rendering for logs, API responses, and audit records."""
    code = normalize_currency(currency)
    value = from_minor_units(minor, code)
    exponent = exponent_for(code)
    return f"{value:,.{exponent}f} {code}"
