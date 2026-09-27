"""Small, dependency-free adapter for the documented Jev System One API.

The adapter is intentionally narrower than a general provider client.  Jev
returns one bounded logical lane choice; VideoMind's application policy remains
the authority that accepts, rejects, or falls back from that choice.  No
provider/model mapping, prompt, video content, or credential crosses the
application routing contract.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol
from urllib.parse import urlsplit

from dovideo.application.model_routing import (
    InvalidRoutingContextError,
    InvalidRoutingSuggestionError,
    MODEL_ROUTING_CONTRACT_VERSION,
    ModelRouteLane,
    RoutingSuggestion,
    TaskRoutingContext,
)

from .http import AsyncJsonPostClient, StdlibAsyncJsonPostClient, post_json, response_parts


JEV_DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
OPENROUTER_DECISIONS_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_JEV_MODEL = "typesafe/jev-1.13"
JEV_QUESTION_KEY = "model_lane"
JEV_MAX_MODEL_LENGTH = 256
JEV_MAX_ENDPOINT_LENGTH = 512
JEV_MAX_RESPONSE_USAGE = 10_000_000
JEV_MAX_RESPONSE_COST_USD = 1_000_000.0
JEV_MAX_ATTEMPTS = 2
JEV_MAX_TIMEOUT_SECONDS = 30.0
JEV_MAX_RETRY_DELAY_SECONDS = 2.0

JEV_LANE_CRITERIA: dict[str, str] = {
    "FAST": (
        "Use for straightforward analysis with limited reasoning or cross-video synthesis."
    ),
    "BALANCED": (
        "Use for ordinary multi-step video analysis with moderate reasoning complexity."
    ),
    "DEEP": (
        "Use for substantial multi-step reasoning, long-range comparison, complex synthesis, "
        "or difficult constraint satisfaction."
    ),
}
JEV_LANE_INSTRUCTIONS = (
    "Choose exactly one analysis lane. Return only a choice answer. "
    "The lane is a logical DOVideo hint, not a provider or model name."
)


class JevTransport(str, Enum):
    """Infrastructure-owned Jev API transport selector."""

    TYPESAFE_DIRECT = "typesafe_direct"
    OPENROUTER = "openrouter"


class JevConfigurationError(ValueError):
    """Jev settings are missing or outside the bounded adapter contract."""


class JevRouterError(RuntimeError):
    """A non-retryable Jev transport or HTTP failure."""


class JevRouterUnavailableError(TimeoutError):
    """A bounded timeout, throttling, or transient Jev failure."""


class JevResponseError(InvalidRoutingSuggestionError):
    """The response did not contain the documented ChoiceAnswer shape."""


class JevRouterObserver(Protocol):
    """Optional bounded observation seam; it receives no raw request/response."""

    def record_jev_routing(
        self,
        *,
        status_code: int | None,
        latency_ms: float,
        gateway: str,
        decision_model: str,
        router_model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        usage_cost_usd: float | None,
        fallback: bool,
    ) -> None:
        ...


@dataclass(frozen=True, slots=True)
class JevRouterSettings:
    """Secret-bearing infrastructure settings for one Jev adapter."""

    endpoint: str = JEV_DEFAULT_ENDPOINT
    model: str = ""
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 2.0
    max_attempts: int = 1
    retry_delay_seconds: float = 0.0
    transport: JevTransport | str = JevTransport.TYPESAFE_DIRECT
    openrouter_api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        transport = _transport(self.transport)
        if transport is JevTransport.OPENROUTER:
            normalized_endpoint = (
                self.endpoint.strip().rstrip("/")
                if isinstance(self.endpoint, str)
                else ""
            )
            if normalized_endpoint not in {
                JEV_DEFAULT_ENDPOINT,
                OPENROUTER_DECISIONS_ENDPOINT,
            }:
                raise JevConfigurationError("OpenRouter Decisions endpoint is fixed")
            endpoint = OPENROUTER_DECISIONS_ENDPOINT
        else:
            endpoint = _endpoint(self.endpoint)
        model = _optional_bounded_text(self.model, "Jev model", JEV_MAX_MODEL_LENGTH)
        if transport is JevTransport.OPENROUTER:
            model = model or OPENROUTER_JEV_MODEL
            if model != OPENROUTER_JEV_MODEL:
                raise JevConfigurationError(
                    "OpenRouter Jev model must use the pinned benchmark identity"
                )
        api_key = None if self.api_key is None else _secret(self.api_key)
        openrouter_api_key = (
            None
            if self.openrouter_api_key is None
            else _secret(self.openrouter_api_key)
        )
        timeout = _finite_positive(self.timeout_seconds, "Jev timeout_seconds")
        if timeout > JEV_MAX_TIMEOUT_SECONDS:
            raise JevConfigurationError("Jev timeout_seconds exceeds the bounded maximum")
        attempts = _positive_int(self.max_attempts, "Jev max_attempts")
        if attempts > JEV_MAX_ATTEMPTS:
            raise JevConfigurationError("Jev max_attempts exceeds the bounded maximum")
        delay = _finite_nonnegative(
            self.retry_delay_seconds,
            "Jev retry_delay_seconds",
        )
        if delay > JEV_MAX_RETRY_DELAY_SECONDS:
            raise JevConfigurationError(
                "Jev retry_delay_seconds exceeds the bounded maximum"
            )
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "openrouter_api_key", openrouter_api_key)
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "max_attempts", attempts)
        object.__setattr__(self, "retry_delay_seconds", delay)
        object.__setattr__(self, "transport", transport)

    def validate_for_use(self) -> None:
        if not self.model:
            raise JevConfigurationError("Jev model is required when model routing is enabled")
        if not self.active_api_key:
            name = (
                "OpenRouter API key"
                if self.transport is JevTransport.OPENROUTER
                else "Jev API key"
            )
            raise JevConfigurationError(
                f"{name} is required when model routing is enabled"
            )

    @property
    def active_api_key(self) -> str | None:
        """Return only the credential selected by the configured transport."""

        if self.transport is JevTransport.OPENROUTER:
            return self.openrouter_api_key
        return self.api_key

    @property
    def gateway(self) -> str:
        """Stable, provider-neutral identity for bounded Jev telemetry."""

        return (
            "OPENROUTER"
            if self.transport is JevTransport.OPENROUTER
            else "TYPESAFE_DIRECT"
        )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        required: bool = False,
    ) -> "JevRouterSettings":
        values = os.environ if environ is None else environ
        transport = _transport(
            _first_value(values, "DOVIDEO_JEV_TRANSPORT")
            or JevTransport.TYPESAFE_DIRECT.value
        )
        if transport is JevTransport.OPENROUTER:
            endpoint = OPENROUTER_DECISIONS_ENDPOINT
            model = _first_value(values, "DOVIDEO_JEV_MODEL") or OPENROUTER_JEV_MODEL
            api_key = None
            openrouter_api_key = _first_value(
                values,
                "DOVIDEO_OPENROUTER_API_KEY",
            )
        else:
            endpoint = (
                _first_value(
                    values,
                    "DOVIDEO_JEV_ENDPOINT",
                    "DOVIDEO_JEV_BASE_URL",
                )
                or JEV_DEFAULT_ENDPOINT
            )
            model = _first_value(values, "DOVIDEO_JEV_MODEL") or ""
            api_key = _first_value(values, "DOVIDEO_JEV_API_KEY")
            openrouter_api_key = None
        selected = cls(
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            transport=transport,
            openrouter_api_key=openrouter_api_key,
            timeout_seconds=_number(
                values,
                "DOVIDEO_JEV_TIMEOUT_SECONDS",
                2.0,
            ),
            max_attempts=_integer(values, "DOVIDEO_JEV_MAX_ATTEMPTS", 1),
            retry_delay_seconds=_number(
                values,
                "DOVIDEO_JEV_RETRY_DELAY_SECONDS",
                0.0,
            ),
        )
        if required:
            selected.validate_for_use()
        return selected

    from_env = from_environment


class JevModelRouter:
    """Implement :class:`ModelRouterPort` over one System One choice call."""

    def __init__(
        self,
        settings: JevRouterSettings,
        *,
        client: AsyncJsonPostClient | object | None = None,
        http_client: AsyncJsonPostClient | object | None = None,
        sleeper: Any | None = None,
        observer: JevRouterObserver | Any | None = None,
    ) -> None:
        if not isinstance(settings, JevRouterSettings):
            raise TypeError("settings must be JevRouterSettings")
        self.settings = settings
        self._client = client if client is not None else http_client
        self._client = self._client or StdlibAsyncJsonPostClient()
        self._sleeper = sleeper or asyncio.sleep
        self._observer = observer

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        client: AsyncJsonPostClient | object | None = None,
        http_client: AsyncJsonPostClient | object | None = None,
        sleeper: Any | None = None,
        observer: JevRouterObserver | Any | None = None,
    ) -> "JevModelRouter":
        return cls(
            JevRouterSettings.from_environment(environ, required=True),
            client=client,
            http_client=http_client,
            sleeper=sleeper,
            observer=observer,
        )

    @staticmethod
    def request_payload(
        context: TaskRoutingContext,
        *,
        model: str,
    ) -> dict[str, Any]:
        """Project only bounded routing signals into the official request."""

        if not isinstance(context, TaskRoutingContext):
            raise InvalidRoutingContextError("routing context is invalid")
        if not model or not isinstance(model, str):
            raise JevConfigurationError("Jev model is required")
        state: dict[str, Any] = {
            "userGoal": context.user_goal,
            "mode": context.mode.value,
            "mediaDurationMs": context.media_duration_ms,
            "segmentCount": context.segment_count,
            "chunkCount": context.chunk_count,
            "asrAvailable": context.asr_available,
            "ocrAvailable": context.ocr_available,
        }
        return {
            "model": model,
            "state": state,
            "questions": {
                JEV_QUESTION_KEY: {
                    "type": "choice",
                    "instructions": JEV_LANE_INSTRUCTIONS,
                    "criteria": dict(JEV_LANE_CRITERIA),
                }
            },
        }

    build_request = request_payload

    @staticmethod
    def parse_response(body: Any) -> RoutingSuggestion:
        """Strictly decode the documented answer without trusting probabilities."""

        if not isinstance(body, Mapping):
            raise JevResponseError("Jev response body is invalid")
        answers = body.get("answers")
        if not isinstance(answers, Mapping):
            raise JevResponseError("Jev response answers are missing")
        answer = answers.get(JEV_QUESTION_KEY)
        if not isinstance(answer, Mapping):
            raise JevResponseError("Jev model_lane answer is missing")
        if answer.get("type") != "choice":
            raise JevResponseError("Jev model_lane answer type is invalid")
        if "choice" not in answer or "confidence" not in answer:
            raise JevResponseError("Jev choice answer is incomplete")
        try:
            return RoutingSuggestion(
                suggestedLane=answer["choice"],
                confidence=answer["confidence"],
                routingContractVersion=MODEL_ROUTING_CONTRACT_VERSION,
            )
        except Exception as error:
            raise JevResponseError("Jev choice answer is outside the routing contract") from error

    async def route(self, context: TaskRoutingContext) -> RoutingSuggestion:
        if not isinstance(context, TaskRoutingContext):
            try:
                context = TaskRoutingContext.model_validate(context)
            except Exception as error:
                raise InvalidRoutingContextError("routing context is invalid") from error
        if context.mode.value == "AUTO":  # defensive boundary for future enum aliases
            raise InvalidRoutingContextError("AUTO cannot enter Jev model routing")
        self.settings.validate_for_use()
        payload = self.request_payload(context, model=self.settings.model)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.active_api_key}",
        }
        started = time.perf_counter()
        status_code: int | None = None
        fallback = True
        try:
            for attempt in range(self.settings.max_attempts):
                try:
                    response = await asyncio.wait_for(
                        post_json(
                            self._client,
                            self.settings.endpoint,
                            headers=headers,
                            payload=payload,
                            timeout=self.settings.timeout_seconds,
                        ),
                        timeout=self.settings.timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except (TimeoutError, OSError) as error:
                    if attempt + 1 >= self.settings.max_attempts:
                        raise JevRouterUnavailableError("Jev router unavailable") from error
                    await self._sleep_before_retry(attempt)
                    continue

                try:
                    status_code, body = response_parts(response)
                except Exception as error:
                    raise JevResponseError("Jev response envelope is invalid") from error
                if status_code in {408, 429} or status_code >= 500:
                    if attempt + 1 >= self.settings.max_attempts:
                        raise JevRouterUnavailableError("Jev router unavailable")
                    await self._sleep_before_retry(attempt)
                    continue
                if status_code in {401, 403}:
                    raise JevRouterError("Jev router authentication failed")
                if status_code < 200 or status_code >= 300:
                    raise JevRouterError("Jev router rejected the request")

                suggestion = self.parse_response(body)
                fallback = False
                self._observe(
                    status_code=status_code,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    body=body,
                    fallback=fallback,
                )
                return suggestion
            raise JevRouterUnavailableError("Jev router unavailable")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._observe(
                status_code=status_code,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                body=None,
                fallback=fallback,
            )
            raise

    route_once = route

    async def _sleep_before_retry(self, attempt: int) -> None:
        delay = self.settings.retry_delay_seconds * (2**attempt)
        value = self._sleeper(delay)
        if hasattr(value, "__await__"):
            await value

    def _observe(
        self,
        *,
        status_code: int | None,
        latency_ms: float,
        body: Any,
        fallback: bool,
    ) -> None:
        observer = self._observer
        record = getattr(observer, "record_jev_routing", None)
        if not callable(record):
            return
        model = body.get("model") if isinstance(body, Mapping) else None
        model = _optional_bounded_text(model, "router model", JEV_MAX_MODEL_LENGTH)
        usage = body.get("usage") if isinstance(body, Mapping) else None
        input_tokens = _usage_int(usage, "input_tokens", "inputTokens")
        output_tokens = _usage_int(usage, "output_tokens", "outputTokens")
        try:
            record(
                status_code=status_code,
                latency_ms=max(0.0, float(latency_ms)),
                gateway=self.settings.gateway,
                decision_model=self.settings.model,
                router_model=model or None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usage_cost_usd=_usage_cost(usage),
                fallback=bool(fallback),
            )
        except Exception:
            # Telemetry is not a routing correctness dependency.
            return


JevAdapter = JevModelRouter
JevSystemOneModelRouter = JevModelRouter
TypeSafeModelRouter = JevModelRouter
JevModelRouterConfig = JevRouterSettings
JevConfiguration = JevRouterSettings


def _endpoint(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JevConfigurationError("Jev endpoint is required")
    normalized = value.strip().rstrip("/")
    if len(normalized) > JEV_MAX_ENDPOINT_LENGTH:
        raise JevConfigurationError("Jev endpoint exceeds its bound")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise JevConfigurationError("Jev endpoint must be HTTP(S)")
    return normalized


def _transport(value: Any) -> JevTransport:
    if isinstance(value, JevTransport):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold().replace("-", "_")
        try:
            return JevTransport(normalized)
        except ValueError:
            pass
    raise JevConfigurationError("Jev transport must be typesafe_direct or openrouter")


def _secret(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JevConfigurationError("Jev API key must be nonblank")
    return value.strip()


def _optional_bounded_text(value: Any, name: str, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise JevConfigurationError(f"{name} must be text")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise JevConfigurationError(f"{name} exceeds its bound")
    return normalized


def _finite_positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevConfigurationError(f"{name} must be finite and positive")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise JevConfigurationError(f"{name} must be finite and positive")
    return normalized


def _finite_nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevConfigurationError(f"{name} must be finite and non-negative")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise JevConfigurationError(f"{name} must be finite and non-negative")
    return normalized


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise JevConfigurationError(f"{name} must be a positive integer")
    return value


def _first_value(values: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        raw = values.get(name)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return None


def _number(values: Mapping[str, str], name: str, default: float) -> float:
    raw = _first_value(values, name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError, OverflowError) as error:
        raise JevConfigurationError(f"{name} must be numeric") from error


def _integer(values: Mapping[str, str], name: str, default: int) -> int:
    raw = _first_value(values, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError) as error:
        raise JevConfigurationError(f"{name} must be an integer") from error


def _usage_int(value: Any, *names: str) -> int | None:
    if not isinstance(value, Mapping):
        return None
    raw = next((value.get(name) for name in names if name in value), None)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if raw < 0 or raw > JEV_MAX_RESPONSE_USAGE:
        return None
    return raw


def _usage_cost(value: Any) -> float | None:
    if not isinstance(value, Mapping):
        return None
    raw = value.get("cost")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    normalized = float(raw)
    if (
        not math.isfinite(normalized)
        or normalized < 0
        or normalized > JEV_MAX_RESPONSE_COST_USD
    ):
        return None
    return normalized


__all__ = [
    "JEV_DEFAULT_ENDPOINT",
    "JEV_LANE_CRITERIA",
    "JEV_LANE_INSTRUCTIONS",
    "JEV_QUESTION_KEY",
    "JEV_MAX_RESPONSE_COST_USD",
    "OPENROUTER_DECISIONS_ENDPOINT",
    "OPENROUTER_JEV_MODEL",
    "JevAdapter",
    "JevConfiguration",
    "JevConfigurationError",
    "JevModelRouter",
    "JevModelRouterConfig",
    "JevSystemOneModelRouter",
    "JevResponseError",
    "JevRouterError",
    "JevRouterObserver",
    "JevRouterSettings",
    "JevRouterUnavailableError",
    "JevTransport",
    "TypeSafeModelRouter",
]
