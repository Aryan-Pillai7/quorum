"""Shared FastAPI dependencies."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.errors import QuorumError
from app.db.session import SessionLocal


class UnauthorizedError(QuorumError):
    """Bad or missing operator token."""

    code = "unauthorized"
    http_status = 401


class ApprovalsDisabledError(QuorumError):
    """No operator token configured, so the write endpoints are off.

    Distinct from a 500: nothing failed. The feature is deliberately unavailable, and a
    caller needs to be able to tell those apart.
    """

    code = "approvals_disabled"
    http_status = 503


def get_db() -> Iterator[Session]:
    """Request-scoped database session.

    No implicit commit: endpoints that write commit explicitly, so a write is always
    visible in the code that performs it rather than happening as a side effect of the
    request ending.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def require_operator_token(
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> str:
    """Gate the operational write endpoints behind one shared bearer token (ADR-0028).

    Demo-grade on purpose, and stated as such in the README: one token, no users, no
    roles, no expiry, no revocation. It exists so that approving a correction is not an
    anonymous unauthenticated POST, which is a meaningfully different thing from being
    a real authorization system.

    Fails CLOSED. With no token configured these endpoints are disabled rather than open,
    because a missing secret must never be read as "no secret needed".
    """
    if not settings.operator_token:
        raise ApprovalsDisabledError(
            "Approval endpoints are disabled because OPERATOR_TOKEN is not set. "
            "Set it to enable them; leaving it unset does not make them public."
        )

    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not supplied or not secrets.compare_digest(supplied, settings.operator_token):
        raise UnauthorizedError("missing or invalid operator token")
    return supplied
