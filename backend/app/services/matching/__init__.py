"""Deterministic matching engine. Phase 2.

SEALED PACKAGE -- see ADR-0004. Nothing here may import the agent layer, the Anthropic
SDK, or any network client. Matching must be reproducible from its inputs alone, with
no API key and no network, forever.

Enforced by tests/unit/test_core_purity.py, not by good intentions.
"""
