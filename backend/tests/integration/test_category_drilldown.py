"""Category drill-down: reading is free, generating is batched (Phase 7).

Two properties carry this phase:

- opening a category and revealing an explanation calls **no** model, because the text is
  already in the audit trail;
- generating a missing one makes **one** call for the whole category, never one per
  finding.

Both are asserted by making a model call impossible or countable rather than by trusting
that the code path looks right.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.api.deps import get_db
from app.config import get_settings
from app.models import (
    Direction,
    Discrepancy,
    MatchRecord,
    MatchStatus,
    MatchStrategy,
    SourceSystem,
    Transaction,
)
from app.services.agent import explain as explain_module
from app.services.agent.client import ModelCall

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
CATEGORY = "MDR_FEE_VARIANCE"
TOKEN = "test-operator-token"


@contextmanager
def _scratch_database(name: str) -> Iterator[str]:
    base = make_url(get_settings().database_url)
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        connection = admin.connect()
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable: {str(exc).splitlines()[0]}")
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
def session() -> Iterator[Session]:
    with _scratch_database("quorum_drilldown_test") as url:
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        engine = create_engine(url)
        db = sessionmaker(bind=engine)()
        try:
            yield db
        finally:
            db.close()
            engine.dispose()


def _client(session, monkeypatch, *, gemini_key: str) -> Iterator[TestClient]:
    """An app bound to the scratch database, with the environment pinned explicitly.

    Both variables are set rather than inherited. Inheriting is what made these tests
    non-hermetic: they passed locally only because a developer .env supplied a real
    GEMINI_API_KEY, and failed in CI, which deliberately runs without one. Setting
    GEMINI_API_KEY to "" is not the same as deleting it -- a delete would let the .env
    value leak back in, while an empty string overrides it and normalizes to None.
    """
    monkeypatch.setenv("OPERATOR_TOKEN", TOKEN)
    monkeypatch.setenv("GEMINI_API_KEY", gemini_key)
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def client(session, monkeypatch) -> Iterator[TestClient]:
    """The default: the agent layer is available, so a stubbed client is actually used.

    The key is a dummy. Every test using this fixture replaces GeminiClient, so no real
    call is ever made -- the key exists only to get past the availability check that
    guards the batch loop.
    """
    yield from _client(session, monkeypatch, gemini_key="test-gemini-key")


@pytest.fixture
def client_without_agent(session, monkeypatch) -> Iterator[TestClient]:
    """No API key configured, which is how CI runs and how a demo box might."""
    yield from _client(session, monkeypatch, gemini_key="")


def make_findings(session: Session, count: int, prefix: str = "a") -> list[Discrepancy]:
    # The prefix keeps external ids unique across calls: transactions are unique on
    # (source, external_id), which a second batch would otherwise collide with.
    findings = []
    for i in range(count):
        txn = Transaction(
            source=SourceSystem.PSP_SETTLEMENT,
            external_id=f"pay_{prefix}_{i:05d}",
            amount_minor=498_020,
            currency="INR",
            direction=Direction.CREDIT,
            occurred_at=datetime.now(UTC),
        )
        session.add(txn)
        session.flush()
        match = MatchRecord(
            match_key=f"ord_{prefix}_{i:05d}",
            status=MatchStatus.BROKEN,
            strategy=MatchStrategy.EXACT_REFERENCE,
            confidence=1,
            psp_transaction_id=txn.id,
            amount_delta_minor=-5_876,
            evidence={"pivot": "psp"},
        )
        session.add(match)
        session.flush()
        finding = Discrepancy(
            match_record_id=match.id,
            category_code=CATEGORY,
            rule_id="R06_mdr_fee_variance",
            delta_minor=-5_876,
            evidence={
                "compared": "psp.fee_minor vs fee implied by the ledger rate",
                "psp_fee_minor": 17_629,
                "expected_fee_minor": 11_753,
                "variance_minor": 5_876,
                "tolerance_minor": 200,
            },
            summary="processor deducted 17629 where the ledger rate implies 11753",
        )
        session.add(finding)
        findings.append(finding)
    session.commit()
    return findings


class _ExplodingClient:
    """A model client that fails the test if anything tries to use it."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("the read path must not construct a model client")


class _CountingClient:
    """Returns a canned explanation for every requested id, and counts calls."""

    calls: list[int] = []

    def __init__(self, *args, **kwargs):
        pass

    def generate_json(self, *, system_instruction, prompt, schema):
        ids = [
            line.split('"')[3]
            for line in prompt.splitlines()
            if '"discrepancy_id"' in line
        ]
        _CountingClient.calls.append(len(ids))
        payload = {
            "explanations": [
                {
                    "discrepancy_id": did,
                    "explanation": "The processor deducted more fee than the rate implies.",
                    "corrective_action": "Raise a fee query with the processor.",
                    "model_confidence": "high",
                }
                for did in ids
            ]
        }
        return ModelCall(
            text=json.dumps(payload),
            model="stub-model",
            latency_ms=1.0,
            prompt_tokens=100,
            output_tokens=50,
            attempts=1,
        )


# --- the read path is free ------------------------------------------------------------


def test_the_drilldown_returns_deterministic_evidence(client, session):
    """The exit criterion: real field comparisons, per finding."""
    make_findings(session, 3)
    body = client.get(f"/v1/categories/{CATEGORY}/findings").json()

    assert body["counts"]["total_findings"] == 3
    finding = body["findings"][0]
    assert finding["compared"] == "psp.fee_minor vs fee implied by the ledger rate"

    values = {r["key"]: r["value"] for r in finding["evidence_rows"]}
    assert values["psp_fee_minor"] == 17_629
    assert values["expected_fee_minor"] == 11_753
    assert values["variance_minor"] == 5_876
    assert any(r["is_money"] for r in finding["evidence_rows"])


def test_opening_a_category_never_constructs_a_model_client(client, session, monkeypatch):
    """The reveal path must be free. Asserted by making a model call impossible."""
    make_findings(session, 3)
    monkeypatch.setattr(explain_module, "GeminiClient", _ExplodingClient)

    response = client.get(f"/v1/categories/{CATEGORY}/findings")

    assert response.status_code == 200
    assert response.json()["counts"]["total_findings"] == 3


def test_unexplained_findings_are_reported_as_such(client, session):
    make_findings(session, 4)
    body = client.get(f"/v1/categories/{CATEGORY}/findings").json()

    assert body["counts"]["explained"] == 0
    assert body["counts"]["unexplained"] == 4
    assert all(f["has_explanation"] is False for f in body["findings"])
    assert all(f["explanation"] is None for f in body["findings"])


def test_an_unknown_category_is_a_404(client):
    assert client.get("/v1/categories/NOT_A_CATEGORY/findings").status_code == 404


# --- generation is batched and guarded ------------------------------------------------


def test_generate_requires_the_operator_token(client, session):
    make_findings(session, 2)
    assert client.post(f"/v1/categories/{CATEGORY}/explain").status_code == 401


def test_generate_makes_one_call_for_the_whole_category(client, session, monkeypatch):
    """The quota constraint, asserted by counting: one call, every finding in it."""
    make_findings(session, 12)
    _CountingClient.calls = []
    monkeypatch.setattr(explain_module, "GeminiClient", _CountingClient)

    body = client.post(
        f"/v1/categories/{CATEGORY}/explain",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()

    assert len(_CountingClient.calls) == 1, "one API call, not one per finding"
    assert _CountingClient.calls[0] == 12, "the single call covered every finding"
    assert body["api_calls"] == 1
    assert body["explained"] == 12


def test_generated_explanations_are_written_to_postgres(client, session, monkeypatch):
    """Verified against the database, not the response body."""
    make_findings(session, 5)
    _CountingClient.calls = []
    monkeypatch.setattr(explain_module, "GeminiClient", _CountingClient)

    client.post(
        f"/v1/categories/{CATEGORY}/explain",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    stored = session.execute(
        text(
            "SELECT COUNT(*) FROM audit_events WHERE action = 'agent.explained' "
            "AND payload->>'category_code' = :c"
        ),
        {"c": CATEGORY},
    ).scalar_one()
    assert stored == 5

    body = client.get(f"/v1/categories/{CATEGORY}/findings").json()
    assert body["counts"]["explained"] == 5
    assert all(f["explanation"] for f in body["findings"])


def test_regenerating_skips_what_is_already_explained(client, session, monkeypatch):
    """Re-pressing the button must cost no quota."""
    make_findings(session, 6)
    _CountingClient.calls = []
    monkeypatch.setattr(explain_module, "GeminiClient", _CountingClient)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    client.post(f"/v1/categories/{CATEGORY}/explain", headers=headers)
    assert len(_CountingClient.calls) == 1

    second = client.post(f"/v1/categories/{CATEGORY}/explain", headers=headers).json()

    assert len(_CountingClient.calls) == 1, "nothing left to explain, so no second call"
    assert second["already_explained_skipped"] == 6
    assert second["api_calls"] == 0


def test_generation_only_touches_the_requested_category(client, session, monkeypatch):
    """A category filter that leaked would silently spend quota on everything."""
    make_findings(session, 3, prefix="req")
    other = make_findings(session, 2, prefix="other")
    for finding in other:
        finding.category_code = "ROUNDING_DIFFERENCE"
    session.commit()

    _CountingClient.calls = []
    monkeypatch.setattr(explain_module, "GeminiClient", _CountingClient)
    client.post(
        f"/v1/categories/{CATEGORY}/explain",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert _CountingClient.calls == [3], "only the requested category was batched"
    untouched = client.get("/v1/categories/ROUNDING_DIFFERENCE/findings").json()
    assert untouched["counts"]["explained"] == 0


def test_generation_without_an_api_key_says_so_rather_than_failing(
    client_without_agent, session
):
    """CI runs with no key, and so might a demo machine.

    No longer skips when a key happens to be present in the environment: the fixture
    pins the absence, so this always runs and always means something.
    """
    make_findings(session, 2)
    body = client_without_agent.post(
        f"/v1/categories/{CATEGORY}/explain",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()

    assert body["agent_available"] is False
    assert body["explained"] == 0
    assert "GEMINI_API_KEY is not set" in body["agent_status"]
    # Auth still passed and the endpoint still answered: an absent key disables the agent
    # layer, it does not reject the request.
    assert body["category_code"] == CATEGORY


def test_generation_lands_in_the_hash_chain(client, session, monkeypatch):
    make_findings(session, 3)
    monkeypatch.setattr(explain_module, "GeminiClient", _CountingClient)
    client.post(
        f"/v1/categories/{CATEGORY}/explain",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    from app.services.audit import verify_chain

    assert verify_chain(session).intact is True
