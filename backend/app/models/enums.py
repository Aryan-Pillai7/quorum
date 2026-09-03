"""Domain enumerations.

Persisted as VARCHAR + CHECK rather than native Postgres ENUM (ADR-0006): adding a
value is an ordinary migration, and raw SQL dumps stay readable for auditors who will
never run the app.
"""

from __future__ import annotations

from enum import StrEnum


class SourceSystem(StrEnum):
    """The three independent records Quorum reconciles against each other.

    Two of three agreeing is a lead, not a reconciliation.
    """

    PSP_SETTLEMENT = "PSP_SETTLEMENT"
    BANK_STATEMENT = "BANK_STATEMENT"
    INTERNAL_LEDGER = "INTERNAL_LEDGER"


class Direction(StrEnum):
    CREDIT = "CREDIT"
    DEBIT = "DEBIT"


class TransactionStatus(StrEnum):
    UNMATCHED = "UNMATCHED"  # ingested, no match attempted or none found
    MATCHED = "MATCHED"      # belongs to a MatchRecord
    EXCEPTION = "EXCEPTION"  # matched but carrying an unresolved discrepancy


class MatchStatus(StrEnum):
    FULL = "FULL"        # all three legs present and in agreement
    PARTIAL = "PARTIAL"  # two of three legs; the third is missing
    BROKEN = "BROKEN"    # legs present but in material disagreement


class MatchStrategy(StrEnum):
    """Which deterministic rule produced the match. Recorded on every MatchRecord so a
    result can always be traced back to the rule that caused it."""

    EXACT_REFERENCE = "EXACT_REFERENCE"
    REFERENCE_AMOUNT_TOLERANCE = "REFERENCE_AMOUNT_TOLERANCE"
    AMOUNT_DATE_WINDOW = "AMOUNT_DATE_WINDOW"
    MANUAL = "MANUAL"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class GateDecision(StrEnum):
    """What the trust layer permits for a given discrepancy category."""

    AUTO_APPLY = "AUTO_APPLY"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    BLOCK = "BLOCK"


class ActorType(StrEnum):
    """Who caused an audited action. Kept distinct so 'the AI did it' is always
    separable from 'a person did it' when reading the audit trail."""

    SYSTEM = "SYSTEM"
    AGENT = "AGENT"
    USER = "USER"
