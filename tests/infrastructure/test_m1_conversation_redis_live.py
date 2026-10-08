"""Opt-in real Redis Lua proofs; clean only UUID-scoped media keys."""
import os
from dataclasses import replace
from uuid import uuid4
import pytest
from redis import Redis

from dovideo.application.conversation_memory import (
    ConversationIdentity, ConversationState, ConversationMemoryService, RollingSummary,
    MEMORY_TTL_SECONDS, MemoryConflict,
)
from dovideo.infrastructure.conversation_memory import RedisConversationMemoryStore, memory_key

pytestmark = pytest.mark.skipif(os.environ.get("DOVIDEO_MEMORY_REDIS_LIVE") != "1",
                               reason="explicit M1 Redis integration opt-in required")


@pytest.fixture
def live():
    client = Redis.from_url(os.environ["DOVIDEO_REDIS_URL"], decode_responses=True,
                           socket_timeout=2, socket_connect_timeout=2)
    client.ping()
    identity = ConversationIdentity(7, uuid4().int >> 96, "a"*64, "GENERAL", str(uuid4()), "r1")
    store = RedisConversationMemoryStore(client)
    try: yield client, store, identity
    finally:
        keys = list(client.scan_iter(match=f"conversation:{{{identity.media_id}}}:*"))
        if keys: client.delete(*keys)
        client.close()


@pytest.mark.asyncio
async def test_atomic_append_ttl_registry_and_same_session_lease(live):
    client, store, identity = live
    assert await store.acquire(identity, "owner")
    assert not await store.acquire(identity, "other")
    service = ConversationMemoryService(store)
    state = await service.save_verified(identity, ConversationState(), "owner", str(uuid4()), "question", "verified answer")
    assert state.version == 1 and len((await store.load(identity)).turns) == 1
    assert MEMORY_TTL_SECONDS-5 <= client.ttl(memory_key(identity)) <= MEMORY_TTL_SECONDS
    assert MEMORY_TTL_SECONDS-5 <= client.ttl(store._registry(identity.media_id)) <= MEMORY_TTL_SECONDS
    await store.release(identity, "other")
    assert not await store.acquire(identity, "other")
    await store.release(identity, "owner")
    assert await store.acquire(identity, "other")


@pytest.mark.asyncio
async def test_stale_summary_and_expired_owner_cannot_commit(live):
    client, store, identity = live
    assert await store.acquire(identity, "owner")
    service = ConversationMemoryService(store)
    state = await service.save_verified(identity, ConversationState(), "owner", str(uuid4()), "q", "a")
    new_summary = RollingSummary(topics=["Redis"], entities=[], key_points=[], unresolved_questions=[], summary_text="新的摘要")
    updated = state.model_copy(update={"version": state.version+1, "summary": new_summary})
    assert await store.commit(identity, state.version, updated, "owner")
    assert not await store.commit(identity, state.version, updated.model_copy(update={"summary": None}), "owner")
    assert (await store.load(identity)).summary == new_summary
    client.delete(memory_key(identity)+":lease")
    assert await store.acquire(identity, "new-owner")
    current = await store.load(identity)
    assert not await store.commit(identity, current.version, current.model_copy(update={"version": current.version+1}), "owner")


@pytest.mark.asyncio
async def test_delete_registry_all_revisions_and_prevents_resurrection(live):
    client, store, identity = live
    service = ConversationMemoryService(store)
    for i, revision in enumerate(["r1", "r2"]):
        other = replace(identity, source_revision=revision)
        assert await store.acquire(other, "owner")
        await service.save_verified(other, ConversationState(), "owner", str(uuid4()), "q", "a")
    await store.delete_media(identity.media_id)
    assert client.get(memory_key(identity)) is None
    assert client.get(memory_key(replace(identity, source_revision="r2"))) is None
    assert client.get(store._deleted(identity.media_id)) == "1"
    assert not await store.commit(identity, 0, ConversationState(version=1), "owner")
    with pytest.raises(MemoryConflict): await store.acquire(identity, "owner")


@pytest.mark.asyncio
async def test_corrupt_state_and_wrong_revision_fail_closed(live):
    client, store, identity = live
    client.set(memory_key(identity), '{"version":0,"unknown":"invalid"}', ex=60)
    with pytest.raises(ValueError): await store.load(identity)
