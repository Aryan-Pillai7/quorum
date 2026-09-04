"""Ingestion runner: CSV file -> IngestionBatch + transactions + quarantined rows.

Two properties this is built around:

1. **One bad row must not cost the batch.** A malformed date on line 217 quarantines
   line 217 and nothing else. The alternative -- aborting -- means an operator fixes one
   cell, re-runs, and discovers the next bad cell, one round trip at a time.

2. **Counts must reconcile.** ingested + quarantined = total, enforced by a database
   CHECK. Anything else makes a match rate computed from these numbers meaningless,
   because the denominator would be unknowable.

Re-ingesting a file already ingested is refused up front on content hash, rather than
discovered row by row through unique-constraint violations.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import QuorumError
from app.models import (
    BatchStatus,
    IngestionBatch,
    QuarantinedRow,
    QuarantineReason,
    SourceSystem,
    Transaction,
    TransactionStatus,
)
from app.services.ingestion.adapters import ADAPTERS, RowError

logger = logging.getLogger(__name__)


class DuplicateBatchError(QuorumError):
    """This exact file has already been ingested."""

    code = "duplicate_batch"
    http_status = 409


class MalformedFileError(QuorumError):
    """The file is unusable as a whole -- missing columns, empty, unreadable."""

    code = "malformed_file"
    http_status = 422


@dataclass(frozen=True)
class IngestionResult:
    batch_id: str
    source: SourceSystem
    filename: str
    total_rows: int
    ingested_rows: int
    quarantined_rows: int

    @property
    def quarantine_rate(self) -> float:
        """Share of rows that could not be normalized. 0.0 for an empty file."""
        return self.quarantined_rows / self.total_rows if self.total_rows else 0.0


def ingest_csv(
    session: Session,
    *,
    source: SourceSystem,
    path: Path,
    allow_reingest: bool = False,
) -> IngestionResult:
    """Ingest one CSV file for one source.

    Commits once at the end: a batch is either fully recorded with counts that reconcile,
    or not recorded at all. A half-written batch is worse than no batch, because its
    counts would silently understate the denominator of every rate derived from it.
    """
    content = path.read_bytes()
    content_hash = hashlib.sha256(content).hexdigest()

    if not allow_reingest:
        existing = session.execute(
            select(IngestionBatch).where(
                IngestionBatch.content_hash == content_hash,
                IngestionBatch.status == BatchStatus.COMPLETED,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise DuplicateBatchError(
                f"{path.name} has already been ingested as batch {existing.id} "
                f"({existing.ingested_rows} rows on {existing.started_at:%Y-%m-%d}). "
                f"Pass allow_reingest=True to ingest it again.",
                details={"batch_id": str(existing.id), "content_hash": content_hash},
            )

    adapter = ADAPTERS[source]
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))

    if reader.fieldnames is None:
        raise MalformedFileError(f"{path.name} is empty: no header row")

    # Checked once, before any row is parsed. A file whose columns moved should fail as a
    # file, not produce hundreds of quarantined rows that bury the actual cause.
    missing = adapter.missing_columns(list(reader.fieldnames))
    if missing:
        raise MalformedFileError(
            f"{path.name} is missing required column(s) for {source.value}: "
            f"{', '.join(missing)}",
            details={"missing_columns": missing, "found_columns": list(reader.fieldnames)},
        )

    batch = IngestionBatch(
        source=source,
        filename=path.name,
        content_hash=content_hash,
        status=BatchStatus.PENDING,
        started_at=datetime.now(UTC),
    )
    session.add(batch)
    session.flush()  # assigns batch.id so rows can reference it in this same transaction

    total = 0
    ingested = 0
    quarantined = 0
    # Duplicates *within* one file are caught here rather than by the database, so the
    # reason recorded is DUPLICATE_IN_BATCH rather than an opaque constraint violation.
    seen_external_ids: set[str] = set()

    for row_number, row in enumerate(reader, start=2):  # line 1 is the header
        total += 1
        normalized, error = adapter.normalize(row)

        if error is None and normalized.external_id in seen_external_ids:
            error = RowError(
                QuarantineReason.DUPLICATE_IN_BATCH,
                f"external_id {normalized.external_id!r} already appeared earlier in this file",
            )

        if error is not None:
            quarantined += 1
            session.add(
                QuarantinedRow(
                    batch_id=batch.id,
                    row_number=row_number,
                    reason=error.reason,
                    detail=error.detail,
                    raw={k: v for k, v in row.items() if k is not None},
                )
            )
            continue

        seen_external_ids.add(normalized.external_id)
        ingested += 1
        session.add(
            Transaction(
                batch_id=batch.id,
                source=source,
                external_id=normalized.external_id,
                counterparty_ref=normalized.counterparty_ref,
                order_ref=normalized.order_ref,
                amount_minor=normalized.amount_minor,
                gross_amount_minor=normalized.gross_amount_minor,
                fee_minor=normalized.fee_minor,
                currency=normalized.currency,
                direction=normalized.direction,
                occurred_at=normalized.occurred_at,
                description=normalized.description,
                status=TransactionStatus.UNMATCHED,
                raw=normalized.raw,
            )
        )

    batch.total_rows = total
    batch.ingested_rows = ingested
    batch.quarantined_rows = quarantined
    batch.status = BatchStatus.COMPLETED
    batch.completed_at = datetime.now(UTC)

    session.commit()

    logger.info(
        "ingestion batch completed",
        extra={
            "batch_id": str(batch.id),
            "source": source.value,
            "filename": path.name,
            "total_rows": total,
            "ingested_rows": ingested,
            "quarantined_rows": quarantined,
        },
    )

    return IngestionResult(
        batch_id=str(batch.id),
        source=source,
        filename=path.name,
        total_rows=total,
        ingested_rows=ingested,
        quarantined_rows=quarantined,
    )
