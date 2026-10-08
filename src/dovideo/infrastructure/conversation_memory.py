"""Redis CAS/lease implementation and equivalent process-local test store."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from dovideo.application.conversation_memory import (
    ConversationIdentity, ConversationState, MEMORY_TTL_SECONDS, MemoryConflict,
)


def memory_key(identity: ConversationIdentity) -> str:
    return (f"conversation:{{{identity.media_id}}}:{identity.user_id}:{identity.goal_digest}:"
            f"{identity.analysis_mode}:{identity.conversation_id}:{identity.source_revision}")


_ACQUIRE = """
if redis.call('exists', KEYS[3]) == 1 then return -1 end
if redis.call('set', KEYS[1], ARGV[1], 'NX', 'PX', 75000) then
  redis.call('sadd', KEYS[2], KEYS[1], KEYS[4])
  redis.call('expire', KEYS[2], ARGV[2])
  return 1
end
return 0
"""
_COMMIT = """
if redis.call('exists', KEYS[4]) == 1 then return 0 end
if redis.call('get', KEYS[2]) ~= ARGV[4] then return 0 end
local old = redis.call('get', KEYS[1])
local version = 0
if old then version = cjson.decode(old).version end
if version ~= tonumber(ARGV[1]) then return 0 end
redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3])
redis.call('sadd', KEYS[3], KEYS[1], KEYS[2])
redis.call('expire', KEYS[3], ARGV[3])
return 1
"""
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""
_DELETE = """
redis.call('set', KEYS[2], '1', 'EX', ARGV[1])
local members = redis.call('smembers', KEYS[1])
for _, key in ipairs(members) do redis.call('del', key) end
redis.call('del', KEYS[1])
return #members
"""


class RedisConversationMemoryStore:
    def __init__(self, client: Any):
        self.client = client

    @staticmethod
    def _registry(media_id):
        return f"conversation:{{{media_id}}}:keys"

    @staticmethod
    def _deleted(media_id):
        return f"conversation:{{{media_id}}}:deleted"

    async def load(self, identity):
        def read():
            with self.client.pipeline(transaction=True) as pipe:
                pipe.exists(self._deleted(identity.media_id))
                pipe.get(memory_key(identity))
                deleted, raw = pipe.execute()
            if deleted:
                raise MemoryConflict("media deleted")
            state = ConversationState.model_validate_json(raw) if raw else ConversationState()
            if any(t.source_revision != identity.source_revision for t in state.turns):
                raise MemoryConflict("source revision mismatch")
            return state
        return await asyncio.to_thread(read)

    async def acquire(self, identity, token):
        key = memory_key(identity)
        result = await asyncio.to_thread(self.client.eval, _ACQUIRE, 4,
                  key + ":lease", self._registry(identity.media_id), self._deleted(identity.media_id),
                  key, token, MEMORY_TTL_SECONDS)
        if result == -1:
            raise MemoryConflict("media deleted")
        return result == 1

    async def release(self, identity, token):
        await asyncio.to_thread(self.client.eval, _RELEASE, 1, memory_key(identity) + ":lease", token)

    async def commit(self, identity, expected_version, state, token):
        if state.version != expected_version + 1:
            raise ValueError("invalid memory version")
        # Validate even model_copy-created state before storing it.
        state = ConversationState.model_validate_json(state.model_dump_json())
        key = memory_key(identity)
        return bool(await asyncio.to_thread(self.client.eval, _COMMIT, 4,
                     key, key + ":lease", self._registry(identity.media_id), self._deleted(identity.media_id),
                     expected_version, state.model_dump_json(), MEMORY_TTL_SECONDS, token))

    async def delete_media(self, media_id):
        await asyncio.to_thread(self.client.eval, _DELETE, 2, self._registry(media_id),
                               self._deleted(media_id), MEMORY_TTL_SECONDS)


class InMemoryConversationMemoryStore:
    """Local/test only; bounded per-session storage with TTL and CAS."""
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.states = {}
        self.leases = {}
        self.deleted = {}

    def _check(self, identity):
        now = self.clock()
        if self.deleted.get(identity.media_id, 0) > now:
            raise MemoryConflict("media deleted")
        for key, (_state, expires) in list(self.states.items()):
            if expires <= now:
                del self.states[key]
        for key, (_token, expires) in list(self.leases.items()):
            if expires <= now:
                del self.leases[key]

    async def load(self, identity):
        self._check(identity)
        entry = self.states.get(identity)
        return ConversationState.model_validate_json(entry[0]) if entry else ConversationState()

    async def acquire(self, identity, token):
        self._check(identity)
        if identity in self.leases:
            return False
        self.leases[identity] = (token, self.clock() + 75)
        return True

    async def release(self, identity, token):
        if self.leases.get(identity, (None,))[0] == token:
            del self.leases[identity]

    async def commit(self, identity, expected_version, state, token):
        self._check(identity)
        current = await self.load(identity)
        if self.leases.get(identity, (None,))[0] != token or current.version != expected_version:
            return False
        if state.version != expected_version + 1:
            raise ValueError("invalid memory version")
        ConversationState.model_validate_json(state.model_dump_json())
        self.states[identity] = (state.model_dump_json(), self.clock() + MEMORY_TTL_SECONDS)
        return True

    async def delete_media(self, media_id):
        self.deleted[media_id] = self.clock() + MEMORY_TTL_SECONDS
        for collection in (self.states, self.leases):
            for identity in list(collection):
                if identity.media_id == media_id:
                    del collection[identity]
