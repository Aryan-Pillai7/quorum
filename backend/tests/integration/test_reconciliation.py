"""End-to-end: ingest the fixture CSVs, reconcile, and check against the manifest.

    docker compose up -d db
    cd backend && pytest -m integration

The manifest (`tests/fixtures/recon_2026_03/expected.json`) is written by the generator
from what it planted, so these assertions compare the engine's output against what the
data actually contains -- not against what a previous run happened to produce. A rule
regression changes the engine's answer and the test fails; it cannot quietly re-baseline.

Numbers derived from this dataset are synthetic (ADR-0015). They show the engine
implements these rules; they are not evidence about real settlement files.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.config import get_settings
from app.models import SourceSystem
from app.services.ingestion import ingest_csv
from app.services.ingestion.runner import DuplicateBatchError
from app.services.matching import reconcile

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = BACKEND_ROOT / "tests" / "fixtures" / "recon_2026_03"
MANIFEST = json.loads((FIXTURE_DIR / "expected.json").read_text(encoding="utf-8"))

CSV_FOR_SOURCE = {
    SourceSystem.PSP_SETTLEMENT: "settlement.csv",
    SourceSystem.BANK_STATEMENT: "bank.csv",
    SourceSystem.INTERNAL_LEDGER: "ledger.csv",
}


@contextmanager
def _scratch_database(name: str) -> Iterator[str]:
    base = make_url(get_settings().database_url)
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        connection = admin.connect()
    except OperationalError as exc:
        pytest.skip(
            f"Postgres not reachable: {str(exc).splitlines()[0]}. "
            f"Start it with `docker compose up -d db`."
        )
    with connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as cleanup:
            cleanup.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def reconciled() -> Iterator[tuple[Session, dict]]:
    """Ingest all three fixture files into a scratch database and reconcile once."""
    with _scratch_database("quorum_reconciliation_test") as url:
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")

        engine = create_engine(url)
        session = sessionmaker(bind=engine)()
        try:
            batches = {
                source: ingest_csv(session, source=source, path=FIXTURE_DIR / filename)
                for source, filename in CSV_FOR_SOURCE.items()
            }
            result = reconcile(session)
            yield session, {"batches": batches, "result": result}
        finally:
            session.close()
            engine.dispose()


# --- ingestion ------------------------------------------------------------------------


def test_every_csv_row_is_either_ingested_or_quarantined(reconciled):
    """The denominator must be knowable: nothing is silently dropped."""
    _, ctx = reconciled
    for source, batch in ctx["batches"].items():
        assert batch.ingested_rows + batch.quarantined_rows == batch.total_rows, source


def test_total_rows_read_match_the_csv_files(reconciled):
    _, ctx = reconciled
    counts = MANIFEST["row_counts"]
    assert ctx["batches"][SourceSystem.PSP_SETTLEMENT].total_rows == (
        counts["settlement_csv_rows"]
    )
    assert ctx["batches"][SourceSystem.BANK_STATEMENT].total_rows == counts["bank_csv_rows"]
    assert ctx["batches"][SourceSystem.INTERNAL_LEDGER].total_rows == counts["ledger_csv_rows"]


def test_planted_malformed_rows_are_quarantined_with_the_right_reasons(reconciled):
    session, ctx = reconciled
    expected = MANIFEST["expected_quarantined"]

    total = sum(b.quarantined_rows for b in ctx["batches"].values())
    assert total == expected["total"]

    by_reason = dict(
        session.execute(
            text("SELECT reason, COUNT(*) FROM quarantined_rows GROUP BY reason")
        ).all()
    )
    assert by_reason == expected["by_reason"]


def test_quarantined_rows_keep_their_original_content_and_a_detail(reconciled):
    """A quarantined row must be actionable: the operator needs the line and the reason."""
    session, _ = reconciled
    rows = session.execute(
        text("SELECT row_number, detail, raw FROM quarantined_rows ORDER BY row_number")
    ).all()
    assert rows
    for row_number, detail, raw in rows:
        assert row_number >= 2, "row 1 is the header"
        assert detail.strip()
        assert raw, "the original row must be preserved"


def test_reingesting_the_same_file_is_refused(reconciled):
    session, _ = reconciled
    with pytest.raises(DuplicateBatchError, match="already been ingested"):
        ingest_csv(
            session, source=SourceSystem.PSP_SETTLEMENT, path=FIXTURE_DIR / "settlement.csv"
        )


# --- matching -------------------------------------------------------------------------


def test_every_ingested_transaction_belongs_to_a_match_record(reconciled):
    """Nothing may vanish between ingestion and matching, or the rate has no denominator."""
    session, _ = reconciled
    orphans = session.execute(
        text(
            "SELECT COUNT(*) FROM transactions t WHERE NOT EXISTS ("
            "  SELECT 1 FROM match_records m WHERE m.psp_transaction_id = t.id"
            "   OR m.bank_transaction_id = t.id OR m.ledger_transaction_id = t.id)"
        )
    ).scalar_one()
    assert orphans == 0


def test_clean_cases_produce_full_matches_with_no_findings(reconciled):
    _, ctx = reconciled
    assert ctx["result"].full_matches == MANIFEST["expected_cases"]["clean"]


def test_each_discrepancy_category_matches_what_was_planted(reconciled):
    """Counted per order, because a split or duplicate plants one case across several rows."""
    session, _ = reconciled
    rows = session.execute(
        text(
            "SELECT d.category_code, COUNT(DISTINCT m.match_key) "
            "FROM discrepancies d JOIN match_records m ON m.id = d.match_record_id "
            "GROUP BY d.category_code"
        )
    ).all()
    observed = {code: count for code, count in rows}
    assert observed == MANIFEST["expected_cases"]["by_category"]


def test_nothing_falls_through_to_novel(reconciled):
    """Every planted shape has a rule. A __novel__ finding here means one stopped working."""
    session, _ = reconciled
    novel = session.execute(
        text("SELECT COUNT(*) FROM discrepancies WHERE category_code = '__novel__'")
    ).scalar_one()
    assert novel == 0


def test_the_compound_case_carries_both_labels(reconciled):
    """The reason ADR-0012 exists: late AND overcharged, both recorded."""
    session, _ = reconciled
    multi = session.execute(
        text(
            "SELECT COUNT(*) FROM ("
            "  SELECT m.match_key FROM discrepancies d "
            "  JOIN match_records m ON m.id = d.match_record_id "
            "  GROUP BY m.match_key HAVING COUNT(DISTINCT d.category_code) > 1) AS t"
        )
    ).scalar_one()
    assert multi == 8, "expected the 8 planted compound cases to carry two categories each"


def test_no_transaction_belongs_to_more_than_one_match(reconciled):
    """Enforced by unique indexes; asserted here because double-counting inflates a rate."""
    session, _ = reconciled
    for column in ("psp_transaction_id", "bank_transaction_id", "ledger_transaction_id"):
        duplicated = session.execute(
            text(
                f"SELECT COUNT(*) FROM (SELECT {column} FROM match_records "
                f"WHERE {column} IS NOT NULL GROUP BY {column} HAVING COUNT(*) > 1) AS t"
            )
        ).scalar_one()
        assert duplicated == 0, column


# --- traceability ---------------------------------------------------------------------


def test_every_finding_records_the_comparison_that_produced_it(reconciled):
    """The Phase 2 traceability requirement, asserted rather than asserted-to."""
    session, _ = reconciled
    rows = session.execute(
        text("SELECT rule_id, summary, evidence FROM discrepancies")
    ).all()
    assert rows
    for rule_id, summary, evidence in rows:
        assert rule_id.startswith("R"), rule_id
        assert summary.strip()
        assert evidence, f"{rule_id} recorded no evidence"


def test_every_match_record_records_how_its_legs_were_joined(reconciled):
    session, _ = reconciled
    rows = session.execute(text("SELECT evidence FROM match_records")).all()
    assert rows
    for (evidence,) in rows:
        assert "pivot" in evidence
        joins = [key for key in evidence if key.endswith("_join")]
        assert joins, f"no leg join recorded in {evidence}"


def test_findings_are_attributable_to_a_named_rule(reconciled):
    """Rule ids must be stable and quotable: 'R06 fired on this row' is actionable."""
    session, _ = reconciled
    by_category: dict[str, set[str]] = defaultdict(set)
    for category, rule_id in session.execute(
        text("SELECT category_code, rule_id FROM discrepancies")
    ).all():
        by_category[category].add(rule_id)
    for category, rule_ids in by_category.items():
        assert len(rule_ids) == 1, f"{category} produced by multiple rules: {rule_ids}"


def test_re_running_the_engine_does_not_duplicate_findings(reconciled):
    """Unique on (match_record_id, rule_id): a second run converges, it does not accumulate."""
    session, ctx = reconciled
    before = session.execute(text("SELECT COUNT(*) FROM discrepancies")).scalar_one()
    reconcile(session)
    after = session.execute(text("SELECT COUNT(*) FROM discrepancies")).scalar_one()
    assert after == before
    assert before == ctx["result"].discrepancies
