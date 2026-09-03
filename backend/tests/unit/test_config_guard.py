"""validate_settings, and proof that it is actually wired into startup.

A validation function nobody calls is dead code wearing a safety vest. The last test
here fails if create_app() stops invoking it.
"""

from __future__ import annotations

import pytest

from app.config import Settings, validate_settings
from app.core.errors import ConfigurationError


def _settings(**overrides) -> Settings:
    base = {
        "environment": "local",
        "database_url": "postgresql+psycopg://quorum:quorum@localhost:5432/quorum",
        "redis_url": "redis://localhost:6379/0",
        "log_level": "INFO",
        "default_auto_apply_threshold": 0.90,
        "default_review_threshold": 0.60,
        "default_min_sample_size": 50,
    }
    return Settings(**{**base, **overrides})


def test_valid_settings_pass_through():
    settings = _settings()
    assert validate_settings(settings) is settings


def test_non_postgres_database_url_is_rejected():
    """Quorum relies on JSONB and on NULLs under unique indexes; sqlite has neither."""
    with pytest.raises(ConfigurationError, match="must be a postgresql URL"):
        validate_settings(_settings(database_url="sqlite:///./quorum.db"))


def test_bad_redis_scheme_is_rejected():
    with pytest.raises(ConfigurationError, match="redis_url must start with"):
        validate_settings(_settings(redis_url="http://localhost:6379"))


def test_invalid_log_level_is_rejected():
    with pytest.raises(ConfigurationError, match="not a valid level name"):
        validate_settings(_settings(log_level="LOUD"))


def test_review_threshold_at_or_above_auto_apply_is_rejected():
    """Otherwise there is no band in which a human is asked, and the gate is incoherent."""
    with pytest.raises(ConfigurationError, match="no human-review band exists"):
        validate_settings(
            _settings(default_review_threshold=0.95, default_auto_apply_threshold=0.90)
        )


def test_equal_thresholds_are_rejected_too():
    with pytest.raises(ConfigurationError, match="no human-review band exists"):
        validate_settings(
            _settings(default_review_threshold=0.90, default_auto_apply_threshold=0.90)
        )


def test_production_requires_an_anthropic_key():
    with pytest.raises(ConfigurationError, match="anthropic_api_key is required"):
        validate_settings(_settings(environment="production", anthropic_api_key=None))


def test_local_does_not_require_an_anthropic_key():
    """Phase 1 has no AI layer; local setup must not demand a key to boot."""
    settings = validate_settings(_settings(environment="local"))
    assert settings.agent_enabled is False


def test_all_problems_are_reported_together():
    """One boot, one complete list -- not a game of whack-a-mole."""
    with pytest.raises(ConfigurationError) as exc_info:
        validate_settings(_settings(database_url="sqlite://", log_level="LOUD"))
    problems = exc_info.value.details["problems"]
    assert len(problems) == 2


def test_create_app_calls_the_guard(monkeypatch):
    """The wiring test: bad config must fail at boot, not at first request."""
    from app import main

    monkeypatch.setattr(main, "get_settings", lambda: _settings(database_url="sqlite://"))
    with pytest.raises(ConfigurationError, match="must be a postgresql URL"):
        main.create_app()
