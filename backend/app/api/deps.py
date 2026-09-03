"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.db.session import SessionLocal


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
