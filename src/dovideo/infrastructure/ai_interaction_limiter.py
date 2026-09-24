"""Redis-backed P5 user/global AI interaction limiting.

The production implementation uses one Lua operation for both dimensions.
The local implementation mirrors the same fixed-window-from-first-use
semantics without pretending to be a distributed production store.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, Mapping

from dovideo.application.ai_interaction import (
    AiInteractionLimitDecision,
    AiInteractionLimiterUnavailable,
)


LOGGER = logging.getLogger("dovideo.ai_interaction")

DEFAULT_AI_USER_RATE_LIMIT = 60
DEFAULT_AI_GLOBAL_RATE_LIMIT = 600
DEFAULT_AI_RATE_WINDOW_SECONDS = 60

AI_INTERACTION_ENDPOINTS = frozenset(
    {"route", "analysis", "follow-up", "evidence-search"}
)

# Both counters are read, checked, incremented, and expired in one Redis
# script.  A rejected user check returns before either counter is mutated;
# likewise a global rejection does not consume the user bucket.
_ATOMIC_ACQUIRE_SCRIPT = """
local user_count = tonumber(redis.call('get', KEYS[1]) or '0')
local global_count = tonumber(redis.call('get', KEYS[2]) or '0')
local window = tonumber(ARGV[3])
local user_ttl = tonumber(redis.call('ttl', KEYS[1]))
local global_ttl = tonumber(redis.call('ttl', KEYS[2]))

if user_count > 0 and user_ttl < 0 then
  redis.call('expire', KEYS[1], window)
  user_ttl = window
end
if global_count > 0 and global_ttl < 0 then
  redis.call('expire', KEYS[2], window)
  global_ttl = window
end

if user_count >= tonumber(ARGV[1]) then
  return {0, 'USER_LIMIT', math.max(1, user_ttl)}
end
if global_count >= tonumber(ARGV[2]) then
  return {0, 'GLOBAL_LIMIT', math.max(1, global_ttl)}
end

local next_user = redis.call('incr', KEYS[1])
local next_global = redis.call('incr', KEYS[2])
if next_user == 1 then
  redis.call('expire', KEYS[1], window)
  user_ttl = window
elseif user_ttl < 1 then
  redis.call('expire', KEYS[1], window)
  user_ttl = window
end
if next_global == 1 then
  redis.call('expire', KEYS[2], window)
  global_ttl = window
elseif global_ttl < 1 then
  redis.call('expire', KEYS[2], window)
  global_ttl = window
end
return {1, 'ALLOWED', math.min(user_ttl, global_ttl)}
"""


class AiInteractionRateLimitConfigurationError(ValueError):
    """Raised for invalid P5 environment configuration."""


@dataclass(frozen=True, slots=True)
class AiInteractionRateLimitSettings:
    """Central P5 policy; values are intentionally conservative defaults."""

    user_limit: int = DEFAULT_AI_USER_RATE_LIMIT
    global_limit: int = DEFAULT_AI_GLOBAL_RATE_LIMIT
    window_seconds: int = DEFAULT_AI_RATE_WINDOW_SECONDS
    enabled: bool = True

    def __post_init__(self) -> None:
        for name in ("user_limit", "global_limit", "window_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AiInteractionRateLimitConfigurationError(
                    f"{name} must be a positive integer"
                )
        if not isinstance(self.enabled, bool):
            raise AiInteractionRateLimitConfigurationError("enabled must be boolean")

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        prefix: str = "DOVIDEO_",
    ) -> "AiInteractionRateLimitSettings":
        values = os.environ if environ is None else environ
        return cls(
            user_limit=_positive_int(
                values,
                f"{prefix}AI_USER_RATE_LIMIT",
                DEFAULT_AI_USER_RATE_LIMIT,
            ),
            global_limit=_positive_int(
                values,
                f"{prefix}AI_GLOBAL_RATE_LIMIT",
                DEFAULT_AI_GLOBAL_RATE_LIMIT,
            ),
            window_seconds=_positive_int(
                values,
                f"{prefix}AI_RATE_WINDOW_SECONDS",
                DEFAULT_AI_RATE_WINDOW_SECONDS,
            ),
            enabled=_boolean(
                values,
                f"{prefix}AI_RATE_LIMIT_ENABLED",
                True,
            ),
        )


class _ObservedLimiter:
    def __init__(self) -> None:
        self._observation_lock = Lock()
        self._observations: Counter[tuple[str, str]] = Counter()

    def _observe(self, endpoint: str, outcome: str) -> None:
        category = endpoint if endpoint in AI_INTERACTION_ENDPOINTS else "other"
        with self._observation_lock:
            self._observations[(category, outcome)] += 1
        # Endpoint and outcome come from bounded constants; no user identity,
        # query, goal, or Redis key is logged.
        LOGGER.info("ai_interaction outcome=%s endpoint=%s", outcome, category)

    def observation_snapshot(self) -> dict[str, int]:
        with self._observation_lock:
            return {
                f"{endpoint}:{outcome}": count
                for (endpoint, outcome), count in self._observations.items()
            }


class AiInteractionLimiter(_ObservedLimiter):
    """Authoritative Redis implementation used by production composition."""

    def __init__(
        self,
        client: Any,
        *,
        settings: AiInteractionRateLimitSettings | None = None,
        prefix: str = "dovideo:ai-interaction",
    ) -> None:
        super().__init__()
        if client is None:
            raise ValueError("Redis client is required for AI interaction limiting")
        self.client = client
        self.settings = settings or AiInteractionRateLimitSettings()
        if not isinstance(prefix, str) or not prefix.strip():
            raise ValueError("AI interaction limiter prefix is required")
        self.prefix = prefix.strip().rstrip(":")

    def _user_key(self, user_id: int) -> str:
        digest = hashlib.sha256(str(user_id).encode("ascii")).hexdigest()
        return f"{self.prefix}:user:{digest}"

    @property
    def global_key(self) -> str:
        return f"{self.prefix}:global"

    async def try_acquire(
        self,
        user_id: int,
        endpoint: str,
    ) -> AiInteractionLimitDecision:
        _validate_user_id(user_id)
        endpoint = _safe_endpoint(endpoint)
        if not self.settings.enabled:
            self._observe(endpoint, "ALLOWED_DISABLED")
            return AiInteractionLimitDecision(True, "ALLOWED_DISABLED")
        try:
            result = await asyncio.to_thread(
                self.client.eval,
                _ATOMIC_ACQUIRE_SCRIPT,
                2,
                self._user_key(user_id),
                self.global_key,
                str(self.settings.user_limit),
                str(self.settings.global_limit),
                str(self.settings.window_seconds),
            )
            decision = _decode_redis_decision(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._observe(endpoint, "BACKEND_FAILURE")
            raise AiInteractionLimiterUnavailable(
                "AI interaction limiter backend unavailable"
            ) from exc
        self._observe(endpoint, decision.reason)
        return decision


class InMemoryAiInteractionLimiter(_ObservedLimiter):
    """Deterministic local/test limiter with the production decision contract."""

    def __init__(
        self,
        *,
        settings: AiInteractionRateLimitSettings | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings or AiInteractionRateLimitSettings()
        self._clock = clock or time.monotonic
        self._lock = asyncio.Lock()
        self._bucket_started: float | None = None
        self._user_counts: dict[int, int] = {}
        self._global_count = 0

    async def try_acquire(
        self,
        user_id: int,
        endpoint: str,
    ) -> AiInteractionLimitDecision:
        _validate_user_id(user_id)
        endpoint = _safe_endpoint(endpoint)
        if not self.settings.enabled:
            self._observe(endpoint, "ALLOWED_DISABLED")
            return AiInteractionLimitDecision(True, "ALLOWED_DISABLED")
        async with self._lock:
            now = float(self._clock())
            if self._bucket_started is None or now >= self._bucket_started + self.settings.window_seconds:
                self._bucket_started = now
                self._user_counts.clear()
                self._global_count = 0
            started = self._bucket_started
            assert started is not None
            remaining = max(1, int(started + self.settings.window_seconds - now))
            user_count = self._user_counts.get(user_id, 0)
            if user_count >= self.settings.user_limit:
                decision = AiInteractionLimitDecision(False, "USER_LIMIT", remaining)
            elif self._global_count >= self.settings.global_limit:
                decision = AiInteractionLimitDecision(False, "GLOBAL_LIMIT", remaining)
            else:
                self._user_counts[user_id] = user_count + 1
                self._global_count += 1
                decision = AiInteractionLimitDecision(True, "ALLOWED", remaining)
        self._observe(endpoint, decision.reason)
        return decision


def _decode_redis_decision(value: Any) -> AiInteractionLimitDecision:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        raise ValueError("AI interaction limiter returned an invalid decision")
    allowed = int(value[0]) == 1
    reason = value[1]
    if isinstance(reason, bytes):
        reason = reason.decode("ascii", errors="strict")
    reason = str(reason)
    retry_after = max(1, int(value[2]))
    return AiInteractionLimitDecision(allowed, reason, retry_after)


def _safe_endpoint(value: str) -> str:
    return value if isinstance(value, str) and value in AI_INTERACTION_ENDPOINTS else "other"


def _validate_user_id(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("authenticated user id must be positive")


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    try:
        value = default if raw is None or not str(raw).strip() else int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise AiInteractionRateLimitConfigurationError(
            f"{name} must be a positive integer"
        ) from exc
    if value <= 0:
        raise AiInteractionRateLimitConfigurationError(
            f"{name} must be a positive integer"
        )
    return value


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name)
    if raw is None or not str(raw).strip():
        return default
    normalized = str(raw).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise AiInteractionRateLimitConfigurationError(
        f"{name} must be a boolean"
    )


__all__ = [
    "AI_INTERACTION_ENDPOINTS",
    "AiInteractionLimiter",
    "AiInteractionRateLimitConfigurationError",
    "AiInteractionRateLimitSettings",
    "InMemoryAiInteractionLimiter",
]
