"""Wire contracts for trust-score reads.

Every score is returned alongside its sample size and cold-start flag. A score without
its denominator invites a reader to trust 1/1 as much as 900/1000, so the API does not
offer the option of showing one without the other.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class TrustCategoryResponse(BaseModel):
    code: str
    display_name: str
    description: str
    severity: str
    tolerance_minor: int = Field(
        description="Per-category absolute tolerance in currency minor units."
    )
    auto_resolvable: bool = Field(
        description=(
            "Policy ceiling. When false this category is never auto-applied, at any score."
        )
    )

    score: Decimal
    sample_size: int = Field(description="Observations the score is computed from.")
    correct_count: int
    min_sample_size: int
    auto_apply_threshold: Decimal
    review_threshold: Decimal
    is_cold_start: bool = Field(
        description="True while sample_size < min_sample_size: the score is not yet evidence."
    )

    gate_decision: str = Field(
        description="What the trust gate currently permits: AUTO_APPLY | HUMAN_REVIEW | BLOCK"
    )
    gate_reason: str


class TrustCategoryListResponse(BaseModel):
    categories: list[TrustCategoryResponse]
    total: int
    # Stated on the response itself rather than in docs, so the caveat travels with the data.
    note: str = Field(
        default=(
            "Phase 1: all scores are cold-start seeds with sample_size 0. No outcome data "
            "has been recorded yet, so no score here reflects measured agent accuracy."
        )
    )
