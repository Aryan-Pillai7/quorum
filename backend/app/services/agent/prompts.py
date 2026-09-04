"""Prompt construction for the explanation agent.

The anti-hallucination strategy here is structural rather than instructional: the model is
given only the field comparisons the deterministic engine already recorded, and is asked
to restate them in plain language. It is not asked to work out what happened -- the rules
already did that, and their answer is the ground truth.

So the prompt carries no invented context: no merchant names, no customer history, no
"typical" behaviour. If a fact is not in the evidence dict the engine wrote, it is not in
the prompt, and a model that introduces it is producing something a reviewer can catch
against the same evidence shown beside it in the UI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

SYSTEM_INSTRUCTION = """\
You are a payment reconciliation analyst. You explain, in plain language, discrepancies \
that a deterministic rules engine has ALREADY detected and classified.

Rules you must follow:

1. Use ONLY the facts in the `evidence` and `summary` fields given to you. Never invent \
merchant names, customer details, dates, amounts, causes, or history that is not present.
2. The rule engine's classification is correct. Do not dispute or re-classify it. Your job \
is to make it readable, not to second-guess it.
3. Amounts are in minor units (paise; 100 paise = 1 rupee). Convert to rupees when you \
write, and say "INR".
4. Write for a finance operations reviewer who will act on this. Be concrete and specific \
about which of the three sources disagrees and by how much.
5. The corrective action is ADVISORY. A human decides. Never write as though the action \
has been or will be taken automatically.
6. If the evidence is insufficient to explain the discrepancy, say so plainly in the \
explanation rather than speculating. That is an acceptable and useful answer.
7. Keep each explanation under 600 characters and each corrective action under 400.

Return one entry for EVERY discrepancy_id you are given. Do not invent ids.\
"""


@dataclass(frozen=True)
class DiscrepancyPromptItem:
    """One discrepancy, reduced to exactly what the model is allowed to see."""

    discrepancy_id: str
    rule_id: str
    summary: str
    evidence: dict[str, Any]
    delta_minor: int
    match_key: str
    currency: str = "INR"

    def to_payload(self) -> dict[str, Any]:
        return {
            "discrepancy_id": self.discrepancy_id,
            "reference": self.match_key,
            "detected_by_rule": self.rule_id,
            "rule_summary": self.summary,
            "amount_delta_minor": self.delta_minor,
            "currency": self.currency,
            "evidence": self.evidence,
        }


def build_batch_prompt(
    *,
    category_code: str,
    category_display_name: str,
    category_description: str,
    items: list[DiscrepancyPromptItem],
) -> str:
    """One prompt covering every discrepancy in a single category.

    Batching by category is what keeps the call count proportional to the number of
    *kinds* of problem rather than the number of rows (ADR-0018). It also gives the model
    one consistent frame per call instead of re-establishing context per row.
    """
    payload = {
        "category": {
            "code": category_code,
            "name": category_display_name,
            "definition": category_description,
        },
        "discrepancies": [item.to_payload() for item in items],
    }

    return (
        f"All {len(items)} discrepancies below were classified as "
        f"{category_code} ({category_display_name}) by the deterministic rules engine.\n\n"
        f"Category definition: {category_description}\n\n"
        f"Explain each one for a finance operations reviewer, and draft an advisory "
        f"corrective action for each.\n\n"
        f"Data:\n{json.dumps(payload, indent=2, default=str)}\n\n"
        f"Return JSON with an `explanations` array containing exactly "
        f"{len(items)} entries, one per discrepancy_id above."
    )
