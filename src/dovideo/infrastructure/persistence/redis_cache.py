"""Redis-py-compatible hot cache adapter for checkpoint records.

No Redis package is imported here.  The adapter accepts any synchronous client
implementing the small redis-py method surface used below.  The surrounding
``CheckpointRepository`` remains responsible for treating cache failures as
non-authoritative after a durable commit.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any, Callable

from .errors import CheckpointCacheError


def _decode(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8")
    return value


class RedisCheckpointCache:
    """Small synchronous cache boundary backed by an injected Redis client."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        redis_client: Any | None = None,
        ttl_seconds: float = 7 * 24 * 60 * 60,
        registry_ttl_seconds: float | None = None,
    ) -> None:
        if client is not None and redis_client is not None:
            raise ValueError("client and redis_client are mutually exclusive")
        self.client = client if client is not None else redis_client
        if self.client is None:
            raise ValueError("a Redis-compatible client is required")
        for method_name in ("hget", "hset", "hdel", "delete", "expire"):
            if not callable(getattr(self.client, method_name, None)):
                raise TypeError(f"Redis client has no {method_name} operation")
        if ttl_seconds < 0:
            raise ValueError("Redis checkpoint TTL cannot be negative")
        self.ttl_seconds = float(ttl_seconds)
        self.registry_ttl_seconds = (
            self.ttl_seconds
            if registry_ttl_seconds is None
            else float(registry_ttl_seconds)
        )
        self._lock = threading.RLock()
        self._registrations: dict[int, dict[str, str]] = {}

    def _run(self, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except CheckpointCacheError:
            raise
        except Exception as exc:
            # Keep Redis connection details, key material, and provider error
            # messages out of the public exception string.
            raise CheckpointCacheError("Redis Agent checkpoint cache operation failed") from exc

    @staticmethod
    def _media_registry_key(media_id: int) -> str:
        return f"agent:checkpoint:{media_id}:keys"

    @staticmethod
    def _checkpoint_registry_key(media_id: int) -> str:
        return f"agent:checkpoint:{media_id}:checkpoint-index"

    def get_hash(self, redis_key: str, field: str) -> Any:
        return self._run(lambda: _decode(self.client.hget(redis_key, field)))

    def set_hash(
        self,
        redis_key: str,
        field: str,
        value: Any,
        ttl_seconds: float | None = None,
    ) -> None:
        def put() -> None:
            self.client.hset(redis_key, field, value)
            if ttl_seconds is not None:
                self.client.expire(redis_key, max(0, int(ttl_seconds)))

        self._run(put)

    def delete_hash(self, redis_key: str, *fields: str) -> None:
        if not fields:
            self.delete_key(redis_key)
            return
        self._run(lambda: self.client.hdel(redis_key, *fields))

    def delete_key(self, redis_key: str) -> None:
        self._run(lambda: self.client.delete(redis_key))
        with self._lock:
            for registrations in self._registrations.values():
                for checkpoint_name, registered_key in tuple(registrations.items()):
                    if registered_key == redis_key:
                        registrations.pop(checkpoint_name, None)

    def delete_keys(self, *redis_keys: str) -> None:
        if not redis_keys:
            return
        self._run(lambda: self.client.delete(*redis_keys))
        with self._lock:
            for registrations in self._registrations.values():
                for checkpoint_name, registered_key in tuple(registrations.items()):
                    if registered_key in redis_keys:
                        registrations.pop(checkpoint_name, None)

    def expire(self, redis_key: str, ttl_seconds: float) -> None:
        self._run(lambda: self.client.expire(redis_key, max(0, int(ttl_seconds))))

    def ttl(self, redis_key: str) -> float:
        method = getattr(self.client, "ttl", None)
        if not callable(method):
            return -1.0
        value = self._run(lambda: method(redis_key))
        return float(value)

    # Redis-like feedback list operations.
    def right_push(self, redis_key: str, value: Any) -> int:
        return int(self._run(lambda: self.client.rpush(redis_key, value)))

    rpush = right_push
    rightPush = right_push

    def trim(self, redis_key: str, start: int, stop: int) -> None:
        self._run(lambda: self.client.ltrim(redis_key, int(start), int(stop)))

    ltrim = trim

    def list_range(self, redis_key: str, start: int = 0, stop: int = -1) -> list[Any]:
        def read_list() -> list[Any]:
            values = self.client.lrange(redis_key, int(start), int(stop))
            return [] if values is None else [_decode(value) for value in values]

        return self._run(read_list)

    lrange = list_range
    range = list_range

    # Redis-like goal key index operations.
    def set_add(self, redis_key: str, *values: Any) -> int:
        return int(self._run(lambda: self.client.sadd(redis_key, *values)))

    sadd = set_add

    def set_members(self, redis_key: str) -> set[Any]:
        def read_set() -> set[Any]:
            values = self.client.smembers(redis_key)
            return set() if values is None else {_decode(value) for value in values}

        return self._run(read_set)

    smembers = set_members

    def register_media_key(self, media_id: int, redis_key: str) -> None:
        registry = self._media_registry_key(media_id)
        self.set_add(registry, redis_key)
        self.expire(registry, self.registry_ttl_seconds)

    def register_checkpoint_key(
        self,
        media_id: int,
        checkpoint_name: str,
        redis_key: str,
    ) -> None:
        self.register_media_key(media_id, redis_key)
        index = self._checkpoint_registry_key(media_id)
        self.set_hash(index, checkpoint_name, redis_key)
        self.expire(index, self.registry_ttl_seconds)
        with self._lock:
            self._registrations.setdefault(int(media_id), {})[
                str(checkpoint_name)
            ] = str(redis_key)

    def _registered_checkpoint_keys(
        self,
        media_id: int,
        checkpoint_prefix: str | None = None,
    ) -> tuple[tuple[str, str], ...]:
        index = self._checkpoint_registry_key(media_id)
        values: Mapping[Any, Any] | None = None
        hgetall = getattr(self.client, "hgetall", None)
        if callable(hgetall):
            raw = self._run(lambda: hgetall(index))
            if raw is not None:
                values = raw
        pairs: dict[str, str] = {}
        if values is not None:
            pairs.update({str(_decode(name)): str(_decode(value)) for name, value in values.items()})
        with self._lock:
            pairs.update(self._registrations.get(int(media_id), {}))
        if checkpoint_prefix is not None:
            pairs = {
                name: key
                for name, key in pairs.items()
                if name.startswith(str(checkpoint_prefix))
            }
        return tuple(pairs.items())

    def delete_prefix(self, media_id: int, checkpoint_prefix: str) -> None:
        pairs = self._registered_checkpoint_keys(media_id, checkpoint_prefix)
        keys = tuple(dict.fromkeys(key for _name, key in pairs))
        if keys:
            self.delete_keys(*keys)
        if pairs:
            index = self._checkpoint_registry_key(media_id)
            self._run(lambda: self.client.hdel(index, *(name for name, _key in pairs)))

    def delete_media(self, media_id: int) -> None:
        registry = self._media_registry_key(media_id)
        keys = set(str(key) for key in self.set_members(registry))
        with self._lock:
            keys.update(self._registrations.get(int(media_id), {}).values())
        keys.update((registry, self._checkpoint_registry_key(media_id)))
        if keys:
            self.delete_keys(*tuple(keys))
        with self._lock:
            self._registrations.pop(int(media_id), None)

    def snapshot(self, redis_key: str) -> dict[str, Any]:
        hgetall = getattr(self.client, "hgetall", None)
        if not callable(hgetall):
            return {}
        def read_hash() -> dict[str, Any]:
            values = hgetall(redis_key) or {}
            return {str(_decode(key)): _decode(value) for key, value in values.items()}

        return self._run(read_hash)

    # Familiar names for adapter composition roots.
    get = get_hash
    put = set_hash
    write = set_hash
    delete = delete_key
    remove = delete_key


RedisHotCheckpointCache = RedisCheckpointCache
RedisCache = RedisCheckpointCache


__all__ = ["RedisCache", "RedisCheckpointCache", "RedisHotCheckpointCache"]
