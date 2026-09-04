"""Three-way matching engine.

SEALED (ADR-0004): no agent layer, no network client. Results are reproducible from the
stored transactions alone.

There is no reference field shared by all three sources, so the processor settlement
report is the pivot (ADR-0013):

    ledger --order_ref--> psp settlement --counterparty_ref (UTR)--> bank

Each leg join is a named comparison of two named fields, recorded on the match record's
`evidence`. Combined with the per-finding evidence on each discrepancy row, that is what
makes any outcome traceable to the exact comparison that produced it.

Phase 2 assumes 1:1 settlement -- one payment, one bank credit (ADR-0014). Aggregated
payouts, where forty payments become one bank line, need a different algorithm and are
out of scope.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Discrepancy,
    DiscrepancyCategory,
    GroupMethod,
    GroupStatus,
    MatchRecord,
    MatchStatus,
    MatchStrategy,
    SettlementGroup,
    SourceSystem,
    Transaction,
    TransactionStatus,
)
from app.services.matching.aggregation import (
    AggregationResult,
    BankCredit,
    CandidateRow,
    group_by_shared_reference,
    group_by_subset_sum,
)
from app.services.matching.rules import (
    DEFAULT_TIMING_WINDOW_DAYS,
    Finding,
    LegView,
    MatchContext,
    classify,
)

logger = logging.getLogger(__name__)

# How far apart two rows may be and still be paired by the amount+date fallback. Wider
# than the timing window on purpose: this pass runs only when no reference is available,
# so it needs more room, and it is scored lower for exactly that reason.
FALLBACK_WINDOW_DAYS = 3

CONFIDENCE_EXACT_REFERENCE = Decimal("1.0000")
# An aggregated payout matched by its own settlement reference is as certain as a 1:1
# reference match. One found by search is not: it rests on amounts adding up, which is
# weaker evidence than a shared identifier, and it is scored accordingly.
CONFIDENCE_AGGREGATE_SHARED_REFERENCE = Decimal("0.9500")
CONFIDENCE_AGGREGATE_SUBSET_SUM = Decimal("0.7000")
CONFIDENCE_REFERENCE_TOLERANCE = Decimal("0.9000")
CONFIDENCE_AMOUNT_DATE_WINDOW = Decimal("0.6000")
CONFIDENCE_SINGLE_LEG = Decimal("0.3000")


@dataclass(frozen=True)
class ReconciliationResult:
    """What one reconciliation run produced. Every count is stated, so a rate derived
    from these numbers always has a visible denominator."""

    psp_rows: int
    bank_rows: int
    ledger_rows: int
    match_records: int
    full_matches: int
    partial_matches: int
    broken_matches: int
    discrepancies: int
    findings_by_category: dict[str, int]

    @property
    def total_rows(self) -> int:
        return self.psp_rows + self.bank_rows + self.ledger_rows


def _to_leg(txn: Transaction) -> LegView:
    return LegView(
        external_id=txn.external_id,
        amount_minor=txn.amount_minor,
        occurred_at=txn.occurred_at,
        gross_amount_minor=txn.gross_amount_minor,
        fee_minor=txn.fee_minor,
        counterparty_ref=txn.counterparty_ref,
        order_ref=txn.order_ref,
    )


def _load_tolerances(session: Session) -> dict[str, int]:
    """Category tolerances come from the seeded taxonomy, so tuning one is a data change."""
    rows = session.execute(
        select(DiscrepancyCategory.code, DiscrepancyCategory.tolerance_minor)
    ).all()
    return {code: tolerance for code, tolerance in rows}



def _to_candidate(txn: Transaction) -> CandidateRow:
    return CandidateRow(
        transaction_id=str(txn.id),
        external_id=txn.external_id,
        amount_minor=txn.amount_minor,
        occurred_at=txn.occurred_at,
        currency=txn.currency,
        counterparty_ref=txn.counterparty_ref,
    )


def _to_bank_credit(txn: Transaction) -> BankCredit:
    return BankCredit(
        transaction_id=str(txn.id),
        external_id=txn.external_id,
        amount_minor=txn.amount_minor,
        occurred_at=txn.occurred_at,
        currency=txn.currency,
        counterparty_ref=txn.counterparty_ref,
    )


def _detect_settlement_groups(
    bank_rows: list[Transaction],
    psp_rows: list[Transaction],
    tolerance_minor: int,
) -> list[AggregationResult]:
    """Find aggregated payouts before 1:1 matching runs.

    Order matters. If N settlement rows share a payout reference with one bank credit,
    the ordinary 1:1 pass would match the first of them to the credit and then report an
    amount mismatch -- which is the Phase 2 bug this phase exists to fix. So aggregation
    is detected first, and the rows it claims are excluded from the 1:1 pass.

    Which pass applies is decided by a deterministic precondition on the reference, never
    by trying 1:1 first and reacting to the result:

    - a credit whose reference is carried by 2+ settlement rows -> shared-reference pass
    - a credit whose reference is carried by no settlement row  -> subset-sum search
    - a credit whose reference is carried by exactly one row    -> left to the 1:1 pass
    """
    psp_by_ref: dict[str, list[Transaction]] = defaultdict(list)
    for txn in psp_rows:
        if txn.counterparty_ref:
            psp_by_ref[txn.counterparty_ref].append(txn)

    # A settlement row whose reference matches some bank credit is matchable 1:1 by
    # reference, so it has no business in a subset-sum pool. Excluding those is what makes
    # the search viable at all: on the fixture it takes the candidate pool from 87-166
    # rows -- far past the cap, so the search refused to start -- down to 3-13. Date and
    # currency bounds alone are nowhere near selective enough on real volumes.
    bank_refs = {t.counterparty_ref for t in bank_rows if t.counterparty_ref}

    results: list[AggregationResult] = []
    claimed: set[str] = set()  # settlement rows already inside a resolved group

    for bank in bank_rows:
        ref = bank.counterparty_ref
        sharing = [t for t in psp_by_ref.get(ref or "", []) if str(t.id) not in claimed]

        if len(sharing) >= 2:
            result = group_by_shared_reference(
                _to_bank_credit(bank),
                [_to_candidate(t) for t in sharing],
                tolerance_minor=tolerance_minor,
            )
        elif not sharing and ref:
            # No settlement row carries this credit's reference, so a 1:1 reference match
            # is impossible and the search is the only remaining explanation.
            available = [
                t
                for t in psp_rows
                if str(t.id) not in claimed
                and (not t.counterparty_ref or t.counterparty_ref not in bank_refs)
            ]
            result = group_by_subset_sum(
                _to_bank_credit(bank),
                [_to_candidate(t) for t in available],
                tolerance_minor=tolerance_minor,
            )
        else:
            continue

        if result is None or result.status is GroupStatus.INCONCLUSIVE:
            # Most bank credits are ordinary 1:1 matches. Recording a group row for every
            # one that failed to aggregate would bury the real findings in noise.
            continue

        results.append(result)
        if result.status is GroupStatus.RESOLVED:
            claimed.update(m.transaction_id for m in result.members)

    return results



def _persist_settlement_groups(
    session: Session,
    results: list[AggregationResult],
    consumed: set[str],
) -> tuple[dict[str, SettlementGroup], list[tuple[MatchRecord, list[Finding]]]]:
    """Write the groups and their bank-leg match records.

    Each group gets its own match record holding the bank leg, which is what keeps the
    unique index on `match_records.bank_transaction_id` doing its job while N settlement
    rows point at the same credit (ADR-0019).

    An AMBIGUOUS group claims no members. The engine can see that several sets explain the
    credit and will not pick one: attributing money to the wrong payments silently is the
    specific failure this whole system exists to prevent. The bank credit is still recorded
    with every competing set in evidence, and the settlement rows stay available to the
    ordinary passes.
    """
    group_by_psp_id: dict[str, SettlementGroup] = {}
    records: list[tuple[MatchRecord, list[Finding]]] = []

    for result in results:
        group = SettlementGroup(
            bank_transaction_id=result.bank.transaction_id,
            method=result.method,
            status=result.status,
            currency=result.bank.currency,
            bank_amount_minor=result.bank.amount_minor,
            members_total_minor=result.members_total_minor,
            delta_minor=result.delta_minor,
            member_count=len(result.members),
            candidates_considered=result.candidates_considered,
            solution_count=result.solution_count,
            nodes_explored=result.nodes_explored,
            evidence=result.to_evidence(),
            notes=result.reason,
        )
        session.add(group)
        session.flush()  # assign group.id before anything references it

        findings: list[Finding] = []
        if result.status is GroupStatus.RESOLVED:
            for member in result.members:
                group_by_psp_id[member.transaction_id] = group
                txn = session.get(Transaction, member.transaction_id)
                txn.settlement_group_id = group.id
            status = MatchStatus.FULL if result.delta_minor == 0 else MatchStatus.BROKEN
            confidence = (
                CONFIDENCE_AGGREGATE_SHARED_REFERENCE
                if result.method is GroupMethod.SHARED_REFERENCE
                else CONFIDENCE_AGGREGATE_SUBSET_SUM
            )
        else:
            status = MatchStatus.BROKEN
            confidence = CONFIDENCE_SINGLE_LEG
            findings.append(
                Finding(
                    rule_id="R13_aggregation_ambiguous",
                    category_code="AGGREGATION_AMBIGUOUS",
                    delta_minor=result.bank.amount_minor,
                    summary=result.reason,
                    evidence=result.to_evidence(),
                    explains_delta=True,
                )
            )

        strategy = (
            MatchStrategy.AGGREGATE_SHARED_REFERENCE
            if result.method is GroupMethod.SHARED_REFERENCE
            else MatchStrategy.AGGREGATE_SUBSET_SUM
        )
        record = MatchRecord(
            match_key=result.bank.counterparty_ref or result.bank.external_id,
            status=status,
            strategy=strategy,
            confidence=confidence,
            bank_transaction_id=result.bank.transaction_id,
            settlement_group_id=group.id,
            amount_delta_minor=result.delta_minor,
            evidence={
                "pivot": "settlement_group",
                "settlement_group_id": str(group.id),
                "aggregation": result.to_evidence(),
                "psp_join": {
                    "field": (
                        "counterparty_ref"
                        if result.method is GroupMethod.SHARED_REFERENCE
                        else "amount_subset_sum"
                    ),
                    "value": result.bank.counterparty_ref,
                    "matched": result.status is GroupStatus.RESOLVED,
                },
            },
            notes=result.reason,
        )
        records.append((record, findings))
        consumed.add(result.bank.transaction_id)

    return group_by_psp_id, records


def reconcile(
    session: Session,
    *,
    timing_window_days: int = DEFAULT_TIMING_WINDOW_DAYS,
    fallback_window_days: int = FALLBACK_WINDOW_DAYS,
) -> ReconciliationResult:
    """Match every currently unmatched transaction and record what was found.

    Idempotent in the sense that matched transactions are excluded from subsequent runs;
    re-running after ingesting more data extends the result rather than rebuilding it.
    """
    tolerances = _load_tolerances(session)

    unmatched = (
        session.execute(
            select(Transaction)
            .where(Transaction.status == TransactionStatus.UNMATCHED)
            .order_by(Transaction.external_id)
        )
        .scalars()
        .all()
    )

    by_source: dict[SourceSystem, list[Transaction]] = defaultdict(list)
    for txn in unmatched:
        by_source[txn.source].append(txn)

    psp_rows = by_source[SourceSystem.PSP_SETTLEMENT]
    bank_rows = by_source[SourceSystem.BANK_STATEMENT]
    ledger_rows = by_source[SourceSystem.INTERNAL_LEDGER]

    ledger_by_order: dict[str, list[Transaction]] = defaultdict(list)
    for txn in ledger_rows:
        if txn.order_ref:
            ledger_by_order[txn.order_ref].append(txn)

    bank_by_utr: dict[str, list[Transaction]] = defaultdict(list)
    for txn in bank_rows:
        if txn.counterparty_ref:
            bank_by_utr[txn.counterparty_ref].append(txn)

    # Processor rows sharing an order reference are either a routing split or a duplicate
    # settlement. Grouped up front so each match knows about its siblings.
    psp_by_order: dict[str, list[Transaction]] = defaultdict(list)
    for txn in psp_rows:
        if txn.order_ref:
            psp_by_order[txn.order_ref].append(txn)

    # Transaction ids as STRINGS throughout. The aggregation dataclasses carry string
    # ids while the ORM carries UUID objects, and a set holding both silently fails
    # every membership test across the boundary -- which let the amount+date fallback
    # re-claim a bank credit an aggregation group already held.
    consumed: set[str] = set()
    records: list[tuple[MatchRecord, list[Finding]]] = []

    # --- Pass 0: aggregated payouts (ADR-0019) ------------------------------------------
    # Runs before 1:1 so a shared payout reference is not consumed one row at a time.
    rounding_tolerance = tolerances.get("ROUNDING_DIFFERENCE", 0)
    aggregation_results = _detect_settlement_groups(bank_rows, psp_rows, rounding_tolerance)
    group_by_psp_id, aggregation_records = _persist_settlement_groups(
        session, aggregation_results, consumed
    )
    records.extend(aggregation_records)

    # --- Pass 1: pivot on the processor settlement report ------------------------------
    for psp in psp_rows:
        siblings = [t for t in psp_by_order.get(psp.order_ref or "", []) if t.id != psp.id]
        sibling_sum = (
            sum(t.amount_minor for t in psp_by_order[psp.order_ref]) if siblings else None
        )

        # The ledger leg attaches to whichever sibling sorts first, because a transaction
        # may belong to at most one match (ADR-0005). The others still carry the split
        # finding, with the whole group named in their evidence.
        anchor = min(psp_by_order.get(psp.order_ref or "", [psp]), key=lambda t: t.external_id)
        ledger = None
        if psp.order_ref and psp.id == anchor.id:
            candidates = [
                t for t in ledger_by_order.get(psp.order_ref, []) if str(t.id) not in consumed
            ]
            ledger = candidates[0] if candidates else None

        group = group_by_psp_id.get(str(psp.id))

        bank = None
        join_bank_by = None
        if group is not None:
            # The bank leg belongs to the group's own record, not to this row: no single
            # settlement row matched that credit, the set did.
            join_bank_by = "settlement_group"
        elif psp.counterparty_ref:
            candidates = [
                t for t in bank_by_utr.get(psp.counterparty_ref, []) if str(t.id) not in consumed
            ]
            if candidates:
                bank = candidates[0]
                join_bank_by = "counterparty_ref"

        if bank is None and group is None and psp.counterparty_ref:
            bank = _find_by_amount_and_date(psp, bank_rows, consumed, fallback_window_days)
            if bank is not None:
                join_bank_by = "amount_date_window"

        if group is not None:
            strategy = (
                MatchStrategy.AGGREGATE_SHARED_REFERENCE
                if group.method is GroupMethod.SHARED_REFERENCE
                else MatchStrategy.AGGREGATE_SUBSET_SUM
            )
        elif ledger is None and bank is None:
            # A processor row alone. Still recorded, so it appears in the numbers rather
            # than vanishing from the denominator.
            strategy = MatchStrategy.EXACT_REFERENCE
        elif join_bank_by == "amount_date_window":
            strategy = MatchStrategy.AMOUNT_DATE_WINDOW
        else:
            strategy = MatchStrategy.EXACT_REFERENCE

        # The order's ledger entry, whether or not it attached to *this* record. A split
        # group's siblings need it to classify as ROUTING_SPLIT rather than to look like
        # the ledger entry never existed.
        group_ledger = ledger
        if group_ledger is None and psp.order_ref:
            group_candidates = ledger_by_order.get(psp.order_ref, [])
            group_ledger = group_candidates[0] if group_candidates else None

        ctx = MatchContext(
            psp=_to_leg(psp),
            bank=_to_leg(bank) if bank else None,
            ledger=_to_leg(ledger) if ledger else None,
            strategy=strategy,
            tolerances=tolerances,
            timing_window_days=timing_window_days,
            sibling_psp_ids=tuple(t.external_id for t in siblings),
            sibling_sum_minor=sibling_sum,
            order_group_ledger=_to_leg(group_ledger) if group_ledger else None,
            settlement_group_present=group is not None,
        )
        findings = classify(ctx)

        if (
            group is None
            and abs(ctx.delta_minor) > tolerances.get("ROUNDING_DIFFERENCE", 0)
            and ctx.leg_count > 1
        ):
            strategy = (
                MatchStrategy.REFERENCE_AMOUNT_TOLERANCE
                if strategy is MatchStrategy.EXACT_REFERENCE
                else strategy
            )

        record = MatchRecord(
            match_key=psp.order_ref or psp.counterparty_ref or psp.external_id,
            status=_status_for(ctx, findings),
            strategy=strategy,
            confidence=_confidence_for(strategy, ctx.leg_count),
            psp_transaction_id=psp.id,
            bank_transaction_id=bank.id if bank else None,
            ledger_transaction_id=ledger.id if ledger else None,
            settlement_group_id=group.id if group is not None else None,
            amount_delta_minor=ctx.delta_minor,
            evidence={
                "pivot": "psp",
                "ledger_join": {
                    "field": "order_ref",
                    "value": psp.order_ref,
                    "matched": ledger is not None,
                },
                "bank_join": {
                    "field": join_bank_by or "counterparty_ref",
                    "value": psp.counterparty_ref,
                    "matched": bank is not None or group is not None,
                    "settlement_group_id": str(group.id) if group is not None else None,
                },
                "observed_minor": ctx.observed_minor,
                "expected_minor": ctx.expected_minor,
            },
            notes=f"{len(findings)} finding(s): "
            + (", ".join(f.rule_id for f in findings) or "none"),
        )
        records.append((record, findings))

        consumed.add(str(psp.id))
        if bank is not None:
            consumed.add(str(bank.id))
        if ledger is not None:
            consumed.add(str(ledger.id))

    # --- Pass 2: bank and ledger rows the processor report never covered ---------------
    # With no processor row, these two share no reference field at all, so amount+date is
    # the only option available -- and it is scored accordingly.
    leftover_ledger = [t for t in ledger_rows if str(t.id) not in consumed]
    for ledger in leftover_ledger:
        bank = _find_by_amount_and_date(ledger, bank_rows, consumed, fallback_window_days)

        ctx = MatchContext(
            psp=None,
            bank=_to_leg(bank) if bank else None,
            ledger=_to_leg(ledger),
            strategy=MatchStrategy.AMOUNT_DATE_WINDOW,
            tolerances=tolerances,
            timing_window_days=timing_window_days,
        )
        findings = classify(ctx)

        record = MatchRecord(
            match_key=ledger.order_ref or ledger.external_id,
            status=_status_for(ctx, findings),
            strategy=MatchStrategy.AMOUNT_DATE_WINDOW,
            confidence=_confidence_for(MatchStrategy.AMOUNT_DATE_WINDOW, ctx.leg_count),
            psp_transaction_id=None,
            bank_transaction_id=bank.id if bank else None,
            ledger_transaction_id=ledger.id,
            amount_delta_minor=ctx.delta_minor,
            evidence={
                "pivot": "ledger",
                "psp_join": {"field": "order_ref", "value": ledger.order_ref, "matched": False},
                "bank_join": {
                    "field": "amount_date_window",
                    "window_days": fallback_window_days,
                    "matched": bank is not None,
                },
                "note": "no shared reference field exists between bank and ledger",
            },
            notes=f"{len(findings)} finding(s): "
            + (", ".join(f.rule_id for f in findings) or "none"),
        )
        records.append((record, findings))
        consumed.add(str(ledger.id))
        if bank is not None:
            consumed.add(str(bank.id))

    # --- Pass 3: bank credits nothing accounts for -------------------------------------
    for bank in [t for t in bank_rows if str(t.id) not in consumed]:
        ctx = MatchContext(
            psp=None,
            bank=_to_leg(bank),
            ledger=None,
            strategy=MatchStrategy.AMOUNT_DATE_WINDOW,
            tolerances=tolerances,
            timing_window_days=timing_window_days,
        )
        findings = classify(ctx)
        record = MatchRecord(
            match_key=bank.counterparty_ref or bank.external_id,
            status=MatchStatus.PARTIAL,
            strategy=MatchStrategy.AMOUNT_DATE_WINDOW,
            confidence=CONFIDENCE_SINGLE_LEG,
            bank_transaction_id=bank.id,
            amount_delta_minor=0,
            evidence={
                "pivot": "bank",
                "psp_join": {
                    "field": "counterparty_ref",
                    "value": bank.counterparty_ref,
                    "matched": False,
                },
                "ledger_join": {"field": "amount_date_window", "matched": False},
            },
            notes="unattributed bank credit: no processor row and no ledger entry",
        )
        records.append((record, findings))
        consumed.add(str(bank.id))

    return _persist(session, records, len(psp_rows), len(bank_rows), len(ledger_rows))


def _find_by_amount_and_date(
    anchor: Transaction,
    candidates: list[Transaction],
    consumed: set[str],
    window_days: int,
) -> Transaction | None:
    """Exact amount, same currency, occurring within the window. Nearest date wins.

    Exact on amount, never approximate: pairing rows that merely resemble each other is
    how a reconciliation engine invents matches that were never there.
    """
    window = timedelta(days=window_days)
    viable = [
        t
        for t in candidates
        if str(t.id) not in consumed
        and t.amount_minor == anchor.amount_minor
        and t.currency == anchor.currency
        and abs(t.occurred_at - anchor.occurred_at) <= window
    ]
    if not viable:
        return None
    return min(viable, key=lambda t: (abs(t.occurred_at - anchor.occurred_at), t.external_id))


def _status_for(ctx: MatchContext, findings: list[Finding]) -> MatchStatus:
    if ctx.leg_count == 3 and not findings:
        return MatchStatus.FULL
    if ctx.leg_count == 3:
        return MatchStatus.BROKEN
    return MatchStatus.PARTIAL


def _confidence_for(strategy: MatchStrategy, leg_count: int) -> Decimal:
    if leg_count < 2:
        return CONFIDENCE_SINGLE_LEG
    if strategy is MatchStrategy.EXACT_REFERENCE:
        return CONFIDENCE_EXACT_REFERENCE
    if strategy is MatchStrategy.REFERENCE_AMOUNT_TOLERANCE:
        return CONFIDENCE_REFERENCE_TOLERANCE
    return CONFIDENCE_AMOUNT_DATE_WINDOW


def _persist(
    session: Session,
    records: list[tuple[MatchRecord, list[Finding]]],
    psp_rows: int,
    bank_rows: int,
    ledger_rows: int,
) -> ReconciliationResult:
    findings_by_category: dict[str, int] = defaultdict(int)
    total_findings = 0
    status_counts: dict[MatchStatus, int] = defaultdict(int)

    for record, findings in records:
        session.add(record)
        session.flush()  # assign record.id before its findings reference it
        status_counts[record.status] += 1

        for finding in findings:
            total_findings += 1
            findings_by_category[finding.category_code] += 1
            session.add(
                Discrepancy(
                    match_record_id=record.id,
                    category_code=finding.category_code,
                    rule_id=finding.rule_id,
                    delta_minor=finding.delta_minor,
                    evidence=finding.evidence,
                    summary=finding.summary,
                )
            )

        for txn_id in (
            record.psp_transaction_id,
            record.bank_transaction_id,
            record.ledger_transaction_id,
        ):
            if txn_id is None:
                continue
            txn = session.get(Transaction, txn_id)
            txn.status = (
                TransactionStatus.MATCHED if not findings else TransactionStatus.EXCEPTION
            )

    session.commit()

    result = ReconciliationResult(
        psp_rows=psp_rows,
        bank_rows=bank_rows,
        ledger_rows=ledger_rows,
        match_records=len(records),
        full_matches=status_counts[MatchStatus.FULL],
        partial_matches=status_counts[MatchStatus.PARTIAL],
        broken_matches=status_counts[MatchStatus.BROKEN],
        discrepancies=total_findings,
        findings_by_category=dict(findings_by_category),
    )
    logger.info(
        "reconciliation run completed",
        extra={
            "match_records": result.match_records,
            "full_matches": result.full_matches,
            "partial_matches": result.partial_matches,
            "broken_matches": result.broken_matches,
            "discrepancies": result.discrepancies,
        },
    )
    return result
