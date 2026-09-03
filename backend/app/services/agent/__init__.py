"""Claude-backed explanation and classification layer. Phase 3.

This layer explains and proposes. It never writes ledger state directly: every output
passes through the trust gate in app/services/trust.py first.
"""
