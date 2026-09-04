"""The audit hash chain, against a real Postgres (ADR-0022).

The tests that matter are the ones that tamper. A chain nobody has watched fail is a
chain nobody knows works, so each form of tampering the design claims to detect gets a
test that performs it and asserts it is caught:

- editing a stored row
- deleting a row from the middle
- re-ordering by rewriting a link

Also covered: rows written before the chain existed are reported as uncovered, not
quietly counted as verified.
"""

from __future__ import annotations

import json
import uuid
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
from app.models import ActorType
from app.services.audit import compute_entry_hash, record, verify_chain

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]


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


@pytest.fixture
def audit_session() -> Iterator[Session]:
    """A migrated, empty database per test, so tampering in one cannot leak into another."""
    with _scratch_database("quorum_audit_chain_test") as url:
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")

        engine = create_engine(url)
        session = sessionmaker(bind=engine)()
        try:
            yield session
        finally:
            session.close()
            engine.dispose()


def append(session: Session, n: int = 5) -> None:
    for i in range(n):
        record(
            session,
            action="test.event",
            entity_type="thing",
            entity_id=f"thing_{i}",
            actor_type=ActorType.SYSTEM,
            payload={"index": i, "note": f"event {i}"},
        )
    session.commit()


# --- the chain forms ------------------------------------------------------------------


def test_appending_events_builds_a_linked_chain(audit_session):
    append(audit_session, 5)
    rows = audit_session.execute(
        text("SELECT seq, prev_hash, entry_hash FROM audit_events ORDER BY seq")
    ).all()

    assert len(rows) == 5
    assert rows[0][1] is None, "the first entry has nothing to link to"
    for previous, current in zip(rows, rows[1:], strict=False):
        assert current[1] == previous[2], "each entry must link to its predecessor"
    assert len({r[2] for r in rows}) == 5, "every entry hash is distinct"


def test_a_freshly_written_chain_verifies(audit_session):
    append(audit_session, 10)
    report = verify_chain(audit_session)
    assert report.intact is True
    assert report.chained_events == 10
    assert report.verified_events == 10
    assert report.unchained_legacy_events == 0


def test_events_written_in_one_transaction_chain_to_each_other(audit_session):
    """A batch must not have every row linking to the last committed entry."""
    for i in range(4):
        record(audit_session, action="batch", entity_type="t", entity_id=str(i))
    audit_session.commit()

    prev_hashes = audit_session.execute(
        text("SELECT prev_hash FROM audit_events ORDER BY seq")
    ).scalars().all()
    assert prev_hashes[0] is None
    assert len({p for p in prev_hashes if p}) == 3, "each row links to a different predecessor"
    assert verify_chain(audit_session).intact is True


# --- tampering is detected ------------------------------------------------------------


def test_editing_a_stored_row_is_detected(audit_session):
    """The headline claim: a retroactive edit does not survive verification."""
    append(audit_session, 5)
    assert verify_chain(audit_session).intact is True

    audit_session.execute(
        text(
            "UPDATE audit_events SET payload = jsonb_set(payload, '{note}', '\"edited\"') "
            "WHERE seq = 3"
        )
    )
    audit_session.commit()
    audit_session.expire_all()

    report = verify_chain(audit_session)
    assert report.intact is False
    assert any(p.kind == "content_altered" and p.seq == 3 for p in report.problems)


def test_editing_the_actor_is_detected(audit_session):
    """Rewriting who did something is exactly the tampering an audit trail exists for."""
    append(audit_session, 4)
    audit_session.execute(
        text("UPDATE audit_events SET actor_type = 'USER' WHERE seq = 2")
    )
    audit_session.commit()
    audit_session.expire_all()

    report = verify_chain(audit_session)
    assert report.intact is False
    assert any(p.kind == "content_altered" for p in report.problems)


def test_deleting_a_row_from_the_middle_is_detected(audit_session):
    """Each surviving row stays internally valid, so only the linkage reveals the gap."""
    append(audit_session, 6)
    audit_session.execute(text("DELETE FROM audit_events WHERE seq = 3"))
    audit_session.commit()
    audit_session.expire_all()

    report = verify_chain(audit_session)
    assert report.intact is False
    assert any(p.kind == "broken_link" for p in report.problems)


def test_rewriting_a_link_is_detected(audit_session):
    append(audit_session, 5)
    audit_session.execute(
        text("UPDATE audit_events SET prev_hash = repeat('a', 64) WHERE seq = 4")
    )
    audit_session.commit()
    audit_session.expire_all()

    report = verify_chain(audit_session)
    assert report.intact is False
    # Both checks fire: the link no longer matches, and the row's own hash no longer
    # matches its content, because prev_hash is part of what is hashed.
    assert {p.kind for p in report.problems} == {"broken_link", "content_altered"}


def test_the_report_names_the_first_broken_entry(audit_session):
    """A verification that only says "broken" sends someone hunting through the table."""
    append(audit_session, 8)
    audit_session.execute(
        text("UPDATE audit_events SET action = 'tampered' WHERE seq = 6")
    )
    audit_session.commit()
    audit_session.expire_all()

    report = verify_chain(audit_session)
    assert report.problems[0].seq == 6
    assert report.problems[0].event_id
    assert "edited after it was written" in report.problems[0].detail


# --- honesty about what is not covered ------------------------------------------------


def test_rows_predating_the_chain_are_reported_not_counted_as_verified(audit_session):
    """Back-filling hashes over them would prove nothing, so they are named instead."""
    audit_session.execute(
        text(
            "INSERT INTO audit_events (id, occurred_at, actor_type, action, entity_type, "
            "entity_id, payload) VALUES (:id, now(), 'SYSTEM', 'legacy', 't', 'x', '{}')"
        ),
        {"id": uuid.uuid4()},
    )
    audit_session.commit()
    append(audit_session, 3)

    report = verify_chain(audit_session)
    assert report.intact is True
    assert report.total_events == 4
    assert report.unchained_legacy_events == 1
    assert report.chained_events == 3
    assert report.verified_events == 3


def test_the_report_states_what_the_chain_does_not_protect_against(audit_session):
    """The caveat travels with the verdict, so "intact" is never read as more than it is."""
    append(audit_session, 2)
    caveat = verify_chain(audit_session).to_dict()["caveat"]
    assert "Does not prevent" in caveat
    assert "recompute the chain forward" in caveat


def test_a_recomputed_chain_passes_verification(audit_session):
    """Proves the documented weakness is real rather than theoretical.

    An attacker with write access can edit a row and rebuild every hash after it. This
    test performs exactly that and asserts verification is fooled -- which is why the
    README says "detects silent alteration", not "prevents tampering".
    """
    append(audit_session, 4)
    rows = audit_session.execute(
        text(
            "SELECT id, occurred_at, actor_type, actor_id, action, entity_type, "
            "entity_id, payload, prev_hash, seq FROM audit_events ORDER BY seq"
        )
    ).all()

    prev = None
    for i, row in enumerate(rows):
        payload = {"index": 99, "note": "rewritten"} if i == 1 else row.payload
        new_hash = compute_entry_hash(
            event_id=row.id,
            occurred_at=row.occurred_at,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            payload=payload,
            prev_hash=prev,
        )
        audit_session.execute(
            text(
                "UPDATE audit_events SET payload = :p, prev_hash = :prev, entry_hash = :h "
                "WHERE seq = :seq"
            ),
            {"p": json.dumps(payload), "prev": prev, "h": new_hash, "seq": row.seq},
        )
        prev = new_hash
    audit_session.commit()
    audit_session.expire_all()

    assert verify_chain(audit_session).intact is True, (
        "a fully recomputed chain passes -- this is the documented limitation, not a bug"
    )
