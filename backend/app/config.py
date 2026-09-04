"""Application settings, and the startup guard that validates them.

`validate_settings` is called from `create_app()` in app/main.py -- it is not
decoration. A misconfigured Quorum fails at boot with a specific message rather than
at 2am with a confusing one.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ConfigurationError

Environment = Literal["local", "test", "demo", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    environment: Environment = "local"
    log_level: str = "INFO"

    # Postgres is the source of truth. Required.
    database_url: str = "postgresql+psycopg://quorum:quorum@localhost:55432/quorum"

    # Redis is a cache only (ADR-0009). The app runs correctly without it.
    redis_url: str = "redis://localhost:6379/0"
    redis_socket_timeout_seconds: float = 0.5
    trust_cache_ttl_seconds: int = 300

    # Gemini (ADR-0016). Absent means the agent layer is disabled and says so, rather
    # than silently degrading to no explanations with no indication why.
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash"
    gemini_timeout_seconds: float = 30.0
    # Transient 5xx from the API was observed in roughly a third of calls during Phase 3
    # testing, so retry is a requirement here, not a precaution.
    gemini_max_retries: int = 3
    # Pause between category batches. The free tier is per-minute, and nine batches fired
    # back to back reliably trips it; pacing avoids the retry path rather than relying on
    # it. Set to 0.0 to disable when running against a paid quota.
    gemini_inter_batch_seconds: float = 1.5

    # Fallback trust-gate thresholds, used only for a category with no trust_scores row.
    # Live thresholds are per-row in Postgres so they can be tuned per category.
    default_auto_apply_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    default_review_threshold: float = Field(default=0.60, ge=0.0, le=1.0)
    default_min_sample_size: int = Field(default=50, ge=1)

    @field_validator("gemini_api_key", mode="before")
    @classmethod
    def _blank_key_is_no_key(cls, value: str | None) -> str | None:
        """Treat an empty or whitespace-only key as absent.

        docker-compose passes GEMINI_API_KEY through as "" when the host has not set
        it. Without this, the empty string reads as a configured key and the service
        reports agent_enabled=true while having no way to call Gemini -- a capability
        claim it cannot honour.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def agent_enabled(self) -> bool:
        return bool(self.gemini_api_key)


def validate_settings(settings: Settings) -> Settings:
    """Fail fast on configuration that is inconsistent or unsafe.

    Called by create_app() at startup. Returns the settings so it can be used inline.
    """
    problems: list[str] = []

    if not settings.database_url.startswith("postgresql"):
        problems.append(
            f"database_url must be a postgresql URL (Quorum relies on JSONB and partial "
            f"unique indexes); got {settings.database_url.split(':', 1)[0]!r}"
        )

    if not settings.redis_url.startswith(("redis://", "rediss://", "unix://")):
        problems.append(
            f"redis_url must start with redis:// or rediss://; got {settings.redis_url!r}"
        )

    if settings.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        problems.append(f"log_level {settings.log_level!r} is not a valid level name")

    # A review threshold at or above the auto-apply threshold makes the gate incoherent:
    # there would be no band in which a human is asked.
    if settings.default_review_threshold >= settings.default_auto_apply_threshold:
        problems.append(
            f"default_review_threshold ({settings.default_review_threshold}) must be strictly "
            f"below default_auto_apply_threshold ({settings.default_auto_apply_threshold}), "
            f"otherwise no human-review band exists"
        )

    if settings.environment == "production" and not settings.agent_enabled:
        problems.append("gemini_api_key is required when environment=production")

    if problems:
        raise ConfigurationError(
            "invalid configuration: " + "; ".join(problems), details={"problems": problems}
        )
    return settings


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton. Cached so env is read once."""
    return Settings()
