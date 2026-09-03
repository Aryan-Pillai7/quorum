"""v1 API router.

Health lives at the root (/health) rather than under /v1: probes should not have to
track an API version to know whether the process is alive.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import health, trust

root_router = APIRouter()
root_router.include_router(health.router)

v1_router = APIRouter(prefix="/v1")
v1_router.include_router(trust.router)
