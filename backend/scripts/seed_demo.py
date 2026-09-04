"""Load the Phase 2 fixture into the configured database and reconcile it.

    python scripts/seed_demo.py

Idempotent by refusal: re-running against an already-seeded database reports that the
files are already ingested rather than duplicating them.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import sessionmaker

from app.db.session import engine
from app.models import SourceSystem
from app.services.ingestion import ingest_csv
from app.services.ingestion.runner import DuplicateBatchError
from app.services.matching import reconcile

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "recon_2026_03"
FILES = {
    SourceSystem.PSP_SETTLEMENT: "settlement.csv",
    SourceSystem.BANK_STATEMENT: "bank.csv",
    SourceSystem.INTERNAL_LEDGER: "ledger.csv",
}


def main() -> None:
    session = sessionmaker(bind=engine)()
    try:
        for source, filename in FILES.items():
            try:
                result = ingest_csv(session, source=source, path=FIXTURE_DIR / filename)
                print(
                    f"{source.value}: {result.ingested_rows} ingested, "
                    f"{result.quarantined_rows} quarantined of {result.total_rows}"
                )
            except DuplicateBatchError as exc:
                print(f"{source.value}: already ingested -- {exc.message.splitlines()[0]}")

        result = reconcile(session)
        print(
            f"reconciled: {result.match_records} match records "
            f"({result.full_matches} full, {result.broken_matches} broken, "
            f"{result.partial_matches} partial), {result.discrepancies} findings"
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
