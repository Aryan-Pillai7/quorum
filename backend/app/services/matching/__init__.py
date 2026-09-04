"""Deterministic matching engine.

SEALED PACKAGE -- see ADR-0004. Nothing here may import the agent layer, the Anthropic
SDK, or any network client. Matching must be reproducible from its inputs alone, with
no API key and no network, forever.

Enforced by tests/unit/test_core_purity.py, not by good intentions.
"""

from app.services.matching.engine import ReconciliationResult, reconcile
from app.services.matching.rules import (
    RULE_ORDER,
    Finding,
    LegView,
    MatchContext,
    classify,
)

__all__ = [
    "RULE_ORDER",
    "Finding",
    "LegView",
    "MatchContext",
    "ReconciliationResult",
    "classify",
    "reconcile",
]
