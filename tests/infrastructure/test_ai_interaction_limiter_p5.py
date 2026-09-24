from __future__ import annotations

import asyncio

import pytest

from dovideo.application import AiInteractionLimiterUnavailable
from dovideo.infrastructure.ai_interaction_limiter import (
    AiInteractionLimiter,
    AiInteractionRateLimitConfigurationError,
    AiInteractionRateLimitSettings,
    InMemoryAiInteractionLimiter,
)


def test_ai_rate_limit_defaults_and_custom_environment() -> None:
    defaults = AiInteractionRateLimitSettings.from_environment({})
    assert defaults.user_limit == 60
    assert defaults.global_limit == 600
    assert defaults.window_seconds == 60
    assert defaults.enabled is True

    custom = AiInteractionRateLimitSettings.from_environment(
        {
            "DOVIDEO_AI_USER_RATE_LIMIT": "2",
            "DOVIDEO_AI_GLOBAL_RATE_LIMIT": "5",
            "DOVIDEO_AI_RATE_WINDOW_SECONDS": "17",
            "DOVIDEO_AI_RATE_LIMIT_ENABLED": "false",
        }
    )
    assert custom == AiInteractionRateLimitSettings(2, 5, 17, False)


@pytest.mark.parametrize(
    "name,value",
    [
        ("DOVIDEO_AI_USER_RATE_LIMIT", "0"),
        ("DOVIDEO_AI_GLOBAL_RATE_LIMIT", "-1"),
        ("DOVIDEO_AI_RATE_WINDOW_SECONDS", "not-a-number"),
        ("DOVIDEO_AI_RATE_LIMIT_ENABLED", "maybe"),
    ],
)
def test_ai_rate_limit_rejects_invalid_configuration(name: str, value: str) -> None:
    with pytest.raises(AiInteractionRateLimitConfigurationError):
        AiInteractionRateLimitSettings.from_environment({name: value})


@pytest.mark.asyncio
async def test_user_and_global_buckets_are_independent_and_rejections_do_not_consume_other_bucket() -> None:
    now = [100.0]
    limiter = InMemoryAiInteractionLimiter(
        settings=AiInteractionRateLimitSettings(user_limit=2, global_limit=3, window_seconds=10),
        clock=lambda: now[0],
    )

    assert (await limiter.try_acquire(1, "route")).allowed is True
    assert (await limiter.try_acquire(1, "analysis")).allowed is True
    user_rejected = await limiter.try_acquire(1, "follow-up")
    assert user_rejected.allowed is False
    assert user_rejected.reason == "USER_LIMIT"

    # The rejected user request did not consume global capacity.
    assert (await limiter.try_acquire(2, "analysis")).allowed is True
    global_rejected = await limiter.try_acquire(3, "evidence-search")
    assert global_rejected.allowed is False
    assert global_rejected.reason == "GLOBAL_LIMIT"

    now[0] = 111.0
    assert (await limiter.try_acquire(1, "route")).allowed is True
    assert limiter.observation_snapshot()["route:ALLOWED"] == 2


@pytest.mark.asyncio
async def test_concurrent_local_admissions_never_exceed_user_or_global_allowance() -> None:
    limiter = InMemoryAiInteractionLimiter(
        settings=AiInteractionRateLimitSettings(user_limit=5, global_limit=7, window_seconds=60),
        clock=lambda: 1.0,
    )
    decisions = await asyncio.gather(
        *(limiter.try_acquire(9, "analysis") for _ in range(30))
    )
    assert sum(decision.allowed for decision in decisions) == 5
    assert all(
        decision.reason in {"ALLOWED", "USER_LIMIT"}
        for decision in decisions
    )


class _RecordingRedis:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = [1, b"ALLOWED", 60] if result is None else result
        self.error = error
        self.calls = []

    def eval(self, script, number_of_keys, *arguments):
        self.calls.append((script, number_of_keys, arguments))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_redis_limiter_uses_one_atomic_script_and_does_not_put_raw_user_id_in_key() -> None:
    redis = _RecordingRedis()
    limiter = AiInteractionLimiter(
        redis,
        settings=AiInteractionRateLimitSettings(user_limit=2, global_limit=3, window_seconds=60),
    )

    decision = await limiter.try_acquire(12345, "route")

    assert decision.allowed is True
    assert len(redis.calls) == 1
    script, number_of_keys, arguments = redis.calls[0]
    assert number_of_keys == 2
    assert "12345" not in arguments[0]
    assert arguments[1] == "dovideo:ai-interaction:global"
    assert "incr" in script and "expire" in script and "ttl" in script


@pytest.mark.asyncio
async def test_redis_backend_failure_is_fail_closed() -> None:
    limiter = AiInteractionLimiter(
        _RecordingRedis(error=ConnectionError("private redis detail")),
    )

    with pytest.raises(AiInteractionLimiterUnavailable):
        await limiter.try_acquire(1, "analysis")
    assert limiter.observation_snapshot()["analysis:BACKEND_FAILURE"] == 1
