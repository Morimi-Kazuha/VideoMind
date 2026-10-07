"""Redis-backed P5 user/global AI interaction limiting.

The production implementation uses one Lua operation for both dimensions.
The local implementation mirrors the same continuous token-bucket
semantics without pretending to be a distributed production store.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
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

# Redis supplies one millisecond-resolution time for both buckets. Refill is
# persisted even on rejection, but business tokens are deducted both-or-none.
# Idle expiry is twice the full refill duration, so recreation is always safe.
_ATOMIC_ACQUIRE_SCRIPT = """
local clock = redis.call('TIME')
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local user_capacity = tonumber(ARGV[1])
local global_capacity = tonumber(ARGV[2])
local window_ms = tonumber(ARGV[3]) * 1000
local ttl_ms = window_ms * 2

local function refill(key, capacity)
  local state = redis.call('HMGET', key, 'tokens', 'last_refill_ms')
  if not state[1] and not state[2] then
    return capacity, now_ms
  end
  local tokens = tonumber(state[1])
  local last = tonumber(state[2])
  if not tokens or not last or tokens < 0 then
    error('invalid token bucket state')
  end
  -- A backward Redis clock step must not grant the same elapsed time twice.
  local timestamp = math.max(now_ms, last)
  return math.min(capacity, tokens + (timestamp - last) * capacity / window_ms), timestamp
end

local user_tokens, user_last = refill(KEYS[1], user_capacity)
local global_tokens, global_last = refill(KEYS[2], global_capacity)
local allowed = user_tokens >= 1 and global_tokens >= 1
local reason = 'ALLOWED'
local retry_after = 0
if allowed then
  user_tokens = user_tokens - 1
  global_tokens = global_tokens - 1
else
  reason = user_tokens < 1 and 'USER_LIMIT' or 'GLOBAL_LIMIT'
  local user_wait = user_tokens < 1 and
    ((1 - user_tokens) * window_ms / user_capacity + user_last - now_ms) or 0
  local global_wait = global_tokens < 1 and
    ((1 - global_tokens) * window_ms / global_capacity + global_last - now_ms) or 0
  retry_after = math.max(1, math.ceil(math.max(user_wait, global_wait) / 1000))
end

local function save(key, tokens, last)
  redis.call('HSET', key, 'tokens', string.format('%.17g', tokens), 'last_refill_ms', last)
  redis.call('PEXPIRE', key, ttl_ms)
end
save(KEYS[1], user_tokens, user_last)
save(KEYS[2], global_tokens, global_last)
return {allowed and 1 or 0, reason, retry_after}
"""


class AiInteractionRateLimitConfigurationError(ValueError):
    """Raised for invalid P5 environment configuration."""


@dataclass(frozen=True, slots=True)
class AiInteractionRateLimitSettings:
    """Limits are capacities; window_seconds is the full refill duration."""

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
        # v1 used String counters; v2 Hash buckets coexist without WRONGTYPE.
        self.prefix = prefix.strip().rstrip(":") + ":v2"

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


@dataclass(slots=True)
class _BucketState:
    tokens: float
    last_refill: float


def _refill(state: _BucketState | None, capacity: int, window: int, now: float) -> _BucketState:
    if state is None:
        return _BucketState(float(capacity), now)
    timestamp = max(now, state.last_refill)
    return _BucketState(
        min(capacity, state.tokens + (timestamp - state.last_refill) * capacity / window),
        timestamp,
    )


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
        self._lock = Lock()
        self._users: dict[int, _BucketState] = {}
        self._global: _BucketState | None = None
        self._next_cleanup = 0.0

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
        with self._lock:
            now = float(self._clock())
            window = self.settings.window_seconds
            if now >= self._next_cleanup:
                self._users = {
                    user: state for user, state in self._users.items()
                    if now - state.last_refill < 2 * window
                }
                self._next_cleanup = now + window
            user = _refill(self._users.get(user_id), self.settings.user_limit, window, now)
            global_bucket = _refill(self._global, self.settings.global_limit, window, now)
            if user.tokens < 1 or global_bucket.tokens < 1:
                user_wait = (
                    (1 - user.tokens) * window / self.settings.user_limit + user.last_refill - now
                    if user.tokens < 1 else 0
                )
                global_wait = (
                    (1 - global_bucket.tokens) * window / self.settings.global_limit + global_bucket.last_refill - now
                    if global_bucket.tokens < 1 else 0
                )
                decision = AiInteractionLimitDecision(
                    False, "USER_LIMIT" if user.tokens < 1 else "GLOBAL_LIMIT",
                    max(1, math.ceil(max(user_wait, global_wait))),
                )
            else:
                user.tokens -= 1
                global_bucket.tokens -= 1
                decision = AiInteractionLimitDecision(True, "ALLOWED")
            self._users[user_id] = user
            self._global = global_bucket
        self._observe(endpoint, decision.reason)
        return decision


def _decode_redis_decision(value: Any) -> AiInteractionLimitDecision:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        raise ValueError("AI interaction limiter returned an invalid decision")
    flag = int(value[0])
    if flag not in (0, 1):
        raise ValueError("AI interaction limiter returned an invalid flag")
    allowed = flag == 1
    reason = value[1]
    if isinstance(reason, bytes):
        reason = reason.decode("ascii", errors="strict")
    reason = str(reason)
    if reason not in ({"ALLOWED"} if allowed else {"USER_LIMIT", "GLOBAL_LIMIT"}):
        raise ValueError("AI interaction limiter returned an invalid reason")
    retry_after = None if allowed else max(1, int(value[2]))
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
