"""Walk the audit hash chain and report whether it is intact.

    python scripts/verify_audit_chain.py

Exits 1 when the chain is broken, so it can gate a deploy or a nightly job.
"""

from __future__ import annotations

import json
import sys

from sqlalchemy.orm import sessionmaker

from app.db.session import engine
from app.services.audit import verify_chain


def main() -> int:
    session = sessionmaker(bind=engine)()
    try:
        report = verify_chain(session)
    finally:
        session.close()

    print(json.dumps(report.to_dict(), indent=2))
    if not report.intact:
        print(
            f"\nCHAIN BROKEN: {len(report.problems)} problem(s), first at seq "
            f"{report.problems[0].seq}",
            file=sys.stderr,
        )
        return 1
    print(
        f"\nchain intact: {report.verified_events} of {report.chained_events} chained "
        f"events verified, {report.unchained_legacy_events} predate the chain"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
