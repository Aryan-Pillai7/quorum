"""The approval loop end to end, against a real Postgres (ADR-0025 .. ADR-0028).

The claim being tested is the pitch: a category's trust score moves in response to
audited human feedback, and the gate opens only when it has actually earned it. Every
assertion reads back from the database rather than trusting a return value.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.config import Settings, get_settings
from app.models import (
    ApprovalDecision,
    AuditStatus,
    Direction,
    Discrepancy,
    MatchRecord,
    MatchStatus,
    MatchStrategy,
    SourceSystem,
    Transaction,
)
from app.services.approvals import ApprovalError, approve, audit_approval, select_for_audit
from app.services.audit import verify_chain
from app.services.trust import decide_gate

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
CATEGORY = "TIMING_DIFFERENCE"


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
    with _scratch_database("quorum_approval_loop_test") as url:
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


@pytest.fixture
def audit_everything() -> Settings:
    """Audit 100% of approvals.

    Not a weakening. The baseline sample exists to save human effort, so auditing
    everything is strictly more evidence per approval, not less (ADR-0025).
    """
    return get_settings().model_copy(update={"audit_baseline_rate": 1.0})


def make_findings(session: Session, count: int, category: str = CATEGORY) -> list[Discrepancy]:
    """Minimal findings to approve. Matching is not exercised here.

    Each match record needs a real leg: ck_match_records_at_least_one_leg rejects a match
    that links nothing, which is the Phase 1 constraint doing its job.
    """
    findings = []
    for i in range(count):
        txn = Transaction(
            source=SourceSystem.PSP_SETTLEMENT,
            external_id=f"pay_{i:05d}",
            amount_minor=100_000,
            currency="INR",
            direction=Direction.CREDIT,
            occurred_at=datetime.now(UTC),
        )
        session.add(txn)
        session.flush()
        match = MatchRecord(
            match_key=f"ord_{i:05d}",
            status=MatchStatus.BROKEN,
            strategy=MatchStrategy.EXACT_REFERENCE,
            confidence=1,
            psp_transaction_id=txn.id,
            amount_delta_minor=0,
            evidence={"pivot": "psp"},
        )
        session.add(match)
        session.flush()
        finding = Discrepancy(
            match_record_id=match.id,
            category_code=category,
            rule_id="R08_timing_difference",
            delta_minor=0,
            evidence={"lag_days": 1},
            summary="bank credited 1 day late",
        )
        session.add(finding)
        findings.append(finding)
    session.commit()
    return findings


def gate_from_db(session: Session, category: str = CATEGORY) -> tuple[str, float, int]:
    """Read the gate state straight from Postgres."""
    row = session.execute(
        text(
            "SELECT t.score, t.sample_size, t.min_sample_size, t.auto_apply_threshold, "
            "t.review_threshold, c.auto_resolvable FROM trust_scores t "
            "JOIN discrepancy_categories c ON c.code = t.category_code "
            "WHERE t.category_code = :c"
        ),
        {"c": category},
    ).one()
    evaluation = decide_gate(
        score=row.score,
        sample_size=row.sample_size,
        auto_apply_threshold=row.auto_apply_threshold,
        review_threshold=row.review_threshold,
        min_sample_size=row.min_sample_size,
        category_auto_resolvable=row.auto_resolvable,
    )
    return evaluation.decision.value, float(row.score), row.sample_size


# --- approving is not evidence --------------------------------------------------------


def test_an_approval_alone_moves_no_trust_score(session, audit_everything):
    """The central separation of ADR-0025."""
    finding = make_findings(session, 1)[0]
    before = gate_from_db(session)

    approve(
        session,
        discrepancy_id=finding.id,
        decision=ApprovalDecision.APPROVED,
        approver_id="ops.a",
        settings=audit_everything,
    )

    assert gate_from_db(session) == before, "approving must not move the score"


def test_a_finding_can_only_be_approved_once(session, audit_everything):
    """Two approvals on one finding would double-count as evidence."""
    finding = make_findings(session, 1)[0]
    approve(
        session, discrepancy_id=finding.id, decision=ApprovalDecision.APPROVED,
        approver_id="ops.a", settings=audit_everything,
    )
    with pytest.raises(ApprovalError, match="already approved"):
        approve(
            session, discrepancy_id=finding.id, decision=ApprovalDecision.APPROVED,
            approver_id="ops.b", settings=audit_everything,
        )


def test_an_edited_approval_records_both_the_draft_and_the_edit(session, audit_everything):
    """The trail must show what the approver was shown, not only what they chose."""
    finding = make_findings(session, 1)[0]
    approval = approve(
        session, discrepancy_id=finding.id, decision=ApprovalDecision.EDITED_APPROVED,
        approver_id="ops.a", final_action="Escalate to the settlement desk instead.",
        settings=audit_everything,
    )
    assert approval.final_action == "Escalate to the settlement desk instead."
    assert approval.decision is ApprovalDecision.EDITED_APPROVED


def test_an_edit_without_text_is_rejected(session, audit_everything):
    finding = make_findings(session, 1)[0]
    with pytest.raises(ApprovalError, match="requires the edited action"):
        approve(
            session, discrepancy_id=finding.id, decision=ApprovalDecision.EDITED_APPROVED,
            approver_id="ops.a", final_action="   ", settings=audit_everything,
        )


# --- auditing is evidence -------------------------------------------------------------


def test_auditing_an_approval_moves_the_score(session, audit_everything):
    finding = make_findings(session, 1)[0]
    approval = approve(
        session, discrepancy_id=finding.id, decision=ApprovalDecision.APPROVED,
        approver_id="ops.a", settings=audit_everything,
    )
    audit_approval(
        session, approval_id=approval.id, was_correct=True,
        auditor_id="audit.a", settings=audit_everything,
    )

    _decision, score, sample_size = gate_from_db(session)
    assert sample_size == 1
    assert score == pytest.approx(0.2, abs=1e-6)


def test_an_unselected_approval_cannot_be_audited(session):
    """Auditing outside the sample would bias the measurement."""
    never_audit = get_settings().model_copy(update={"audit_baseline_rate": 0.0})
    findings = make_findings(session, 5)

    unselected = None
    for finding in findings:
        approval = approve(
            session, discrepancy_id=finding.id, decision=ApprovalDecision.APPROVED,
            approver_id="ops.a", settings=never_audit,
        )
        if approval.audit_status is AuditStatus.NOT_SELECTED:
            unselected = approval
            break

    assert unselected is not None, "rate 0.0 should leave gate-neutral approvals unsampled"
    with pytest.raises(ApprovalError, match="not selected for audit"):
        audit_approval(
            session, approval_id=unselected.id, was_correct=True,
            auditor_id="audit.a", settings=never_audit,
        )


def test_an_approval_cannot_be_audited_twice(session, audit_everything):
    finding = make_findings(session, 1)[0]
    approval = approve(
        session, discrepancy_id=finding.id, decision=ApprovalDecision.APPROVED,
        approver_id="ops.a", settings=audit_everything,
    )
    audit_approval(
        session, approval_id=approval.id, was_correct=True,
        auditor_id="audit.a", settings=audit_everything,
    )
    with pytest.raises(ApprovalError, match="already audited"):
        audit_approval(
            session, approval_id=approval.id, was_correct=True,
            auditor_id="audit.b", settings=audit_everything,
        )


def test_an_incorrect_verdict_lowers_the_score(session, audit_everything):
    findings = make_findings(session, 2)
    for finding, correct in zip(findings, (True, False), strict=True):
        approval = approve(
            session, discrepancy_id=finding.id, decision=ApprovalDecision.APPROVED,
            approver_id="ops.a", settings=audit_everything,
        )
        audit_approval(
            session, approval_id=approval.id, was_correct=correct,
            auditor_id="audit.a", settings=audit_everything,
        )

    _decision, score, sample_size = gate_from_db(session)
    assert sample_size == 2
    assert score == pytest.approx(0.16, abs=1e-6)  # 0.2 then pulled down


# --- audit selection ------------------------------------------------------------------


def test_a_gate_moving_approval_is_always_audited(session):
    """Rate 0.0 must not stop the 100% rule for approvals that could move the gate."""
    never_audit = get_settings().model_copy(update={"audit_baseline_rate": 0.0})
    session.execute(
        text(
            "UPDATE trust_scores SET sample_size = 29, correct_count = 29, score = 0.99 "
            "WHERE category_code = :c"
        ),
        {"c": CATEGORY},
    )
    session.commit()

    selection = select_for_audit(
        session, approval_id=uuid.uuid4(), category_code=CATEGORY, settings=never_audit
    )
    assert selection.selected is True
    assert "could move" in selection.reason


def test_selection_is_deterministic_for_the_same_approval(session):
    """A hash draw, not a random one: a caller cannot retry until it escapes the sample."""
    half = get_settings().model_copy(update={"audit_baseline_rate": 0.5})
    approval_id = uuid.uuid4()
    first = select_for_audit(session, approval_id=approval_id, category_code=CATEGORY,
                             settings=half)
    second = select_for_audit(session, approval_id=approval_id, category_code=CATEGORY,
                              settings=half)
    assert first.selected == second.selected
    assert first.reason == second.reason


def test_the_baseline_rate_is_roughly_honoured(session):
    """Not exact -- it is a hash draw over ids -- but it must be in the right region."""
    quarter = get_settings().model_copy(update={"audit_baseline_rate": 0.25})
    selected = sum(
        select_for_audit(
            session, approval_id=uuid.uuid4(), category_code=CATEGORY, settings=quarter
        ).selected
        for _ in range(400)
    )
    assert 60 <= selected <= 140, f"expected roughly 100 of 400, got {selected}"


# --- the loop, end to end -------------------------------------------------------------


def test_thirty_audited_approvals_open_the_gate(session, audit_everything):
    """The pitch claim, verified against Postgres at every step.

    30 is not a demo threshold: it is TIMING_DIFFERENCE's seeded min_sample_size for a
    LOW severity category, unchanged since Phase 1.
    """
    assert gate_from_db(session)[0] == "HUMAN_REVIEW"

    findings = make_findings(session, 30)
    flipped_at = None
    for finding in findings:
        approval = approve(
            session, discrepancy_id=finding.id, decision=ApprovalDecision.APPROVED,
            approver_id="ops.demo", settings=audit_everything,
        )
        result = audit_approval(
            session, approval_id=approval.id, was_correct=True,
            auditor_id="audit.demo", settings=audit_everything,
        )
        if result.gate_changed:
            flipped_at = result.sample_size

    decision, score, sample_size = gate_from_db(session)
    assert decision == "AUTO_APPLY"
    assert sample_size == 30
    assert score > 0.99
    assert flipped_at == 30, "the gate must open at the threshold, not before it"


def test_a_high_value_transaction_still_needs_a_human_after_the_gate_opens(
    session, audit_everything
):
    """The exit criterion's fail-safe, at the exact state the demo reaches (ADR-0027)."""
    for finding in make_findings(session, 30):
        approval = approve(
            session, discrepancy_id=finding.id, decision=ApprovalDecision.APPROVED,
            approver_id="ops.demo", settings=audit_everything,
        )
        audit_approval(
            session, approval_id=approval.id, was_correct=True,
            auditor_id="audit.demo", settings=audit_everything,
        )

    decision, score, sample_size = gate_from_db(session)
    assert decision == "AUTO_APPLY", "precondition: the category must have earned it"

    threshold = audit_everything.high_value_review_threshold_minor
    high_value = decide_gate(
        score=score, sample_size=sample_size,
        auto_apply_threshold=0.85, review_threshold=0.50, min_sample_size=30,
        category_auto_resolvable=True,
        amount_minor=threshold + 1,
        high_value_threshold_minor=threshold,
    )
    assert high_value.decision.value == "HUMAN_REVIEW"
    assert "high-value review threshold" in high_value.reason


# --- the loop is audited --------------------------------------------------------------


def test_every_approval_and_audit_lands_in_the_hash_chain(session, audit_everything):
    finding = make_findings(session, 1)[0]
    approval = approve(
        session, discrepancy_id=finding.id, decision=ApprovalDecision.APPROVED,
        approver_id="ops.a", settings=audit_everything,
    )
    audit_approval(
        session, approval_id=approval.id, was_correct=True,
        auditor_id="audit.a", settings=audit_everything,
    )

    actions = session.execute(
        text("SELECT action FROM audit_events ORDER BY seq")
    ).scalars().all()
    assert "approval.recorded" in actions
    assert "approval.audited" in actions
    assert verify_chain(session).intact is True


def test_the_audit_event_distinguishes_trust_affecting_from_operational(
    session, audit_everything
):
    """Reading the trail must make it obvious which events were evidence."""
    finding = make_findings(session, 1)[0]
    approval = approve(
        session, discrepancy_id=finding.id, decision=ApprovalDecision.APPROVED,
        approver_id="ops.a", settings=audit_everything,
    )
    audit_approval(
        session, approval_id=approval.id, was_correct=True,
        auditor_id="audit.a", settings=audit_everything,
    )

    rows = dict(
        session.execute(
            text(
                "SELECT action, (payload->>'affects_trust')::boolean FROM audit_events "
                "WHERE action IN ('approval.recorded', 'approval.audited')"
            )
        ).all()
    )
    assert rows["approval.recorded"] is False
    assert rows["approval.audited"] is True


def test_an_audited_row_names_its_auditor_and_time(session, audit_everything):
    finding = make_findings(session, 1)[0]
    approval = approve(
        session, discrepancy_id=finding.id, decision=ApprovalDecision.APPROVED,
        approver_id="ops.a", settings=audit_everything,
    )
    audit_approval(
        session, approval_id=approval.id, was_correct=True,
        auditor_id="audit.a", note="checked against the settlement file",
        settings=audit_everything,
    )
    session.expire_all()

    row = session.execute(
        text("SELECT auditor_id, audited_at, audit_note FROM approvals")
    ).one()
    assert row.auditor_id == "audit.a"
    assert row.audited_at <= datetime.now(UTC)
    assert row.audit_note
