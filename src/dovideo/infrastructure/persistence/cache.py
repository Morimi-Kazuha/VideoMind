"""A dependency-free Redis-hash-shaped hot cache for checkpoint tests/local use."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any


DEFAULT_CHECKPOINT_TTL_SECONDS = 7 * 24 * 60 * 60


class InMemoryHotCheckpointCache:
    """Thread-safe hash cache with Redis-like key TTL semantics.

    The adapter is deliberately small and injectable.  It is useful for
    offline tests and local composition roots; it does not claim to be a
    Redis implementation.  ``fail_reads``/``fail_writes``/``fail_deletes``
    provide deterministic outage injection for repository tests.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        ttl_seconds: float = DEFAULT_CHECKPOINT_TTL_SECONDS,
        default_ttl_seconds: float | None = None,
        fail_reads: bool = False,
        fail_writes: bool = False,
        fail_deletes: bool = False,
        read_error: Exception | None = None,
        write_error: Exception | None = None,
        delete_error: Exception | None = None,
    ) -> None:
        self._clock = clock or time.monotonic
        self.default_ttl_seconds = (
            ttl_seconds if default_ttl_seconds is None else default_ttl_seconds
        )
        self._values: dict[str, dict[str, Any]] = defaultdict(dict)
        self._lists: dict[str, list[Any]] = defaultdict(list)
        self._sets: dict[str, set[Any]] = defaultdict(set)
        self._expires: dict[str, float] = {}
        self._media_keys: dict[int, set[str]] = defaultdict(set)
        self._checkpoint_keys: dict[tuple[int, str], set[str]] = defaultdict(set)
        self._lock = threading.RLock()
        self.fail_reads = fail_reads
        self.fail_writes = fail_writes
        self.fail_deletes = fail_deletes
        self.read_error = read_error
        self.write_error = write_error
        self.delete_error = delete_error

    def _raise_if_failed(self, operation: str) -> None:
        flag = getattr(self, f"fail_{operation}s")
        error = getattr(self, f"{operation}_error")
        if flag:
            raise RuntimeError(f"in-memory checkpoint cache {operation} unavailable")
        if error is not None:
            raise error

    def _purge(self, redis_key: str) -> None:
        expires = self._expires.get(redis_key)
        if expires is not None and expires <= self._clock():
            self._values.pop(redis_key, None)
            self._lists.pop(redis_key, None)
            self._sets.pop(redis_key, None)
            self._expires.pop(redis_key, None)
            for keys in self._media_keys.values():
                keys.discard(redis_key)
            for keys in self._checkpoint_keys.values():
                keys.discard(redis_key)

    def get_hash(self, redis_key: str, field: str) -> Any:
        with self._lock:
            self._raise_if_failed("read")
            self._purge(redis_key)
            return self._values.get(redis_key, {}).get(field)

    def set_hash(
        self,
        redis_key: str,
        field: str,
        value: Any,
        ttl_seconds: float | None = None,
    ) -> None:
        with self._lock:
            self._raise_if_failed("write")
            self._purge(redis_key)
            self._values[redis_key][field] = value
            if ttl_seconds is not None:
                self.expire(redis_key, ttl_seconds)

    def delete_hash(self, redis_key: str, *fields: str) -> None:
        with self._lock:
            self._raise_if_failed("delete")
            self._purge(redis_key)
            if fields:
                values = self._values.get(redis_key)
                if values is not None:
                    for field in fields:
                        values.pop(field, None)
                    if not values:
                        self._values.pop(redis_key, None)
            else:
                self._values.pop(redis_key, None)

    def delete_key(self, redis_key: str) -> None:
        self.delete_hash(redis_key)
        with self._lock:
            self._lists.pop(redis_key, None)
            self._sets.pop(redis_key, None)
            self._expires.pop(redis_key, None)
            for keys in self._media_keys.values():
                keys.discard(redis_key)
            for keys in self._checkpoint_keys.values():
                keys.discard(redis_key)

    def expire(self, redis_key: str, ttl_seconds: float) -> None:
        with self._lock:
            self._raise_if_failed("write")
            self._purge(redis_key)
            if (
                redis_key not in self._values
                and redis_key not in self._lists
                and redis_key not in self._sets
            ):
                return
            self._expires[redis_key] = self._clock() + max(0.0, float(ttl_seconds))
            if ttl_seconds <= 0:
                self._purge(redis_key)

    # Redis-shaped aliases.  They intentionally use the same in-memory lock.
    hget = get_hash
    hset = set_hash
    hdel = delete_hash

    def get(self, redis_key: str, field: str) -> Any:
        return self.get_hash(redis_key, field)

    read = get

    def put(
        self,
        redis_key: str,
        field: str,
        value: Any,
        ttl_seconds: float | None = None,
    ) -> None:
        self.set_hash(redis_key, field, value, ttl_seconds)

    write = put

    def delete(self, redis_key: str, *fields: str) -> None:
        if fields:
            self.delete_hash(redis_key, *fields)
        else:
            self.delete_key(redis_key)

    remove = delete

    # Redis-like list operations used only by AgentFeedback in Phase 8C.
    def right_push(self, redis_key: str, value: Any) -> int:
        with self._lock:
            self._raise_if_failed("write")
            self._purge(redis_key)
            self._lists[redis_key].append(value)
            return len(self._lists[redis_key])

    rpush = right_push
    rightPush = right_push

    def trim(self, redis_key: str, start: int, stop: int) -> None:
        with self._lock:
            self._raise_if_failed("write")
            self._purge(redis_key)
            values = self._lists.get(redis_key)
            if values is None:
                return
            length = len(values)
            first = start if start >= 0 else length + start
            last = stop if stop >= 0 else length + stop
            first = max(0, first)
            last = min(length - 1, last)
            self._lists[redis_key] = values[first : last + 1] if first <= last else []
            if not self._lists[redis_key]:
                self._lists.pop(redis_key, None)

    ltrim = trim

    def list_range(self, redis_key: str, start: int = 0, stop: int = -1) -> list[Any]:
        with self._lock:
            self._raise_if_failed("read")
            self._purge(redis_key)
            values = self._lists.get(redis_key, [])
            length = len(values)
            first = start if start >= 0 else length + start
            last = stop if stop >= 0 else length + stop
            first = max(0, first)
            last = min(length - 1, last)
            return list(values[first : last + 1]) if first <= last else []

    lrange = list_range
    range = list_range

    # Redis-like set operations used as the media goal-key index.
    def set_add(self, redis_key: str, *values: Any) -> int:
        with self._lock:
            self._raise_if_failed("write")
            self._purge(redis_key)
            before = len(self._sets[redis_key])
            self._sets[redis_key].update(values)
            return len(self._sets[redis_key]) - before

    sadd = set_add

    def set_members(self, redis_key: str) -> set[Any]:
        with self._lock:
            self._raise_if_failed("read")
            self._purge(redis_key)
            return set(self._sets.get(redis_key, set()))

    smembers = set_members

    def register_media_key(self, media_id: int, redis_key: str) -> None:
        with self._lock:
            self._media_keys[media_id].add(redis_key)

    def register_checkpoint_key(
        self,
        media_id: int,
        checkpoint_name: str,
        redis_key: str,
    ) -> None:
        with self._lock:
            self.register_media_key(media_id, redis_key)
            self._checkpoint_keys[(media_id, checkpoint_name)].add(redis_key)

    def delete_media(self, media_id: int) -> None:
        with self._lock:
            keys = tuple(self._media_keys.pop(media_id, ()))
        for redis_key in keys:
            self.delete_key(redis_key)
        with self._lock:
            for key in tuple(self._checkpoint_keys):
                if key[0] == media_id:
                    self._checkpoint_keys.pop(key, None)

    def delete_prefix(self, media_id: int, checkpoint_prefix: str) -> None:
        with self._lock:
            keys: set[str] = set()
            for (registered_media_id, name), redis_keys in self._checkpoint_keys.items():
                if registered_media_id == media_id and name.startswith(checkpoint_prefix):
                    keys.update(redis_keys)
        for redis_key in keys:
            self.delete_key(redis_key)

    def ttl(self, redis_key: str) -> float:
        """Return Redis-like remaining seconds (-2 missing, -1 no expiry)."""

        with self._lock:
            self._purge(redis_key)
            if (
                redis_key not in self._values
                and redis_key not in self._lists
                and redis_key not in self._sets
            ):
                return -2.0
            if redis_key not in self._expires:
                return -1.0
            return max(0.0, self._expires[redis_key] - self._clock())

    def snapshot(self, redis_key: str) -> dict[str, Any]:
        with self._lock:
            self._purge(redis_key)
            return dict(self._values.get(redis_key, {}))

    def list_snapshot(self, redis_key: str) -> list[Any]:
        return self.list_range(redis_key)

    def set_snapshot(self, redis_key: str) -> set[Any]:
        return self.set_members(redis_key)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._lists.clear()
            self._sets.clear()
            self._expires.clear()
            self._media_keys.clear()
            self._checkpoint_keys.clear()


InMemoryCheckpointCache = InMemoryHotCheckpointCache
MemoryHotCheckpointCache = InMemoryHotCheckpointCache
InMemoryHotCache = InMemoryHotCheckpointCache
MemoryCheckpointCache = InMemoryHotCheckpointCache


__all__ = [
    "DEFAULT_CHECKPOINT_TTL_SECONDS",
    "InMemoryCheckpointCache",
    "InMemoryHotCache",
    "InMemoryHotCheckpointCache",
    "MemoryCheckpointCache",
    "MemoryHotCheckpointCache",
]
