"""The contract the agent layer must satisfy before its output touches anything.

Model output is untrusted input. It is parsed into these types and validated before it
reaches the database or an API response; anything that fails validation is dropped and
counted, never patched up to look plausible.

Two words are deliberately kept apart in this file:

- `model_confidence` is what the model says about its own answer. It is self-reported and
  worth very little on its own.
- a **trust score** is what Quorum has measured about this category over time.

Only the second one gates anything. Naming them the same would invite exactly the
confusion the trust layer exists to prevent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ModelConfidence = Literal["low", "medium", "high"]

# Explanations are read in a review queue, not a chat window. Long ones get skimmed, and
# a skimmed explanation is worse than a short one.
MAX_EXPLANATION_CHARS = 600
MAX_ACTION_CHARS = 400


class AgentExplanation(BaseModel):
    """One explained discrepancy, as returned by the model and validated here."""

    discrepancy_id: str = Field(description="Must be one of the ids sent in the request.")
    explanation: str = Field(
        description="Plain-language account of what went wrong, using only the given facts."
    )
    corrective_action: str = Field(
        description="What a human should do about it. Advisory only; never auto-applied."
    )
    model_confidence: ModelConfidence = Field(
        description=(
            "The model's own claim about this answer. NOT a trust score, and it gates "
            "nothing."
        )
    )

    @field_validator("explanation", "corrective_action")
    @classmethod
    def _non_empty_and_bounded(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @field_validator("explanation")
    @classmethod
    def _explanation_length(cls, value: str) -> str:
        if len(value) > MAX_EXPLANATION_CHARS:
            raise ValueError(f"explanation exceeds {MAX_EXPLANATION_CHARS} characters")
        return value

    @field_validator("corrective_action")
    @classmethod
    def _action_length(cls, value: str) -> str:
        if len(value) > MAX_ACTION_CHARS:
            raise ValueError(f"corrective_action exceeds {MAX_ACTION_CHARS} characters")
        return value


class AgentBatchResponse(BaseModel):
    """The whole response for one category batch."""

    explanations: list[AgentExplanation]


# The JSON schema handed to the model. Kept explicit rather than derived, because the
# model sees this and it should read as instructions, not as a serialized class.
RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "explanations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "discrepancy_id": {"type": "string"},
                    "explanation": {"type": "string"},
                    "corrective_action": {"type": "string"},
                    "model_confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": [
                    "discrepancy_id",
                    "explanation",
                    "corrective_action",
                    "model_confidence",
                ],
            },
        }
    },
    "required": ["explanations"],
}
