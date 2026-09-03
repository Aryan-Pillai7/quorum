"""DiscrepancyCategory -- the taxonomy of ways a three-way match can fail.

A table rather than an enum, because each category carries per-category tolerance and
because trust scores hang off it. The taxonomy is seeded in the initial migration so a
freshly created database is immediately usable.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Enum, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import Severity


class DiscrepancyCategory(Base, TimestampMixin):
    __tablename__ = "discrepancy_categories"

    # The code is the primary key rather than a surrogate int: it appears in audit
    # records, API responses and logs, and "AMOUNT_MISMATCH" is legible where "7" is not.
    code: Mapped[str] = mapped_column(String(64), primary_key=True)

    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    severity: Mapped[Severity] = mapped_column(
        Enum(Severity, native_enum=False, length=16), nullable=False
    )

    # Per-category absolute tolerance in minor units. Integer, so comparison stays exact
    # (ADR-0002). A rounding difference of a few paise is noise; a rupee is not.
    tolerance_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    # Whether this category is *eligible* for automation at all. This is a policy
    # ceiling, independent of the trust score: a category marked False is never
    # auto-applied no matter how high its score climbs.
    auto_resolvable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    trust_score = relationship(
        "TrustScore", back_populates="category", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("tolerance_minor >= 0", name="tolerance_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<DiscrepancyCategory {self.code} severity={self.severity}>"
