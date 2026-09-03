"""Health endpoints and the readiness policy.

The policy under test: Postgres down means not ready; Redis down means degraded but
still ready (ADR-0009). A readiness probe that fails on a cache outage turns a
slowdown into an outage.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.services import health as health_service
from app.services.health import ComponentHealth, ReadinessReport, check_readiness


class _FailingEngine:
    def connect(self):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


def test_liveness_returns_ok_without_touching_dependencies(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "quorum"
    assert body["version"] == "0.1.0"


def test_liveness_echoes_a_supplied_request_id(client):
    """Correlation must survive across services, not restart at our boundary."""
    response = client.get("/health", headers={"X-Request-ID": "trace-abc-123"})
    assert response.headers["X-Request-ID"] == "trace-abc-123"


def test_liveness_generates_a_request_id_when_none_supplied(client):
    response = client.get("/health")
    assert len(response.headers["X-Request-ID"]) == 32


def test_readiness_is_not_ready_when_postgres_is_down(monkeypatch):
    monkeypatch.setattr(health_service, "engine", _FailingEngine())
    monkeypatch.setattr(health_service.cache, "ping", lambda: True)

    report = check_readiness()

    assert report.ready is False
    postgres = next(c for c in report.components if c.name == "postgres")
    assert postgres.status == "down"
    assert postgres.detail  # the failure reason is reported, not swallowed


def test_readiness_stays_ready_when_only_redis_is_down(monkeypatch):
    """The load-bearing assertion of ADR-0009."""
    monkeypatch.setattr(health_service.cache, "ping", lambda: False)
    monkeypatch.setattr(
        health_service, "_check_postgres", lambda: ComponentHealth("postgres", "ok", 1.0)
    )

    report = check_readiness()

    assert report.ready is True
    redis_component = next(c for c in report.components if c.name == "redis")
    assert redis_component.status == "degraded"
    assert redis_component.status != "down"


def test_readiness_reports_both_components(monkeypatch):
    monkeypatch.setattr(
        health_service, "_check_postgres", lambda: ComponentHealth("postgres", "ok", 1.0)
    )
    monkeypatch.setattr(health_service.cache, "ping", lambda: True)

    report = check_readiness()

    assert {c.name for c in report.components} == {"postgres", "redis"}


@pytest.mark.parametrize(
    ("ready", "expected_status"), [(True, 200), (False, 503)]
)
def test_readiness_endpoint_maps_readiness_to_http_status(
    client, monkeypatch, ready, expected_status
):
    from app.api.v1.routes import health as health_route

    monkeypatch.setattr(
        health_route,
        "check_readiness",
        lambda: ReadinessReport(
            ready=ready,
            components=[ComponentHealth("postgres", "ok" if ready else "down", 1.0)],
        ),
    )
    response = client.get("/health/ready")
    assert response.status_code == expected_status
    assert response.json()["ready"] is ready


def test_unknown_route_returns_404(client):
    assert client.get("/v1/does-not-exist").status_code == 404
