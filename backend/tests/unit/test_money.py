"""Money handling. See ADR-0002: integer minor units, no float, no silent rounding."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.errors import MoneyError, UnsupportedCurrencyError
from app.core.money import (
    exponent_for,
    format_minor,
    from_minor_units,
    to_minor_units,
)


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("1234.56", "INR", 123456),
        ("1,234.56", "INR", 123456),
        ("12,34,567.89", "INR", 123456789),  # Indian lakh/crore grouping
        ("1,234,567.89", "USD", 123456789),  # Western grouping
        ("-1,234.56", "INR", -123456),
        ("0.00", "INR", 0),
        ("100", "JPY", 100),  # zero-decimal currency
        ("0.001", "BHD", 1),  # three-decimal currency
        (100, "INR", 10000),  # int is major units
        (Decimal("12.34"), "USD", 1234),
        ("  1234.56  ", "INR", 123456),  # source files carry stray whitespace
    ],
)
def test_to_minor_units_converts_exactly(amount, currency, expected):
    assert to_minor_units(amount, currency) == expected


def test_float_is_rejected_rather_than_coerced():
    """0.1 + 0.2 != 0.3 in binary float. Reconciliation cannot afford that."""
    with pytest.raises(MoneyError, match="float amounts are rejected"):
        to_minor_units(1234.56, "INR")


def test_bool_is_rejected_despite_being_an_int():
    with pytest.raises(MoneyError, match="must not be a bool"):
        to_minor_units(True, "INR")


def test_excess_precision_raises_instead_of_rounding():
    """A dropped paisa reappears later as an unexplained discrepancy. Refuse it."""
    with pytest.raises(MoneyError, match="refusing to round"):
        to_minor_units("100.005", "INR")


def test_zero_decimal_currency_rejects_fractional_input():
    with pytest.raises(MoneyError, match="refusing to round"):
        to_minor_units("100.50", "JPY")


@pytest.mark.parametrize("bad", ["abc", "12.34.56", "1,23,,456", "", "1e5", "₹100"])
def test_unparseable_amounts_raise(bad):
    with pytest.raises(MoneyError):
        to_minor_units(bad, "INR")


def test_unknown_currency_raises_rather_than_assuming_two_decimals():
    with pytest.raises(UnsupportedCurrencyError, match="no configured minor-unit exponent"):
        to_minor_units("100.00", "XYZ")


@pytest.mark.parametrize("bad", ["IN", "INRR", "12R", ""])
def test_malformed_currency_codes_raise(bad):
    with pytest.raises(UnsupportedCurrencyError):
        exponent_for(bad)


def test_currency_code_is_case_insensitive():
    assert to_minor_units("100.00", "inr") == 10000


@pytest.mark.parametrize(
    ("value", "currency"), [("1234.56", "INR"), ("-99.99", "USD"), ("100", "JPY"), ("1.234", "KWD")]
)
def test_round_trip_is_lossless(value, currency):
    minor = to_minor_units(value, currency)
    assert from_minor_units(minor, currency) == Decimal(value)


@pytest.mark.parametrize(
    ("minor", "currency", "expected"),
    [
        (123456, "INR", "1,234.56 INR"),
        (-123456, "INR", "-1,234.56 INR"),
        (100, "JPY", "100 JPY"),
        (0, "USD", "0.00 USD"),
        (1234567890, "INR", "12,345,678.90 INR"),
    ],
)
def test_format_minor_renders_with_currency(minor, currency, expected):
    assert format_minor(minor, currency) == expected


def test_from_minor_units_rejects_non_int():
    with pytest.raises(MoneyError, match="must be int"):
        from_minor_units(12.34, "INR")
