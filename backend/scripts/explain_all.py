"""Generate agent explanations for every unexplained discrepancy, paced for the free tier.

    python scripts/explain_all.py [max_per_category]

Safe to re-run: findings that already have an explanation in the audit trail are skipped,
so an interrupted run resumes instead of starting over.
"""

from __future__ import annotations

import sys

from sqlalchemy.orm import sessionmaker

from app.db.session import engine
from app.models import ActorType
from app.services import audit
from app.services.agent import explain_discrepancies


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    session = sessionmaker(bind=engine)()
    try:
        run = explain_discrepancies(session, limit_per_category=limit)
        for item in run.explained:
            audit.record(
                session,
                action="agent.explained" if item.explanation else "agent.gated_only",
                entity_type="discrepancy",
                entity_id=item.discrepancy_id,
                actor_type=ActorType.AGENT if item.explanation else ActorType.SYSTEM,
                actor_id=run.model if item.explanation else None,
                payload={
                    "category_code": item.category_code,
                    "rule_id": item.rule_id,
                    "match_key": item.match_key,
                    "delta_minor": item.delta_minor,
                    "explanation": item.explanation,
                    "corrective_action": item.corrective_action,
                    "model_confidence": item.model_confidence,
                    "explanation_status": item.explanation_status,
                    "gate_decision": item.gate_decision,
                    "gate_reason": item.gate_reason,
                    "is_cold_start": item.is_cold_start,
                    "observations_needed": item.observations_needed,
                    "model": run.model,
                    "advisory_only": True,
                },
            )
        session.commit()

        print(f"skipped (already explained): {run.skipped_already_explained}")
        print(f"explained this run: {run.explained_count} / {len(run.explained)}")
        for t in run.telemetry:
            flag = "FAIL" if t.failed else " ok "
            print(
                f"  [{flag}] {t.category_code:22s} n={t.batch_size:3d} "
                f"{t.latency_ms:8.0f}ms tries={t.attempts} "
                f"in={t.prompt_tokens} out={t.output_tokens}"
                + (f"  {t.error[:70]}" if t.error else "")
            )
    finally:
        session.close()


if __name__ == "__main__":
    main()
