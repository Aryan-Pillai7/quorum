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
    # Lite by choice, not by compromise: this layer restates field comparisons the rules
    # engine already made, which is not a reasoning task. It measured ~0.9s against
    # ~1.8s for the full flash model, and free-tier quota is per model, so a lite model
    # also leaves the heavier one free.
    gemini_model: str = "gemini-3.1-flash-lite"
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

    # Amounts at or above this are always human-reviewed, whatever a category has earned
    # (ADR-0027). INR 2,000.00 in paise. A judgement call sized so a meaningful share of
    # real settlements clears it, not a calibrated value.
    high_value_review_threshold_minor: int = Field(default=200_000, ge=0)

    # Exponential moving average weight for trust recalibration (ADR-0026). At 0.2 a
    # category recovers from a bad run in a handful of observations rather than being
    # anchored by its whole history, which is the behaviour wanted when a processor
    # changes something and the old evidence stops describing reality.
    trust_ema_alpha: float = Field(default=0.2, gt=0.0, le=1.0)

    # Share of trust-neutral approvals audited anyway, to keep an unbiased baseline
    # (ADR-0025). Approvals that would move a gate state are audited at 100% regardless.
    audit_baseline_rate: float = Field(default=0.2, ge=0.0, le=1.0)

    # Trust decay on silence (ADR-0030). Grace: how long a category may go without an
    # audited observation before its score starts being discounted. Decay: how long the
    # discount then takes to reach the review-threshold floor. Judgement calls.
    trust_decay_grace_days: int = Field(default=14, ge=1)
    trust_decay_days: int = Field(default=28, ge=1)

    # Demo-grade auth for the operational write endpoints only (ADR-0028). One shared
    # bearer token, no users, no roles, no expiry. Absent means those endpoints are
    # disabled rather than open: a missing token must never read as "no auth needed".
    operator_token: str | None = None

    @property
    def approvals_enabled(self) -> bool:
        return bool(self.operator_token)

    @field_validator("gemini_api_key", "operator_token", mode="before")
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
