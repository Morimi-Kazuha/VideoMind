"""Replaceable persistence protocols for generic Agent checkpoints.

These protocols intentionally describe only records and hash fields.  A
future MySQL implementation can replace :class:`DurableCheckpointStore` and
a future Redis implementation can replace :class:`HotCheckpointCache`
without changing the application AgentLoop API.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol, TypeVar

from .models import CheckpointRecord


T = TypeVar("T")


class DurableCheckpointStore(Protocol):
    """The recovery source of truth for checkpoint records."""

    def read(self, media_id: int, checkpoint_name: str) -> CheckpointRecord | None:
        ...

    def upsert(
        self,
        media_id: int,
        checkpoint_name: str,
        stage: str | None,
        payload: str | None,
    ) -> None:
        ...

    def delete(self, media_id: int, checkpoint_name: str) -> None:
        ...

    def delete_prefix(self, media_id: int, checkpoint_prefix: str) -> None:
        ...

    def delete_media(self, media_id: int) -> None:
        ...


class HotCheckpointCache(Protocol):
    """Hash-field cache with explicit expiration and deletion operations."""

    def get_hash(self, redis_key: str, field: str) -> Any:
        ...

    def set_hash(
        self,
        redis_key: str,
        field: str,
        value: Any,
        ttl_seconds: float | None = None,
    ) -> None:
        ...

    def delete_hash(self, redis_key: str, *fields: str) -> None:
        ...

    def delete_key(self, redis_key: str) -> None:
        ...

    def expire(self, redis_key: str, ttl_seconds: float) -> None:
        ...

    # Optional Redis-shaped extensions used by the Phase 8C lifecycle.  They
    # are listed here so a redis-py adapter can replace the in-memory cache
    # without changing AgentCheckpointService.
    def right_push(self, redis_key: str, value: Any) -> int:
        ...

    def trim(self, redis_key: str, start: int, stop: int) -> None:
        ...

    def list_range(self, redis_key: str, start: int = 0, stop: int = -1) -> list[Any]:
        ...

    def set_add(self, redis_key: str, *values: Any) -> int:
        ...

    def set_members(self, redis_key: str) -> set[Any]:
        ...

    def register_media_key(self, media_id: int, redis_key: str) -> None:
        ...

    def register_checkpoint_key(
        self,
        media_id: int,
        checkpoint_name: str,
        redis_key: str,
    ) -> None:
        ...

    def delete_prefix(self, media_id: int, checkpoint_prefix: str) -> None:
        ...

    def delete_media(self, media_id: int) -> None:
        ...


class CheckpointCodec(Protocol):
    """Version-aware JSON codec used for durable and cached payloads."""

    def encode(self, value: Any) -> str:
        ...

    def decode(self, payload: str, target_type: Any = None) -> Any:
        ...


CheckpointStore = DurableCheckpointStore
CheckpointCache = HotCheckpointCache


__all__ = [
    "CheckpointCache",
    "CheckpointCodec",
    "CheckpointStore",
    "DurableCheckpointStore",
    "HotCheckpointCache",
]
