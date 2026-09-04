"""Generate agent explanations for every unexplained discrepancy, paced for the free tier.

    python scripts/explain_all.py [max_per_category]

Safe to re-run: findings that already have an explanation in the audit trail are skipped,
so an interrupted run resumes instead of starting over.
"""

from __future__ import annotations

import sys

from sqlalchemy.orm import sessionmaker

from app.db.session import engine
from app.services.agent import explain_discrepancies
from app.services.agent.explain import persist_explanation_run


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    session = sessionmaker(bind=engine)()
    try:
        run = explain_discrepancies(session, limit_per_category=limit)
        persist_explanation_run(session, run)
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
