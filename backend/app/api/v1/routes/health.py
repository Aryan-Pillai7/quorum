"""Health endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.config import get_settings
from app.schemas.health import LivenessResponse, ReadinessResponse
from app.services.health import check_readiness

router = APIRouter(tags=["health"])

SERVICE_VERSION = "0.1.0"


@router.get("/health", response_model=LivenessResponse, summary="Liveness probe")
def health() -> LivenessResponse:
    """Is the process up? Touches no dependency, so it never fails because Postgres did."""
    settings = get_settings()
    return LivenessResponse(
        status="ok",
        service="quorum",
        version=SERVICE_VERSION,
        environment=settings.environment,
    )


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"model": ReadinessResponse, "description": "Postgres unreachable"}},
)
def readiness(response: Response) -> ReadinessResponse:
    """Can the service do useful work?

    Returns 503 only when Postgres is unreachable. Redis being down reports as degraded
    with a 200, because the system stays correct without it (ADR-0009).
    """
    report = check_readiness()
    if not report.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse.model_validate(report.to_dict())
