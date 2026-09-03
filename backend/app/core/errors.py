"""Typed application errors with stable, machine-readable codes.

Every error surfaced over HTTP carries a `code` that clients and audit records can
match on. Codes are part of the API contract: rename one and you break a consumer.
"""

from __future__ import annotations

from typing import Any


class QuorumError(Exception):
    """Base for every error Quorum raises deliberately.

    Anything that is *not* a QuorumError escaping to the HTTP layer is an unexpected
    bug and is reported as such, rather than being dressed up as a handled condition.
    """

    code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ConfigurationError(QuorumError):
    """Settings are internally inconsistent or unsafe. Raised at startup, never at request time."""

    code = "configuration_error"
    http_status = 500


class MoneyError(QuorumError):
    """An amount could not be represented exactly. Never rounded away silently."""

    code = "money_error"
    http_status = 422


class UnsupportedCurrencyError(MoneyError):
    code = "unsupported_currency"
    http_status = 422


class NotFoundError(QuorumError):
    code = "not_found"
    http_status = 404


class DependencyUnavailableError(QuorumError):
    """A required backing service (Postgres) is unreachable."""

    code = "dependency_unavailable"
    http_status = 503
