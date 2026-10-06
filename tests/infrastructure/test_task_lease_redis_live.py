"""Opt-in real Redis Lua/TTL lease regressions; UUID-scoped keys only."""
import asyncio
import os
import time
from uuid import uuid4

import pytest
from redis import Redis

from dovideo.application.task_lease import TaskLeaseKeeper
from dovideo.application.value_objects import TaskKey
from dovideo.infrastructure.redis import RedisTaskLock

pytestmark = pytest.mark.skipif(os.environ.get("DOVIDEO_TASK_LEASE_LIVE") != "1",
                               reason="explicit Redis lease integration opt-in required")


@pytest.fixture
def live():
    client = Redis.from_url(os.environ["DOVIDEO_REDIS_URL"], decode_responses=True,
                            socket_timeout=2, socket_connect_timeout=2)
    client.ping()
    key = TaskKey(987654, "lease proof")
    lock = RedisTaskLock(client, ttl_ms=300, prefix=f"test:analysis-lease:{uuid4().hex}")
    try:
        yield client, key, lock
    finally:
        client.delete(lock.redis_key(key))
        client.close()


@pytest.mark.asyncio
async def test_live_c_d_old_owner_cannot_refresh_or_release_new_owner(live):
    client, key, lock = live
    token_a = await lock.acquire(key)
    assert token_a is not None
    await asyncio.sleep(0.4)
    token_b = await lock.acquire(key)
    assert token_b is not None and token_b != token_a
    client.pexpire(lock.redis_key(key), 5000)
    before = client.pttl(lock.redis_key(key))
    assert await lock.refresh(key, token_a) is False
    after = client.pttl(lock.redis_key(key))
    assert 4000 < after <= before  # Would be ~300ms if old refresh changed TTL.
    await lock.release(key, token_a)
    assert client.get(lock.redis_key(key)) == token_b
    assert client.pttl(lock.redis_key(key)) > 4000
    assert await lock.refresh(key, token_b) is True
    assert 0 < client.pttl(lock.redis_key(key)) <= 300


@pytest.mark.asyncio
async def test_live_a_b_renewed_long_work_excludes_duplicate_then_stops_and_expires(live):
    client, key, lock = live
    acquired_at = time.monotonic()
    token = await lock.acquire(key)
    keeper = TaskLeaseKeeper(lock, key, token, acquired_at=acquired_at)
    entered = asyncio.Event()
    async def work():
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(keeper.run(work))
    try:
        await entered.wait()
        await asyncio.sleep(0.85)
        assert client.get(lock.redis_key(key)) == token
        assert await lock.acquire(key) is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    await asyncio.sleep(0.4)
    replacement = await lock.acquire(key)
    assert replacement is not None and replacement != token


def test_live_default_lease_contract_remains_fifteen_minutes(live):
    client, _, _ = live
    assert RedisTaskLock(client).lease_seconds == 900
