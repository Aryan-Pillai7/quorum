"""CSV ingestion: source-specific adapters normalizing into `transactions`."""

from app.services.ingestion.adapters import (
    ADAPTERS,
    BankStatementAdapter,
    InternalLedgerAdapter,
    NormalizedRow,
    PSPSettlementAdapter,
    RowError,
    SourceAdapter,
)
from app.services.ingestion.runner import IngestionResult, ingest_csv

__all__ = [
    "ADAPTERS",
    "BankStatementAdapter",
    "IngestionResult",
    "InternalLedgerAdapter",
    "NormalizedRow",
    "PSPSettlementAdapter",
    "RowError",
    "SourceAdapter",
    "ingest_csv",
]
