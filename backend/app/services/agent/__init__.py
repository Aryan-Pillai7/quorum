"""Gemini-backed explanation layer (ADR-0016).

This layer explains and proposes. It never writes ledger state directly: every output
passes through the trust gate in app/services/trust.py first, and in Phase 3 every
output is advisory because no category has earned automation yet.
"""

from app.services.agent.client import (
    AgentUnavailableError,
    GeminiClient,
    ModelCall,
    agent_availability,
    parse_json_payload,
)
from app.services.agent.explain import (
    BatchTelemetry,
    ExplainedDiscrepancy,
    ExplanationRun,
    explain_discrepancies,
    validate_batch_response,
)
from app.services.agent.schema import AgentBatchResponse, AgentExplanation

__all__ = [
    "AgentBatchResponse",
    "AgentExplanation",
    "AgentUnavailableError",
    "BatchTelemetry",
    "ExplainedDiscrepancy",
    "ExplanationRun",
    "GeminiClient",
    "ModelCall",
    "agent_availability",
    "explain_discrepancies",
    "parse_json_payload",
    "validate_batch_response",
]
