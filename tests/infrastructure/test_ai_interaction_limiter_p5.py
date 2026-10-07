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
    assert arguments[0] == limiter._user_key(12345)
    assert arguments[1] == "dovideo:ai-interaction:v2:global"
    assert "'TIME'" in script and "'HMGET'" in script and "'PEXPIRE'" in script
    assert "'incr'" not in script.lower()


@pytest.mark.asyncio
async def test_redis_backend_failure_is_fail_closed() -> None:
    limiter = AiInteractionLimiter(
        _RecordingRedis(error=ConnectionError("private redis detail")),
    )

    with pytest.raises(AiInteractionLimiterUnavailable):
        await limiter.try_acquire(1, "analysis")
    assert limiter.observation_snapshot()["analysis:BACKEND_FAILURE"] == 1


@pytest.mark.asyncio
async def test_burst_partial_refill_full_refill_and_capacity_clamp() -> None:
    now = [0.0]
    limiter = InMemoryAiInteractionLimiter(
        settings=AiInteractionRateLimitSettings(4, 40, 10), clock=lambda: now[0],
    )
    for _ in range(4):
        assert (await limiter.try_acquire(1, "route")).allowed
    rejected = await limiter.try_acquire(1, "route")
    assert (rejected.reason, rejected.retry_after_seconds) == ("USER_LIMIT", 3)
    now[0] = 1.25
    rejected = await limiter.try_acquire(1, "route")
    assert rejected.retry_after_seconds == 2
    now[0] = 2.5
    assert (await limiter.try_acquire(1, "route")).allowed
    assert not (await limiter.try_acquire(1, "route")).allowed
    # A long idle period restores capacity, never more than capacity.
    now[0] = 12.5
    assert all([(await limiter.try_acquire(1, "route")).allowed for _ in range(4)])
    assert not (await limiter.try_acquire(1, "route")).allowed
    now[0] = 1000
    assert all([(await limiter.try_acquire(1, "route")).allowed for _ in range(4)])
    assert not (await limiter.try_acquire(1, "route")).allowed


@pytest.mark.asyncio
async def test_old_window_boundary_does_not_reset_tokens() -> None:
    now = [0.0]
    limiter = InMemoryAiInteractionLimiter(
        settings=AiInteractionRateLimitSettings(4, 100, 10), clock=lambda: now[0],
    )
    assert (await limiter.try_acquire(1, "route")).allowed
    now[0] = 9.99
    assert all([(await limiter.try_acquire(1, "route")).allowed for _ in range(4)])
    now[0] = 10.01
    assert not (await limiter.try_acquire(1, "route")).allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("user_capacity,global_capacity,reason,wait", [
    (1, 10, "USER_LIMIT", 10), (10, 1, "GLOBAL_LIMIT", 10),
    (20, 20, "USER_LIMIT", 1),
])
async def test_retry_wait_uses_both_shortages_and_rounds_up(
    user_capacity, global_capacity, reason, wait,
) -> None:
    now = [0.0]
    limiter = InMemoryAiInteractionLimiter(
        settings=AiInteractionRateLimitSettings(user_capacity, global_capacity, 10),
        clock=lambda: now[0],
    )
    for _ in range(min(user_capacity, global_capacity)):
        assert (await limiter.try_acquire(1, "analysis")).allowed
    rejected = await limiter.try_acquire(1, "analysis")
    assert (rejected.reason, rejected.retry_after_seconds) == (reason, wait)
    now[0] += wait
    assert (await limiter.try_acquire(1, "analysis")).allowed


@pytest.mark.asyncio
async def test_both_shortages_wait_for_global_even_with_user_reason() -> None:
    now = [0.0]
    limiter = InMemoryAiInteractionLimiter(
        settings=AiInteractionRateLimitSettings(1, 2, 10), clock=lambda: now[0],
    )
    assert (await limiter.try_acquire(1, "route")).allowed
    now[0] = 6.0
    assert (await limiter.try_acquire(2, "route")).allowed
    assert (await limiter.try_acquire(3, "route")).allowed
    # User needs four seconds, global needs five: retain user reason but wait
    # until both can satisfy the request, without synthesizing internal state.
    rejected = await limiter.try_acquire(1, "route")
    assert (rejected.reason, rejected.retry_after_seconds) == ("USER_LIMIT", 5)
    now[0] = 11.0
    assert (await limiter.try_acquire(1, "route")).allowed


@pytest.mark.asyncio
async def test_user_refill_times_are_independent_and_idle_state_is_safe_to_prune() -> None:
    now = [0.0]
    limiter = InMemoryAiInteractionLimiter(
        settings=AiInteractionRateLimitSettings(2, 100, 10), clock=lambda: now[0],
    )
    assert (await limiter.try_acquire(1, "route")).allowed
    now[0] = 9.99
    assert (await limiter.try_acquire(2, "route")).allowed
    assert (await limiter.try_acquire(2, "route")).allowed
    now[0] = 10.01
    assert not (await limiter.try_acquire(2, "route")).allowed
    assert (await limiter.try_acquire(1, "route")).allowed
    now[0] = 40.0
    assert (await limiter.try_acquire(3, "route")).allowed
    assert 1 not in limiter._users and 2 not in limiter._users
    assert (await limiter.try_acquire(2, "route")).allowed
    assert (await limiter.try_acquire(2, "route")).allowed
    assert not (await limiter.try_acquire(2, "route")).allowed


@pytest.mark.asyncio
async def test_global_rejection_preserves_user_and_user_rejection_preserves_global() -> None:
    now = [0.0]
    limiter = InMemoryAiInteractionLimiter(
        settings=AiInteractionRateLimitSettings(2, 3, 10), clock=lambda: now[0],
    )
    for _ in range(2):
        assert (await limiter.try_acquire(1, "analysis")).allowed
    assert (limiter._users[1].tokens, limiter._global.tokens) == (0, 1)
    assert (await limiter.try_acquire(1, "route")).reason == "USER_LIMIT"
    assert limiter._global.tokens == 1
    assert (await limiter.try_acquire(2, "route")).allowed
    for _ in range(5):
        assert (await limiter.try_acquire(3, "route")).reason == "GLOBAL_LIMIT"
    assert limiter._users[3].tokens == 2
    now[0] = 10 / 3 + 0.000001
    assert (await limiter.try_acquire(3, "route")).allowed
    assert limiter._users[3].tokens == 1


@pytest.mark.asyncio
async def test_local_multiuser_concurrency_is_bounded_by_shared_global_bucket() -> None:
    limiter = InMemoryAiInteractionLimiter(
        settings=AiInteractionRateLimitSettings(5, 7, 60), clock=lambda: 1.0,
    )
    decisions = await asyncio.gather(*(limiter.try_acquire(i + 1, "route") for i in range(40)))
    assert sum(d.allowed for d in decisions) == 7
    assert all(d.reason == "GLOBAL_LIMIT" for d in decisions[7:])


@pytest.mark.asyncio
async def test_disabled_adapters_allow_without_redis_or_state_mutation() -> None:
    settings = AiInteractionRateLimitSettings(1, 1, 60, False)
    redis = _RecordingRedis(error=ConnectionError("must not call"))
    for limiter in (AiInteractionLimiter(redis, settings=settings), InMemoryAiInteractionLimiter(settings=settings)):
        for _ in range(3):
            assert (await limiter.try_acquire(1, "analysis")).reason == "ALLOWED_DISABLED"
        assert limiter.observation_snapshot() == {"analysis:ALLOWED_DISABLED": 3}
    assert redis.calls == []


@pytest.mark.asyncio
async def test_refill_boundary_and_backward_clock_do_not_overissue() -> None:
    now = [10.0]
    limiter = InMemoryAiInteractionLimiter(
        settings=AiInteractionRateLimitSettings(4, 40, 10), clock=lambda: now[0],
    )
    for _ in range(4):
        await limiter.try_acquire(1, "route")
    now[0] = 12.5 - 0.000001
    assert not (await limiter.try_acquire(1, "route")).allowed
    now[0] = 12.5 + 0.000001
    assert (await limiter.try_acquire(1, "route")).allowed
    now[0] = 10
    assert not (await limiter.try_acquire(1, "route")).allowed
    now[0] = 12.5 + 0.000001
    assert not (await limiter.try_acquire(1, "route")).allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [None, [], [2, "ALLOWED", 0], [1, "private outcome", 0], [0, "ALLOWED", 1]])
async def test_malformed_backend_reply_fails_closed(reply) -> None:
    redis = _RecordingRedis()
    redis.result = reply
    limiter = AiInteractionLimiter(redis)
    with pytest.raises(AiInteractionLimiterUnavailable):
        await limiter.try_acquire(1, "analysis")
    assert limiter.observation_snapshot() == {"analysis:BACKEND_FAILURE": 1}
