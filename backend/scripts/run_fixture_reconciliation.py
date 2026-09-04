"""Ingest the fixture dataset into a scratch database and reconcile it.

Prints the phase-report numbers with their denominators. Creates and drops its own
database, so it never touches development data.

    python scripts/run_fixture_reconciliation.py
"""

from __future__ import annotations

import json
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.config import get_settings
from app.models import SourceSystem
from app.services.ingestion import ingest_csv
from app.services.matching import reconcile

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = BACKEND_ROOT / "tests" / "fixtures" / "recon_2026_03"
SCRATCH_DB = "quorum_fixture_run"


def main() -> None:
    base = make_url(get_settings().database_url)
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{SCRATCH_DB}"'))

    url = base.set(database=SCRATCH_DB).render_as_string(hide_password=False)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    engine = create_engine(url)
    Session = sessionmaker(bind=engine)

    try:
        with Session() as session:
            batches = {
                SourceSystem.PSP_SETTLEMENT: ingest_csv(
                    session, source=SourceSystem.PSP_SETTLEMENT,
                    path=FIXTURE_DIR / "settlement.csv",
                ),
                SourceSystem.BANK_STATEMENT: ingest_csv(
                    session, source=SourceSystem.BANK_STATEMENT, path=FIXTURE_DIR / "bank.csv"
                ),
                SourceSystem.INTERNAL_LEDGER: ingest_csv(
                    session, source=SourceSystem.INTERNAL_LEDGER,
                    path=FIXTURE_DIR / "ledger.csv",
                ),
            }
            result = reconcile(session)

            with engine.connect() as conn:
                groups = conn.execute(
                    text(
                        "SELECT method, status, COUNT(*), SUM(member_count) "
                        "FROM settlement_groups GROUP BY method, status ORDER BY method, status"
                    )
                ).all()
                grouped_rows = conn.execute(
                    text("SELECT COUNT(*) FROM transactions WHERE settlement_group_id IS NOT NULL")
                ).scalar_one()
                quarantine_by_reason = dict(
                    conn.execute(
                        text(
                            "SELECT reason, COUNT(*) FROM quarantined_rows "
                            "GROUP BY reason ORDER BY reason"
                        )
                    ).all()
                )

        report = {
            "ingestion": {
                source.value: {
                    "total_rows": batch.total_rows,
                    "ingested": batch.ingested_rows,
                    "quarantined": batch.quarantined_rows,
                }
                for source, batch in batches.items()
            },
            "quarantine_by_reason": quarantine_by_reason,
            "aggregation": {
                "groups": [
                    {
                        "method": m,
                        "status": s,
                        "count": c,
                        "member_rows": int(members or 0),
                    }
                    for m, s, c, members in groups
                ],
                "settlement_rows_in_a_group": grouped_rows,
            },
            "matching": {
                "psp_rows": result.psp_rows,
                "bank_rows": result.bank_rows,
                "ledger_rows": result.ledger_rows,
                "match_records": result.match_records,
                "full_matches": result.full_matches,
                "partial_matches": result.partial_matches,
                "broken_matches": result.broken_matches,
                "discrepancy_findings": result.discrepancies,
                "findings_by_category": dict(sorted(result.findings_by_category.items())),
            },
        }
        print(json.dumps(report, indent=2))
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
        admin.dispose()


if __name__ == "__main__":
    main()
