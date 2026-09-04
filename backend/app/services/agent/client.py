"""Gemini client wrapper.

Everything the rest of the system needs to know about calling a model lives behind this
module: retries, timeouts, token accounting, latency, and -- most importantly -- what
happens when there is no API key.

On that last point: an absent key makes the agent layer **unavailable**, and every caller
is told so explicitly. It does not produce empty explanations, placeholder text, or a
silently shorter response. A reconciliation tool that quietly stops explaining things is
worse than one that says it cannot.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from app.config import Settings, get_settings
from app.core.errors import QuorumError

logger = logging.getLogger(__name__)

# 4xx statuses that mean "later", not "wrong". 429 is rate limiting; 408 is a server-side
# timeout. Everything else in the 4xx range is a request the server will keep rejecting.
RETRYABLE_CLIENT_STATUSES = frozenset({408, 429})

# Longer than the 5xx backoff: a quota window outlasts a transient blip.
RATE_LIMIT_BASE_BACKOFF_SECONDS = 5.0


class AgentUnavailableError(QuorumError):
    """The agent layer cannot run. Always carries the reason."""

    code = "agent_unavailable"
    http_status = 503


@dataclass(frozen=True)
class ModelCall:
    """One completed call, with everything needed to account for it afterwards."""

    text: str
    model: str
    latency_ms: float
    prompt_tokens: int | None
    output_tokens: int | None
    attempts: int

    @property
    def total_tokens(self) -> int | None:
        if self.prompt_tokens is None or self.output_tokens is None:
            return None
        return self.prompt_tokens + self.output_tokens


def agent_availability(settings: Settings | None = None) -> tuple[bool, str]:
    """(available, reason). The reason is surfaced in API responses verbatim."""
    settings = settings or get_settings()
    if not settings.gemini_api_key:
        return False, (
            "GEMINI_API_KEY is not set, so explanations were not generated. "
            "Reconciliation and trust gating ran normally and are unaffected."
        )
    return True, "agent layer available"


class GeminiClient:
    """Thin wrapper over google-genai.

    Constructed per run rather than held as a module singleton, so a key added to the
    environment takes effect on the next run without a process restart -- which matters
    when the demo is being set up minutes before it is given.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        available, reason = agent_availability(self.settings)
        if not available:
            raise AgentUnavailableError(reason)

        # Imported lazily so the deterministic core and the rest of the app keep working
        # (and keep importing) on a machine where the SDK is not installed.
        from google import genai

        self._genai = genai
        self._client = genai.Client(api_key=self.settings.gemini_api_key)

    def generate_json(self, *, system_instruction: str, prompt: str, schema: dict) -> ModelCall:
        """One JSON-constrained call, retried on transient failures.

        Retry is not a precaution here: intermittent 5xx responses were observed in
        roughly a third of calls while building this phase. Backoff is bounded so a
        struggling API degrades the demo's speed rather than hanging it.
        """
        from google.genai import types
        from google.genai.errors import ClientError, ServerError

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=schema,
            # Low but not zero: these are factual restatements, not creative writing.
            temperature=0.2,
        )

        started = time.perf_counter()
        last_error: Exception | None = None

        for attempt in range(1, self.settings.gemini_max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self.settings.gemini_model, contents=prompt, config=config
                )
                usage = getattr(response, "usage_metadata", None)
                call = ModelCall(
                    text=response.text or "",
                    model=self.settings.gemini_model,
                    latency_ms=round((time.perf_counter() - started) * 1000, 1),
                    prompt_tokens=getattr(usage, "prompt_token_count", None),
                    output_tokens=getattr(usage, "candidates_token_count", None),
                    attempts=attempt,
                )
                logger.info(
                    "gemini call completed",
                    extra={
                        "model": call.model,
                        "latency_ms": call.latency_ms,
                        "prompt_tokens": call.prompt_tokens,
                        "output_tokens": call.output_tokens,
                        "attempts": call.attempts,
                    },
                )
                return call

            except ClientError as exc:
                status = getattr(exc, "code", None)
                if status in RETRYABLE_CLIENT_STATUSES:
                    # 429 is a 4xx but it is emphatically not a configuration problem:
                    # it means "too fast", and the correct response is to wait. Firing
                    # nine category batches back to back is enough to hit the free tier,
                    # which is exactly how this was found. Backoff starts higher than for
                    # 5xx because a quota window is longer than a blip.
                    last_error = exc
                    if attempt < self.settings.gemini_max_retries:
                        backoff = RATE_LIMIT_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                        logger.warning(
                            "gemini rate limited, backing off",
                            extra={
                                "attempt": attempt,
                                "backoff_seconds": backoff,
                                "status": status,
                            },
                        )
                        time.sleep(backoff)
                        continue
                    raise AgentUnavailableError(
                        f"Gemini rate limit not cleared after "
                        f"{self.settings.gemini_max_retries} attempts. Reduce batch "
                        f"frequency or wait for the quota window to reset.",
                        details={"status": status},
                    ) from exc

                # Any other 4xx -- bad key, retired model, malformed request. Retrying an
                # argument the server has already rejected only wastes the demo's time.
                raise AgentUnavailableError(
                    f"Gemini rejected the request (HTTP {status}). This is a "
                    f"configuration problem, not a transient one: {str(exc)[:300]}"
                ) from exc

            except ServerError as exc:
                last_error = exc
                if attempt < self.settings.gemini_max_retries:
                    backoff = 0.5 * (2 ** (attempt - 1))
                    logger.warning(
                        "gemini transient failure, retrying",
                        extra={
                            "attempt": attempt,
                            "backoff_seconds": backoff,
                            "error": str(exc)[:200],
                        },
                    )
                    time.sleep(backoff)

            except Exception as exc:  # noqa: BLE001 - the SDK's error surface is broad
                last_error = exc
                if attempt < self.settings.gemini_max_retries:
                    time.sleep(0.5 * (2 ** (attempt - 1)))

        raise AgentUnavailableError(
            f"Gemini did not respond after {self.settings.gemini_max_retries} attempts: "
            f"{str(last_error)[:300]}",
            details={"attempts": self.settings.gemini_max_retries},
        )


def parse_json_payload(text: str) -> dict[str, Any]:
    """Parse a model response that is supposed to be JSON.

    Response-schema mode makes bare JSON the norm, but models occasionally wrap output in
    a fenced code block. Stripping that is a known, bounded quirk worth handling; anything
    beyond it is a genuine failure and is raised rather than salvaged.
    """
    import json

    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:] if lines[0].startswith("```") else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    return payload
