"""Aggregated payout detection: one bank credit explained by a SET of settlement rows.

SEALED (ADR-0004): pure functions, no database, no clock, no network. Everything here is
a deterministic function of its arguments, so the same inputs give the same grouping
forever.

Phase 4 removes the 1:1 assumption from ADR-0014 for the settlement->bank leg only. The
ledger->settlement leg is untouched: each settlement row still matches its own ledger
entry by order_ref, and aggregation changes nothing about that.

Two passes, in order:

1. **Shared reference.** N payments settling under one settlement UTR, with the bank
   showing one credit carrying that UTR, is what aggregation actually looks like in
   practice. Group by UTR, verify the sum. No search, no ambiguity, exact.

2. **Bounded subset-sum.** Only for credits pass 1 cannot explain. This is a search, so
   it is bounded on every axis and it is allowed to give up -- but giving up is reported
   as INCONCLUSIVE, never as "no aggregation exists".

The refusal to guess is the important part. If two different sets of settlement rows both
sum to the credit, the engine says AMBIGUOUS and hands both to a human. Picking one would
silently attribute money to the wrong payments, which is the specific failure a
reconciliation tool exists to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.models.enums import GroupMethod, GroupStatus

logger = logging.getLogger(__name__)

# --- Search bounds --------------------------------------------------------------------
# Every one of these is a deliberate refusal to let the search grow without limit. They
# are judgement calls sized for a laptop, not tuned values, and they are reported in the
# group's evidence so a bounded-out result is always visible as such.

# Settlement rows must fall within this many days of the bank credit to be candidates.
AGGREGATION_WINDOW_DAYS = 3

# More candidates than this and subset-sum is not attempted at all. 2^40 is not a search.
MAX_CANDIDATES = 40

# A payout of more than this many payments is not something this pass will claim to have
# resolved, even if a subset happens to sum.
MAX_GROUP_SIZE = 50

# Hard ceiling on recursion steps. Guarantees termination regardless of input shape.
MAX_SEARCH_NODES = 200_000

# Finding a second solution answers the only question that matters (is it unique?), so
# the search stops there rather than enumerating all of them.
SOLUTION_CAP = 2


@dataclass(frozen=True)
class CandidateRow:
    """A settlement row eligible to be part of a group."""

    transaction_id: str
    external_id: str
    amount_minor: int
    occurred_at: datetime
    currency: str
    counterparty_ref: str | None


@dataclass(frozen=True)
class BankCredit:
    """The bank line an aggregation is trying to explain."""

    transaction_id: str
    external_id: str
    amount_minor: int
    occurred_at: datetime
    currency: str
    counterparty_ref: str | None


@dataclass
class AggregationResult:
    """The outcome of trying to explain one bank credit.

    `status` is the honest three-way answer: it resolved, several answers fit, or the
    search was bounded out before it could tell. Only RESOLVED forms a group.
    """

    bank: BankCredit
    status: GroupStatus
    method: GroupMethod
    members: list[CandidateRow] = field(default_factory=list)
    competing_solutions: list[list[str]] = field(default_factory=list)
    candidates_considered: int = 0
    nodes_explored: int = 0
    solution_count: int = 0
    reason: str = ""

    @property
    def members_total_minor(self) -> int:
        return sum(m.amount_minor for m in self.members)

    @property
    def delta_minor(self) -> int:
        return self.members_total_minor - self.bank.amount_minor

    def to_evidence(self) -> dict[str, Any]:
        return {
            "method": self.method.value,
            "status": self.status.value,
            "reason": self.reason,
            "bank_transaction": self.bank.external_id,
            "bank_amount_minor": self.bank.amount_minor,
            "members": [m.external_id for m in self.members],
            "members_total_minor": self.members_total_minor,
            "delta_minor": self.delta_minor,
            "candidates_considered": self.candidates_considered,
            "nodes_explored": self.nodes_explored,
            "solution_count": self.solution_count,
            "competing_solutions": self.competing_solutions,
            "bounds": {
                "window_days": AGGREGATION_WINDOW_DAYS,
                "max_candidates": MAX_CANDIDATES,
                "max_group_size": MAX_GROUP_SIZE,
                "max_search_nodes": MAX_SEARCH_NODES,
            },
        }


def bound_candidates(
    bank: BankCredit,
    candidates: list[CandidateRow],
    *,
    window_days: int = AGGREGATION_WINDOW_DAYS,
) -> list[CandidateRow]:
    """Narrow the pool before any search runs.

    Same currency, within the date window, and no row larger than the credit itself --
    a settlement row bigger than the whole payout cannot be part of it. Sorted
    descending by amount then by external_id, which is both the right order for pruning
    and a stable order, so the search is reproducible.
    """
    window = timedelta(days=window_days)
    viable = [
        c
        for c in candidates
        if c.currency == bank.currency
        and abs(c.occurred_at - bank.occurred_at) <= window
        and 0 < c.amount_minor <= bank.amount_minor
    ]
    return sorted(viable, key=lambda c: (-c.amount_minor, c.external_id))


def group_by_shared_reference(
    bank: BankCredit, candidates: list[CandidateRow], *, tolerance_minor: int
) -> AggregationResult | None:
    """Pass 1: settlement rows carrying the bank credit's own settlement UTR.

    Returns None when the shape does not apply at all (no reference, or fewer than two
    rows carry it), so the caller can fall through to the search.
    """
    if not bank.counterparty_ref:
        return None

    members = sorted(
        (c for c in candidates if c.counterparty_ref == bank.counterparty_ref),
        key=lambda c: c.external_id,
    )
    if len(members) < 2:
        # One row sharing the reference is an ordinary 1:1 match, not an aggregation.
        return None

    total = sum(m.amount_minor for m in members)
    delta = total - bank.amount_minor

    if abs(delta) > tolerance_minor:
        # The rows genuinely belong together -- they carry the payout's own reference --
        # but they do not add up. That is a real discrepancy about a real group, not a
        # reason to go searching for a different set that happens to sum.
        return AggregationResult(
            bank=bank,
            status=GroupStatus.RESOLVED if delta == 0 else GroupStatus.INCONCLUSIVE,
            method=GroupMethod.SHARED_REFERENCE,
            members=members,
            candidates_considered=len(candidates),
            solution_count=1,
            reason=(
                f"{len(members)} settlement rows carry the credit's settlement reference "
                f"{bank.counterparty_ref!r} but sum to {total} against a credit of "
                f"{bank.amount_minor}, a difference of {delta} minor units"
            ),
        )

    return AggregationResult(
        bank=bank,
        status=GroupStatus.RESOLVED,
        method=GroupMethod.SHARED_REFERENCE,
        members=members,
        candidates_considered=len(candidates),
        solution_count=1,
        reason=(
            f"{len(members)} settlement rows carry the credit's settlement reference "
            f"{bank.counterparty_ref!r} and sum to {total}, matching the credit"
        ),
    )


def find_subsets_summing_to(
    candidates: list[CandidateRow],
    target_minor: int,
    *,
    tolerance_minor: int = 0,
    max_group_size: int = MAX_GROUP_SIZE,
    max_nodes: int = MAX_SEARCH_NODES,
    solution_cap: int = SOLUTION_CAP,
) -> tuple[list[list[int]], int, bool]:
    """Bounded subset-sum. Returns (solutions as index lists, nodes explored, exhausted).

    `exhausted` is True when the search completed within its budget -- meaning the
    solution list is the whole truth. False means it was cut short, and the caller must
    not read "no solutions" as "none exist".

    Depth-first over amounts sorted descending, with two prunes: stop when the running
    total exceeds the target beyond tolerance, and stop when everything remaining is not
    enough to reach it. Distinct index sets count as distinct solutions even when the
    amounts are equal, because two rows of the same value are two different payments and
    knowing which one the credit covers is the entire question.
    """
    solutions: list[list[int]] = []
    nodes = 0
    truncated = False

    # suffix_sums[i] is the total of candidates[i:], used for the "cannot reach" prune.
    suffix_sums = [0] * (len(candidates) + 1)
    for i in range(len(candidates) - 1, -1, -1):
        suffix_sums[i] = suffix_sums[i + 1] + candidates[i].amount_minor

    def search(start: int, running: int, chosen: list[int]) -> None:
        nonlocal nodes, truncated

        if truncated or len(solutions) >= solution_cap:
            return

        nodes += 1
        if nodes > max_nodes:
            truncated = True
            return

        if abs(running - target_minor) <= tolerance_minor and chosen:
            solutions.append(list(chosen))
            return

        if running > target_minor + tolerance_minor:
            return
        if len(chosen) >= max_group_size:
            return
        if running + suffix_sums[start] < target_minor - tolerance_minor:
            return

        for index in range(start, len(candidates)):
            chosen.append(index)
            search(index + 1, running + candidates[index].amount_minor, chosen)
            chosen.pop()
            if truncated or len(solutions) >= solution_cap:
                return

    search(0, 0, [])
    return solutions, nodes, not truncated


def group_by_subset_sum(
    bank: BankCredit, candidates: list[CandidateRow], *, tolerance_minor: int
) -> AggregationResult:
    """Pass 2: bounded search over the narrowed candidate pool."""
    bounded = bound_candidates(bank, candidates)

    if len(bounded) < 2:
        return AggregationResult(
            bank=bank,
            status=GroupStatus.INCONCLUSIVE,
            method=GroupMethod.SUBSET_SUM,
            candidates_considered=len(bounded),
            reason=(
                f"only {len(bounded)} settlement row(s) fall within the "
                f"{AGGREGATION_WINDOW_DAYS}-day window in {bank.currency}; an "
                f"aggregation needs at least two"
            ),
        )

    if len(bounded) > MAX_CANDIDATES:
        # Refusing to start is more honest than starting and timing out: the result says
        # "not searched", which is different from "searched and found nothing".
        return AggregationResult(
            bank=bank,
            status=GroupStatus.INCONCLUSIVE,
            method=GroupMethod.SUBSET_SUM,
            candidates_considered=len(bounded),
            reason=(
                f"{len(bounded)} candidates exceed the {MAX_CANDIDATES}-row search limit; "
                f"subset-sum was not attempted for this credit"
            ),
        )

    solutions, nodes, exhausted = find_subsets_summing_to(
        bounded, bank.amount_minor, tolerance_minor=tolerance_minor
    )

    if not exhausted:
        return AggregationResult(
            bank=bank,
            status=GroupStatus.INCONCLUSIVE,
            method=GroupMethod.SUBSET_SUM,
            candidates_considered=len(bounded),
            nodes_explored=nodes,
            solution_count=len(solutions),
            reason=(
                f"search stopped after {nodes} steps without exhausting the space; "
                f"no conclusion is drawn either way"
            ),
        )

    if not solutions:
        return AggregationResult(
            bank=bank,
            status=GroupStatus.INCONCLUSIVE,
            method=GroupMethod.SUBSET_SUM,
            candidates_considered=len(bounded),
            nodes_explored=nodes,
            reason=(
                f"no subset of the {len(bounded)} candidates sums to "
                f"{bank.amount_minor} within a tolerance of {tolerance_minor}"
            ),
        )

    if len(solutions) > 1:
        competing = [
            sorted(bounded[i].external_id for i in solution) for solution in solutions
        ]
        return AggregationResult(
            bank=bank,
            status=GroupStatus.AMBIGUOUS,
            method=GroupMethod.SUBSET_SUM,
            candidates_considered=len(bounded),
            nodes_explored=nodes,
            solution_count=len(solutions),
            competing_solutions=competing,
            reason=(
                f"at least {len(solutions)} distinct sets of settlement rows sum to "
                f"{bank.amount_minor}; refusing to choose between them"
            ),
        )

    members = sorted((bounded[i] for i in solutions[0]), key=lambda c: c.external_id)
    if len(members) < 2:
        # A single row summing to the credit is a 1:1 match that the earlier pass should
        # have made. Not an aggregation, so it is not claimed as one.
        return AggregationResult(
            bank=bank,
            status=GroupStatus.INCONCLUSIVE,
            method=GroupMethod.SUBSET_SUM,
            candidates_considered=len(bounded),
            nodes_explored=nodes,
            solution_count=1,
            reason="the only solution is a single settlement row, which is a 1:1 match",
        )

    return AggregationResult(
        bank=bank,
        status=GroupStatus.RESOLVED,
        method=GroupMethod.SUBSET_SUM,
        members=members,
        candidates_considered=len(bounded),
        nodes_explored=nodes,
        solution_count=1,
        reason=(
            f"exactly one set of {len(members)} settlement rows out of {len(bounded)} "
            f"candidates sums to {bank.amount_minor}"
        ),
    )


def detect_aggregation(
    bank: BankCredit, candidates: list[CandidateRow], *, tolerance_minor: int
) -> AggregationResult:
    """Try to explain one bank credit as a set of settlement rows.

    Shared reference first because it is exact and cheap; the search only runs when that
    shape does not apply.
    """
    shared = group_by_shared_reference(bank, candidates, tolerance_minor=tolerance_minor)
    if shared is not None:
        return shared
    return group_by_subset_sum(bank, candidates, tolerance_minor=tolerance_minor)
