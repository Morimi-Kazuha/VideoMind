"""Redis 7 adapters used by the R2 production composition.

All Redis access is behind small application-shaped classes. Redis is never
treated as durable business truth: checkpoints are written to MySQL first,
and Redis loss is a cache miss or a recoverable coordination-state loss.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from dovideo.application import (
    CompletedUploadMarker,
    MEDIA_SESSION_TTL,
    UploadSession,
    UploadSessionState,
)
from dovideo.application.analysis_task_keys import goal_digest
from dovideo.application.ports.ingest import MergeLockLease
from dovideo.application.ports.tasks import TaskActiveMarkerPort, TaskCompletionPort, TaskLockPort
from dovideo.application.value_objects import AnalysisRequest, TaskKey

from .persistence.redis_cache import RedisCheckpointCache


REDIS_CHECKPOINT_TTL_SECONDS = 7 * 24 * 60 * 60
REDIS_TRACE_TTL_SECONDS = 7 * 24 * 60 * 60
REDIS_SESSION_TTL_SECONDS = 24 * 60 * 60
REDIS_LOGIN_FAILURE_WINDOW_SECONDS = 10 * 60
REDIS_MAX_LOGIN_FAILURES = 8
REDIS_ACTIVE_TTL_SECONDS = 6 * 60 * 60

_LOCK_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
_LOCK_REFRESH_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _seconds_until(value: datetime, now: datetime | None = None) -> int:
    remaining = (_as_utc(value) - _as_utc(now or datetime.now(timezone.utc))).total_seconds()
    return max(1, int(remaining))


def _task_suffix(key: TaskKey) -> str:
    return f"{int(key.media_id)}:{goal_digest(key.goal, key.mode)}"


class RedisTaskActiveMarker(TaskActiveMarkerPort):
    """Atomic active marker with a bounded six-hour lease."""

    def __init__(self, client: Any, *, prefix: str = "analysis:active") -> None:
        self.client = client
        self.prefix = prefix.rstrip(":")

    def redis_key(self, key: TaskKey) -> str:
        return f"{self.prefix}:{_task_suffix(key)}"

    async def reserve(self, key: TaskKey, *, ttl_seconds: float) -> bool:
        ttl = max(1, int(ttl_seconds))
        return bool(await asyncio.to_thread(self.client.set, self.redis_key(key), "1", nx=True, ex=ttl))

    async def is_active(self, key: TaskKey) -> bool:
        return bool(await asyncio.to_thread(self.client.exists, self.redis_key(key)))

    async def refresh(self, key: TaskKey, *, ttl_seconds: float) -> None:
        await asyncio.to_thread(self.client.expire, self.redis_key(key), max(1, int(ttl_seconds)))

    async def release(self, key: TaskKey) -> None:
        await asyncio.to_thread(self.client.delete, self.redis_key(key))


class RedisTaskCompletionMarker(TaskCompletionPort):
    """Redis completion marker used only for duplicate suppression."""

    def __init__(self, client: Any, *, prefix: str = "analysis:completed") -> None:
        self.client = client
        self.prefix = prefix.rstrip(":")

    def redis_key(self, key: TaskKey) -> str:
        return f"{self.prefix}:{_task_suffix(key)}"

    async def is_completed(self, key: TaskKey) -> bool:
        return bool(await asyncio.to_thread(self.client.exists, self.redis_key(key)))

    async def mark_completed(self, key: TaskKey, *, ttl_seconds: float) -> None:
        await asyncio.to_thread(self.client.set, self.redis_key(key), "1", ex=max(1, int(ttl_seconds)))

    async def clear_completed(self, key: TaskKey) -> None:
        await asyncio.to_thread(self.client.delete, self.redis_key(key))


class RedisTaskLock(TaskLockPort):
    """Token-safe distributed lock using SET NX PX and Lua release."""

    def __init__(
        self,
        client: Any,
        *,
        ttl_ms: int = 15 * 60 * 1000,
        prefix: str = "lock:analysis",
    ) -> None:
        if ttl_ms <= 0:
            raise ValueError("lock TTL must be positive")
        self.client = client
        self.ttl_ms = int(ttl_ms)
        self.prefix = prefix.rstrip(":")

    def redis_key(self, key: TaskKey) -> str:
        return f"{self.prefix}:{_task_suffix(key)}"

    async def acquire(self, key: TaskKey) -> object | None:
        token = secrets.token_urlsafe(32)
        acquired = await asyncio.to_thread(
            self.client.set,
            self.redis_key(key),
            token,
            nx=True,
            px=self.ttl_ms,
        )
        return token if acquired else None

    async def release(self, key: TaskKey, token: object) -> None:
        if not isinstance(token, str) or not token:
            return
        await asyncio.to_thread(
            self.client.eval,
            _LOCK_RELEASE_SCRIPT,
            1,
            self.redis_key(key),
            token,
        )

    async def refresh(self, key: TaskKey, token: object) -> bool:
        if not isinstance(token, str) or not token:
            return False
        result = await asyncio.to_thread(
            self.client.eval,
            _LOCK_REFRESH_SCRIPT,
            1,
            self.redis_key(key),
            token,
            self.ttl_ms,
        )
        return bool(result)


class _RedisMergeLease(MergeLockLease):
    def __init__(self, owner: "RedisMergeLock", key: str, token: str) -> None:
        self._owner = owner
        self._key = key
        self._token = token
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._owner.client.eval(_LOCK_RELEASE_SCRIPT, 1, self._key, self._token)
        except Exception:
            return


class RedisMergeLock:
    """Synchronous nonblocking upload-merge lock for ``ChunkUploadService``."""

    def __init__(self, client: Any, *, ttl_ms: int = 30 * 60 * 1000) -> None:
        if ttl_ms <= 0:
            raise ValueError("merge lock TTL must be positive")
        self.client = client
        self.ttl_ms = int(ttl_ms)
        self.prefix = "lock:upload-merge"

    def redis_key(self, upload_id: str) -> str:
        return f"{self.prefix}:{upload_id}"

    def try_acquire(self, upload_id: str) -> MergeLockLease | None:
        token = secrets.token_urlsafe(32)
        key = self.redis_key(upload_id)
        if not self.client.set(key, token, nx=True, px=self.ttl_ms):
            return None
        return _RedisMergeLease(self, key, token)


class RedisTaskQuota:
    """Fixed-window Redis quota with increment and expiry."""

    def __init__(self, client: Any, *, limit: int = 30, window_seconds: int = 60) -> None:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("quota settings must be positive")
        self.client = client
        self.limit = int(limit)
        self.window_seconds = int(window_seconds)

    async def try_acquire(self, request: AnalysisRequest) -> bool:
        bucket = int(time.time() // self.window_seconds)
        key = f"quota:analysis:{request.media.media_id}:{bucket}"
        count = int(await asyncio.to_thread(self.client.incr, key))
        if count == 1:
            await asyncio.to_thread(self.client.expire, key, self.window_seconds + 1)
        return count <= self.limit


class RedisUploadSessionStore:
    """24-hour resumable-upload metadata and completion markers."""

    def __init__(self, client: Any, *, ttl_seconds: int = int(MEDIA_SESSION_TTL.total_seconds())) -> None:
        if ttl_seconds <= 0:
            raise ValueError("upload session TTL must be positive")
        self.client = client
        self.ttl_seconds = int(ttl_seconds)
        self.session_prefix = "upload:session"
        self.completed_prefix = "upload:completed"

    def _session_key(self, upload_id: str) -> str:
        return f"{self.session_prefix}:{upload_id}"

    def _completed_key(self, upload_id: str) -> str:
        return f"{self.completed_prefix}:{upload_id}"

    async def create_session(self, session: UploadSession) -> None:
        await asyncio.to_thread(
            self.client.set,
            self._session_key(session.upload_id),
            json.dumps(_session_json(session), separators=(",", ":"), sort_keys=True),
            ex=_seconds_until(session.expires_at),
            nx=True,
        )

    async def get_session(self, upload_id: str) -> UploadSession | None:
        value = await asyncio.to_thread(self.client.get, self._session_key(upload_id))
        if value is None:
            return None
        try:
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            return _session_from_json(json.loads(value))
        except Exception:
            await self.delete_session(upload_id)
            return None

    async def renew_session(self, upload_id: str, session: UploadSession) -> None:
        await asyncio.to_thread(
            self.client.set,
            self._session_key(upload_id),
            json.dumps(_session_json(session), separators=(",", ":"), sort_keys=True),
            ex=_seconds_until(session.expires_at),
        )

    async def delete_session(self, upload_id: str) -> None:
        await asyncio.to_thread(self.client.delete, self._session_key(upload_id))

    async def get_completed(self, upload_id: str) -> CompletedUploadMarker | None:
        value = await asyncio.to_thread(self.client.get, self._completed_key(upload_id))
        if value is None:
            return None
        try:
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            return _marker_from_json(json.loads(value))
        except Exception:
            await self.delete_completed(upload_id)
            return None

    async def set_completed(self, marker: CompletedUploadMarker) -> None:
        await asyncio.to_thread(
            self.client.set,
            self._completed_key(marker.upload_id),
            json.dumps(_marker_json(marker), separators=(",", ":"), sort_keys=True),
            ex=_seconds_until(marker.expires_at),
        )

    async def delete_completed(self, upload_id: str) -> None:
        await asyncio.to_thread(self.client.delete, self._completed_key(upload_id))


def _session_json(value: UploadSession) -> dict[str, Any]:
    return {
        "uploadId": value.upload_id,
        "filename": value.filename,
        "totalChunks": value.total_chunks,
        "userId": value.user_id,
        "createdAt": _as_utc(value.created_at).isoformat(),
        "expiresAt": _as_utc(value.expires_at).isoformat(),
        "state": value.state.value,
    }


def _session_from_json(value: dict[str, Any]) -> UploadSession:
    return UploadSession(
        upload_id=str(value["uploadId"]),
        filename=str(value["filename"]),
        total_chunks=int(value["totalChunks"]),
        user_id=int(value["userId"]),
        created_at=datetime.fromisoformat(str(value["createdAt"])),
        expires_at=datetime.fromisoformat(str(value["expiresAt"])),
        state=UploadSessionState(str(value.get("state", "ACTIVE"))),
    )


def _marker_json(value: CompletedUploadMarker) -> dict[str, Any]:
    return {
        "uploadId": value.upload_id,
        "userId": value.user_id,
        "mediaId": value.media_id,
        "expiresAt": _as_utc(value.expires_at).isoformat(),
        "filename": value.filename,
        "totalChunks": value.total_chunks,
        "createdAt": None if value.created_at is None else _as_utc(value.created_at).isoformat(),
    }


def _marker_from_json(value: dict[str, Any]) -> CompletedUploadMarker:
    created = value.get("createdAt")
    return CompletedUploadMarker(
        upload_id=str(value["uploadId"]),
        user_id=int(value["userId"]),
        media_id=int(value["mediaId"]),
        expires_at=datetime.fromisoformat(str(value["expiresAt"])),
        filename=value.get("filename"),
        total_chunks=None if value.get("totalChunks") is None else int(value["totalChunks"]),
        created_at=None if created is None else datetime.fromisoformat(str(created)),
    )


__all__ = [
    "REDIS_ACTIVE_TTL_SECONDS",
    "REDIS_CHECKPOINT_TTL_SECONDS",
    "REDIS_LOGIN_FAILURE_WINDOW_SECONDS",
    "REDIS_MAX_LOGIN_FAILURES",
    "REDIS_SESSION_TTL_SECONDS",
    "REDIS_TRACE_TTL_SECONDS",
    "RedisCheckpointCache",
    "RedisMergeLock",
    "RedisTaskActiveMarker",
    "RedisTaskCompletionMarker",
    "RedisTaskLock",
    "RedisTaskQuota",
    "RedisUploadSessionStore",
]
