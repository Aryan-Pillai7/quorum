"""ORM models.

Imported as a package so that `Base.metadata` is fully populated before Alembic or the
schema-invariant tests inspect it. A model that is not imported here is invisible to
migrations, which is a silent and expensive failure mode.
"""

from app.models.audit_event import AuditEvent
from app.models.discrepancy import NOVEL_CATEGORY_CODE, Discrepancy
from app.models.discrepancy_category import DiscrepancyCategory
from app.models.enums import (
    ActorType,
    BatchStatus,
    DetectedBy,
    Direction,
    GateDecision,
    GroupMethod,
    GroupStatus,
    MatchStatus,
    MatchStrategy,
    QuarantineReason,
    Severity,
    SourceSystem,
    TransactionStatus,
)
from app.models.ingestion import IngestionBatch, QuarantinedRow
from app.models.match_record import MatchRecord
from app.models.settlement_group import SettlementGroup
from app.models.transaction import Transaction
from app.models.trust_score import TrustScore

__all__ = [
    "NOVEL_CATEGORY_CODE",
    "ActorType",
    "AuditEvent",
    "BatchStatus",
    "DetectedBy",
    "Direction",
    "Discrepancy",
    "DiscrepancyCategory",
    "GateDecision",
    "GroupMethod",
    "GroupStatus",
    "IngestionBatch",
    "MatchRecord",
    "MatchStatus",
    "MatchStrategy",
    "QuarantineReason",
    "QuarantinedRow",
    "Severity",
    "SettlementGroup",
    "SourceSystem",
    "Transaction",
    "TransactionStatus",
    "TrustScore",
]
