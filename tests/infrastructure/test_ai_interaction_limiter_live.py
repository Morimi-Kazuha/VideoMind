"""Opt-in tests of the actual token-bucket Lua against standalone Redis.

Use scripts/run_ai_limiter_live_tests.py; every key is in a UUID test namespace.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from uuid import uuid4

import pytest
from redis import Redis

from dovideo.infrastructure.ai_interaction_limiter import AiInteractionLimiter, AiInteractionRateLimitSettings

pytestmark = pytest.mark.skipif(
    os.environ.get("DOVIDEO_AI_LIMITER_LIVE") != "1", reason="explicit real Redis opt-in required",
)


@pytest.fixture
def live():
    url = os.environ["DOVIDEO_REDIS_URL"]
    first = Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3)
    second = Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3)
    prefix = "dovideo:test:ai-interaction:" + uuid4().hex
    first.ping()  # An explicitly requested live run must fail, never silently skip.
    try:
        yield first, second, prefix
    finally:
        for key in first.scan_iter(match=prefix + ":*"):
            first.delete(key)
        first.close()
        second.close()


def _tokens(client, key):
    return float(client.hget(key, "tokens"))


def _seed(client, key, tokens, elapsed_ms=0):
    # Seed only test state, using the same server time as the production script.
    client.eval("""
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('HSET', KEYS[1], 'tokens', ARGV[1], 'last_refill_ms', now - tonumber(ARGV[2]))
redis.call('PEXPIRE', KEYS[1], 10000)
""", 1, key, tokens, elapsed_ms)


@pytest.mark.asyncio
async def test_live_first_burst_and_real_time_partial_refill(live):
    client, _, prefix = live
    limiter = AiInteractionLimiter(client, prefix=prefix, settings=AiInteractionRateLimitSettings(4, 40, 1))
    assert (await limiter.try_acquire(1, "route")).allowed
    assert 3 <= _tokens(client, limiter._user_key(1)) < 3.1
    for _ in range(3):
        assert (await limiter.try_acquire(1, "route")).allowed
    rejected = await limiter.try_acquire(1, "route")
    assert (rejected.reason, rejected.retry_after_seconds) == ("USER_LIMIT", 1)
    await asyncio.sleep(0.30)
    assert (await limiter.try_acquire(1, "route")).allowed
    assert not (await limiter.try_acquire(1, "route")).allowed


@pytest.mark.asyncio
async def test_live_full_refill_is_clamped_and_no_old_window_reset(live):
    client, _, prefix = live
    limiter = AiInteractionLimiter(client, prefix=prefix, settings=AiInteractionRateLimitSettings(4, 100, 10))
    _seed(client, limiter._user_key(1), 0, 100000)
    for _ in range(4):
        assert (await limiter.try_acquire(1, "analysis")).allowed
    assert not (await limiter.try_acquire(1, "analysis")).allowed
    # Deplete shortly before what used to be a 10-second boundary. Crossing
    # that boundary by 20 ms can only refill .008 tokens, not reset to four.
    _seed(client, limiter._user_key(2), 0, 9990)
    for _ in range(3):
        assert (await limiter.try_acquire(2, "analysis")).allowed
    await asyncio.sleep(0.03)
    assert (await limiter.try_acquire(2, "analysis")).allowed
    assert not (await limiter.try_acquire(2, "analysis")).allowed


@pytest.mark.asyncio
async def test_live_user_and_global_rejections_have_no_partial_deduction(live):
    client, _, prefix = live
    limiter = AiInteractionLimiter(client, prefix=prefix, settings=AiInteractionRateLimitSettings(2, 3, 3600))
    assert (await limiter.try_acquire(1, "route")).allowed
    assert (await limiter.try_acquire(1, "route")).allowed
    before = _tokens(client, limiter.global_key)
    assert (await limiter.try_acquire(1, "route")).reason == "USER_LIMIT"
    assert _tokens(client, limiter.global_key) >= before
    assert (await limiter.try_acquire(2, "route")).allowed
    for _ in range(3):
        assert (await limiter.try_acquire(3, "route")).reason == "GLOBAL_LIMIT"
    assert _tokens(client, limiter._user_key(3)) == 2
    assert 1 <= _tokens(client, limiter._user_key(2)) < 1.01


@pytest.mark.asyncio
@pytest.mark.parametrize("same_user", [True, False])
async def test_live_concurrent_admissions_across_instances(live, same_user):
    first, second, prefix = live
    policy = AiInteractionRateLimitSettings(10, 17, 3600)
    limiters = [AiInteractionLimiter(c, prefix=prefix, settings=policy) for c in (first, second)]
    start = time.monotonic()
    decisions = await asyncio.gather(*(
        limiters[i % 2].try_acquire(1 if same_user else i + 1, "analysis") for i in range(80)
    ))
    assert time.monotonic() - start < 60  # No full request token can refill.
    allowed = sum(d.allowed for d in decisions)
    assert allowed == (10 if same_user else 17)
    remaining = _tokens(first, limiters[0].global_key)
    assert 17 - allowed <= remaining < 17 - allowed + 1
    if not same_user:
        for i, decision in enumerate(decisions):
            tokens = _tokens(first, limiters[0]._user_key(i + 1))
            assert tokens == (9 if decision.allowed else 10)


@pytest.mark.asyncio
async def test_live_ttl_expiration_and_active_denials_do_not_reset_bucket(live):
    client, _, prefix = live
    limiter = AiInteractionLimiter(client, prefix=prefix, settings=AiInteractionRateLimitSettings(1, 1, 1))
    assert (await limiter.try_acquire(1, "route")).allowed
    for key in (limiter._user_key(1), limiter.global_key):
        assert 1800 <= client.pttl(key) <= 2000
    # Rejections renew safe idle TTL without restoring spent tokens.
    for _ in range(3):
        assert not (await limiter.try_acquire(1, "route")).allowed
    await asyncio.sleep(2.15)
    assert not client.exists(limiter._user_key(1), limiter.global_key)
    assert (await limiter.try_acquire(1, "route")).allowed
    assert not (await limiter.try_acquire(1, "route")).allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("user_tokens,global_tokens,reason,wait", [
    (0, 10, "USER_LIMIT", 5), (2, 0, "GLOBAL_LIMIT", 1),
    (0, 0, "USER_LIMIT", 5), (0.99, 10, "USER_LIMIT", 1),
])
async def test_live_retry_after_uses_missing_tokens(live, user_tokens, global_tokens, reason, wait):
    client, _, prefix = live
    limiter = AiInteractionLimiter(client, prefix=prefix, settings=AiInteractionRateLimitSettings(2, 10, 10))
    _seed(client, limiter._user_key(1), user_tokens)
    _seed(client, limiter.global_key, global_tokens)
    decision = await limiter.try_acquire(1, "route")
    assert (decision.allowed, decision.reason, decision.retry_after_seconds) == (False, reason, wait)


@pytest.mark.asyncio
async def test_live_both_shortages_wait_for_slower_global_bucket(live):
    client, _, prefix = live
    limiter = AiInteractionLimiter(client, prefix=prefix, settings=AiInteractionRateLimitSettings(10, 2, 10))
    _seed(client, limiter._user_key(1), 0)
    _seed(client, limiter.global_key, 0)
    decision = await limiter.try_acquire(1, "route")
    assert (decision.reason, decision.retry_after_seconds) == ("USER_LIMIT", 5)


@pytest.mark.asyncio
async def test_live_old_string_keys_coexist_with_stable_hashed_v2_hashes(live):
    client, second, prefix = live
    user_id = 987654321
    digest = hashlib.sha256(str(user_id).encode("ascii")).hexdigest()
    old_user, old_global = f"{prefix}:user:{digest}", f"{prefix}:global"
    client.set(old_user, "60", ex=60)
    client.set(old_global, "600", ex=60)
    first = AiInteractionLimiter(client, prefix=prefix)
    other = AiInteractionLimiter(second, prefix=prefix)
    assert first._user_key(user_id) == other._user_key(user_id) == f"{prefix}:v2:user:{digest}"
    assert str(user_id) not in first._user_key(user_id)
    assert (await first.try_acquire(user_id, "analysis")).allowed
    assert client.type(first._user_key(user_id)) == b"hash"
    assert client.type(first.global_key) == b"hash"
    assert client.get(old_user) == b"60" and client.get(old_global) == b"600"
    assert _tokens(client, first._user_key(user_id)) == 59
    assert _tokens(client, first.global_key) == 599


@pytest.mark.asyncio
async def test_live_strict_fractional_boundary_and_backward_time(live):
    client, _, prefix = live
    limiter = AiInteractionLimiter(client, prefix=prefix, settings=AiInteractionRateLimitSettings(1, 10, 10))
    # A future saved timestamp freezes refill, modeling a backward TIME step
    # and allowing an exact boundary check without host timing assumptions.
    _seed(client, limiter._user_key(1), 1 - 1e-9, -1000)
    decision = await limiter.try_acquire(1, "route")
    assert not decision.allowed and decision.reason == "USER_LIMIT"
    assert _tokens(client, limiter.global_key) == 10
    last = int(client.hget(limiter._user_key(1), "last_refill_ms"))
    assert not (await limiter.try_acquire(1, "route")).allowed
    assert int(client.hget(limiter._user_key(1), "last_refill_ms")) == last
    client.hset(limiter._user_key(1), "tokens", "1")
    assert (await limiter.try_acquire(1, "route")).allowed
    assert _tokens(client, limiter._user_key(1)) == 0
    assert _tokens(client, limiter.global_key) == 9


@pytest.mark.asyncio
async def test_live_bad_v2_state_fails_closed_before_either_write(live):
    from dovideo.application.ai_interaction import AiInteractionLimiterUnavailable

    client, _, prefix = live
    limiter = AiInteractionLimiter(client, prefix=prefix)
    assert (await limiter.try_acquire(1, "route")).allowed
    before = client.hgetall(limiter.global_key)
    client.set(limiter._user_key(2), "unexpected v2 String", ex=60)
    with pytest.raises(AiInteractionLimiterUnavailable):
        await limiter.try_acquire(2, "analysis")
    assert client.hgetall(limiter.global_key) == before
    assert limiter.observation_snapshot()["analysis:BACKEND_FAILURE"] == 1
