"""Wire contracts for the health endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LivenessResponse(BaseModel):
    status: str = Field(examples=["ok"])
    service: str = Field(examples=["quorum"])
    version: str = Field(examples=["0.1.0"])
    environment: str = Field(examples=["local"])


class ComponentHealthResponse(BaseModel):
    name: str
    status: str = Field(description="ok | down | degraded")
    latency_ms: float
    detail: str | None = None


class ReadinessResponse(BaseModel):
    ready: bool = Field(
        description=(
            "False only when Postgres is unreachable. Redis is a cache: its absence "
            "reports as degraded and does not affect readiness."
        )
    )
    components: list[ComponentHealthResponse]
