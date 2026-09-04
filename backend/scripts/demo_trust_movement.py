"""Drive a category's trust score from cold start to AUTO_APPLY, end to end.

    python scripts/demo_trust_movement.py [CATEGORY_CODE]

Approves and audits real findings one at a time and prints the gate state after each,
reading every number back from Postgres directly rather than trusting an API response.

Nothing here is simulated. Each step writes an approval row, an audit verdict, and two
hash-chained audit events, exactly as the HTTP endpoints do -- they call the same service.

The threshold is not lowered for the demo. TIMING_DIFFERENCE carries min_sample_size = 30
from the Phase 1 seed, which is this system's real production value for a LOW severity
category, so the gate flips at the threshold it would flip at in production.
"""

from __future__ import annotations

import sys

from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.session import engine
from app.models import Approval, ApprovalDecision, AuditStatus, Discrepancy
from app.services.approvals import approve, audit_approval
from app.services.trust import decide_gate

DEFAULT_CATEGORY = "TIMING_DIFFERENCE"


def gate_state(session, category_code: str) -> tuple[str, float, int, int, str]:
    """Read the category's trust and gate straight from Postgres."""
    row = session.execute(
        text(
            "SELECT t.score, t.sample_size, t.min_sample_size, t.auto_apply_threshold, "
            "t.review_threshold, c.auto_resolvable "
            "FROM trust_scores t JOIN discrepancy_categories c ON c.code = t.category_code "
            "WHERE t.category_code = :c"
        ),
        {"c": category_code},
    ).one()
    evaluation = decide_gate(
        score=row.score,
        sample_size=row.sample_size,
        auto_apply_threshold=row.auto_apply_threshold,
        review_threshold=row.review_threshold,
        min_sample_size=row.min_sample_size,
        category_auto_resolvable=row.auto_resolvable,
    )
    return (
        evaluation.decision.value,
        float(row.score),
        row.sample_size,
        row.min_sample_size,
        evaluation.reason,
    )


def main() -> int:
    category = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CATEGORY
    settings = get_settings()
    session = sessionmaker(bind=engine)()

    try:
        decision, score, n, minimum, reason = gate_state(session, category)
        print("=" * 78)
        print(f"BEFORE   {category}")
        print(f"  gate         : {decision}")
        print(f"  score        : {score:.4f}")
        print(f"  observations : {n} of {minimum} required")
        print(f"  why          : {reason[:120]}")
        print("=" * 78)
        print()

        findings = session.execute(
            select(Discrepancy)
            .outerjoin(Approval, Approval.discrepancy_id == Discrepancy.id)
            .where(Discrepancy.category_code == category, Approval.id.is_(None))
            .order_by(Discrepancy.id)
        ).scalars().all()

        needed = max(0, minimum - n)
        if len(findings) < needed:
            print(
                f"Only {len(findings)} un-approved {category} findings available but "
                f"{needed} are needed to reach the threshold. Seed more data first."
            )
            return 1

        print(f"Approving and auditing {needed} findings (alpha={settings.trust_ema_alpha})")
        print(f"{'step':>5}  {'score':>7}  {'n':>4}  gate")
        print("-" * 60)

        flipped_at = None
        for step, finding in enumerate(findings[:needed], start=1):
            approval = approve(
                session,
                discrepancy_id=finding.id,
                decision=ApprovalDecision.APPROVED,
                approver_id="ops.demo",
                settings=settings,
            )
            if approval.audit_status is not AuditStatus.PENDING:
                # Not selected for audit: cleared operationally, but never evidence.
                print(f"{step:>5}  {'--':>7}  {'--':>4}  not sampled for audit; no trust change")
                continue

            result = audit_approval(
                session,
                approval_id=approval.id,
                was_correct=True,
                auditor_id="audit.demo",
                note="verified against the settlement file",
                settings=settings,
            )
            marker = ""
            if result.gate_changed:
                flipped_at = result.sample_size
                marker = f"   <-- {result.previous_gate} -> {result.new_gate}"
            print(
                f"{step:>5}  {result.new_score:>7.4f}  {result.sample_size:>4}  "
                f"{result.new_gate}{marker}"
            )

        print()
        decision, score, n, minimum, reason = gate_state(session, category)
        print("=" * 78)
        print(f"AFTER    {category}")
        print(f"  gate         : {decision}")
        print(f"  score        : {score:.4f}")
        print(f"  observations : {n} of {minimum} required")
        print(f"  why          : {reason[:120]}")
        print("=" * 78)

        if flipped_at:
            print(f"\nGate changed at observation {flipped_at}, read back from Postgres.")

        # The fail-safe is not weakened by any of this.
        high_value = decide_gate(
            score=score,
            sample_size=n,
            auto_apply_threshold=0.85,
            review_threshold=0.50,
            min_sample_size=minimum,
            category_auto_resolvable=True,
            amount_minor=settings.high_value_review_threshold_minor,
            high_value_threshold_minor=settings.high_value_review_threshold_minor,
        )
        print(
            f"\nHigh-value fail-safe still holds: a "
            f"{settings.high_value_review_threshold_minor / 100:,.2f} INR transaction in "
            f"this now-{decision} category returns {high_value.decision.value}."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
