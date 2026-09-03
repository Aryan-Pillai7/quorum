"""TrustScore -- per-category automation trust, and the gate it feeds.

A trust score answers exactly one question: is the AI layer allowed to act on this
category of discrepancy without a human? It is derived from observed outcomes only --
how often the agent was right about this category -- never asserted or hand-tuned.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class TrustScore(Base, TimestampMixin):
    """One row per discrepancy category.

    `sample_size` and `correct_count` are stored rather than just the ratio so the score
    is always auditable and its confidence always visible: 1/1 and 900/1000 are both
    "1.0" and "0.9", but only one of them means anything.
    """

    __tablename__ = "trust_scores"

    category_code: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("discrepancy_categories.code", ondelete="CASCADE"),
        primary_key=True,
    )

    # correct_count / sample_size, materialized. 0 at cold start, which the gate treats
    # as "unknown", never as "bad".
    score: Mapped[float] = mapped_column(
        Numeric(5, 4), nullable=False, default=0, server_default="0"
    )

    sample_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    correct_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Per-category thresholds so a low-risk category can automate earlier than a
    # high-risk one without a global policy change.
    auto_apply_threshold: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    review_threshold: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)

    # Cold-start floor: below this many observations the score is not evidence, and the
    # gate refuses AUTO_APPLY regardless of how good the ratio looks.
    min_sample_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50, server_default="50"
    )

    last_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    category = relationship("DiscrepancyCategory", back_populates="trust_score")

    __table_args__ = (
        CheckConstraint("score >= 0 AND score <= 1", name="score_in_range"),
        CheckConstraint("sample_size >= 0", name="sample_size_non_negative"),
        CheckConstraint(
            "correct_count >= 0 AND correct_count <= sample_size",
            name="correct_count_within_sample",
        ),
        CheckConstraint("min_sample_size >= 1", name="min_sample_size_positive"),
        CheckConstraint(
            "auto_apply_threshold >= 0 AND auto_apply_threshold <= 1",
            name="auto_apply_threshold_in_range",
        ),
        CheckConstraint(
            "review_threshold >= 0 AND review_threshold < auto_apply_threshold",
            name="review_below_auto_apply",
        ),
    )

    @property
    def is_cold_start(self) -> bool:
        """True while there is not yet enough evidence for the score to mean anything."""
        return self.sample_size < self.min_sample_size

    def __repr__(self) -> str:
        return (
            f"<TrustScore {self.category_code} score={self.score} "
            f"n={self.sample_size} cold_start={self.is_cold_start}>"
        )
