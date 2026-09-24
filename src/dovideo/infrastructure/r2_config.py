"""Explicit R2 production configuration and infrastructure composition."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .ai_interaction_limiter import (
    AiInteractionRateLimitConfigurationError,
    AiInteractionRateLimitSettings,
)
from .persistence.redis_cache import RedisCheckpointCache
from .persistence.sqlalchemy import (
    SqlAlchemyCheckpointStore,
    SqlAlchemyExecutionRecordRepository,
    SqlAlchemyFailedTaskStore,
    SqlAlchemyMediaRecordRepository,
    SqlAlchemyUserStore,
    create_schema,
    create_sqlalchemy_engine,
)
from .redis import (
    RedisMergeLock,
    RedisTaskActiveMarker,
    RedisTaskCompletionMarker,
    RedisTaskLock,
    RedisTaskQuota,
    RedisUploadSessionStore,
)
from .storage import MinioChunkObjectStore, MinioObjectStorage
from .vector.qdrant import QdrantVectorIndex


class R2ConfigurationError(RuntimeError):
    """Raised when production infrastructure is not explicitly configured."""


@dataclass(frozen=True, slots=True)
class R2Settings:
    """All R2 infrastructure settings; no local fallback is implicit."""

    profile: str
    database_url: str
    redis_url: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_secure: bool
    qdrant_url: str
    qdrant_api_key: str | None
    qdrant_collection: str
    media_workspace: Path
    pool_size: int = 10
    max_overflow: int = 10
    pool_timeout: float = 3.0
    ai_interaction_rate_limit: AiInteractionRateLimitSettings = field(
        default_factory=AiInteractionRateLimitSettings
    )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        require_production: bool = True,
    ) -> "R2Settings":
        values = os.environ if environ is None else environ
        profile = _value(values, "DOVIDEO_PROFILE") or "local"
        if require_production and profile.casefold() != "production":
            raise R2ConfigurationError(
                "R2 production requires DOVIDEO_PROFILE=production; no local fallback is allowed"
            )
        required = {
            name: _value(values, name)
            for name in (
                "DOVIDEO_DATABASE_URL",
                "DOVIDEO_REDIS_URL",
                "DOVIDEO_MINIO_ENDPOINT",
                "DOVIDEO_MINIO_ACCESS_KEY",
                "DOVIDEO_MINIO_SECRET_KEY",
                "DOVIDEO_MINIO_BUCKET",
                "DOVIDEO_QDRANT_URL",
                "DOVIDEO_QDRANT_COLLECTION",
            )
        }
        missing = tuple(name for name, value in required.items() if not value)
        if missing:
            raise R2ConfigurationError("required R2 infrastructure settings are missing")
        try:
            ai_interaction_rate_limit = AiInteractionRateLimitSettings.from_environment(values)
        except AiInteractionRateLimitConfigurationError as exc:
            raise R2ConfigurationError(
                "AI interaction rate-limit settings are invalid"
            ) from exc
        database_url = required["DOVIDEO_DATABASE_URL"]
        if not database_url.startswith("mysql+"):
            raise R2ConfigurationError("R2 durable source of truth must use a MySQL SQLAlchemy URL")
        minio_secure = (_value(values, "DOVIDEO_MINIO_SECURE") or "false").casefold() in {
            "1",
            "true",
            "yes",
        }
        root = Path(_value(values, "DOVIDEO_R2_RUNTIME_ROOT") or Path.cwd() / "work" / "r2-runtime")
        return cls(
            profile=profile,
            database_url=database_url,
            redis_url=required["DOVIDEO_REDIS_URL"],
            minio_endpoint=required["DOVIDEO_MINIO_ENDPOINT"],
            minio_access_key=required["DOVIDEO_MINIO_ACCESS_KEY"],
            minio_secret_key=required["DOVIDEO_MINIO_SECRET_KEY"],
            minio_bucket=required["DOVIDEO_MINIO_BUCKET"],
            minio_secure=minio_secure,
            qdrant_url=required["DOVIDEO_QDRANT_URL"],
            qdrant_api_key=_value(values, "DOVIDEO_QDRANT_API_KEY"),
            qdrant_collection=required["DOVIDEO_QDRANT_COLLECTION"],
            media_workspace=root,
            pool_size=_positive_int(values, "DOVIDEO_DB_POOL_SIZE", 10),
            max_overflow=_nonnegative_int(values, "DOVIDEO_DB_MAX_OVERFLOW", 10),
            pool_timeout=_positive_float(values, "DOVIDEO_DB_POOL_TIMEOUT", 3.0),
            ai_interaction_rate_limit=ai_interaction_rate_limit,
        )


@dataclass(slots=True)
class R2Infrastructure:
    """Concrete R2 adapters and clients owned by one composition root."""

    settings: R2Settings
    engine: object
    redis_client: object
    minio_client: object
    checkpoint_store: SqlAlchemyCheckpointStore
    checkpoint_cache: RedisCheckpointCache
    checkpoint_repository: object
    execution_record_repository: SqlAlchemyExecutionRecordRepository
    media_repository: SqlAlchemyMediaRecordRepository
    user_store: SqlAlchemyUserStore
    failed_task_store: SqlAlchemyFailedTaskStore
    upload_sessions: RedisUploadSessionStore
    object_storage: MinioObjectStorage
    chunk_store: MinioChunkObjectStore
    merge_lock: RedisMergeLock
    active_marker: RedisTaskActiveMarker
    completion_marker: RedisTaskCompletionMarker
    task_lock: RedisTaskLock
    quota: RedisTaskQuota
    vector_index: QdrantVectorIndex

    async def initialize(self) -> None:
        """Perform the bounded startup checks that require live services."""

        import asyncio

        await asyncio.to_thread(create_schema, self.engine)
        await self.object_storage.ensure_bucket()
        ping = getattr(self.redis_client, "ping", None)
        if callable(ping):
            await asyncio.to_thread(ping)

    def close(self) -> None:
        dispose = getattr(self.engine, "dispose", None)
        if callable(dispose):
            dispose()
        close = getattr(self.redis_client, "close", None)
        if callable(close):
            close()


def create_r2_infrastructure(settings: R2Settings | None = None) -> R2Infrastructure:
    """Build production adapters only after explicit production settings."""

    selected = settings or R2Settings.from_environment(require_production=True)
    from redis import Redis
    from minio import Minio

    engine = create_sqlalchemy_engine(
        selected.database_url,
        pool_size=selected.pool_size,
        max_overflow=selected.max_overflow,
        pool_timeout=selected.pool_timeout,
    )
    redis_client = Redis.from_url(
        selected.redis_url,
        decode_responses=False,
        socket_connect_timeout=selected.pool_timeout,
        socket_timeout=selected.pool_timeout,
        health_check_interval=30,
    )
    host, secure = _minio_host(selected.minio_endpoint, selected.minio_secure)
    minio_client = Minio(
        host,
        access_key=selected.minio_access_key,
        secret_key=selected.minio_secret_key,
        secure=secure,
    )
    checkpoint_store = SqlAlchemyCheckpointStore(engine)
    checkpoint_cache = RedisCheckpointCache(redis_client)
    from .persistence.repository import CheckpointRepository

    checkpoint_repository = CheckpointRepository(checkpoint_store, checkpoint_cache)
    object_storage = MinioObjectStorage(
        minio_client,
        bucket=selected.minio_bucket,
        workspace_parent=selected.media_workspace,
    )
    return R2Infrastructure(
        settings=selected,
        engine=engine,
        redis_client=redis_client,
        minio_client=minio_client,
        checkpoint_store=checkpoint_store,
        checkpoint_cache=checkpoint_cache,
        checkpoint_repository=checkpoint_repository,
        execution_record_repository=SqlAlchemyExecutionRecordRepository(engine),
        media_repository=SqlAlchemyMediaRecordRepository(engine),
        user_store=SqlAlchemyUserStore(engine),
        failed_task_store=SqlAlchemyFailedTaskStore(engine),
        upload_sessions=RedisUploadSessionStore(redis_client),
        object_storage=object_storage,
        chunk_store=MinioChunkObjectStore(object_storage),
        merge_lock=RedisMergeLock(redis_client),
        active_marker=RedisTaskActiveMarker(redis_client),
        completion_marker=RedisTaskCompletionMarker(redis_client),
        task_lock=RedisTaskLock(redis_client),
        quota=RedisTaskQuota(redis_client),
        vector_index=QdrantVectorIndex(
            base_url=selected.qdrant_url,
            api_key=selected.qdrant_api_key,
            collection=selected.qdrant_collection,
        ),
    )


def _minio_host(endpoint: str, secure: bool) -> tuple[str, bool]:
    text = endpoint.strip()
    if "://" not in text:
        return text, secure
    parsed = urlsplit(text)
    if not parsed.netloc or parsed.path not in {"", "/"}:
        raise R2ConfigurationError("MinIO endpoint must be host:port or a bare URL")
    return parsed.netloc, parsed.scheme.casefold() == "https"


def _value(values: Mapping[str, str], name: str) -> str | None:
    item = values.get(name)
    if item is None or not item.strip():
        return None
    return item.strip()


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    value = _value(values, name)
    try:
        result = default if value is None else int(value)
    except (TypeError, ValueError) as exc:
        raise R2ConfigurationError("R2 integer setting is invalid") from exc
    if result <= 0:
        raise R2ConfigurationError("R2 integer setting must be positive")
    return result


def _nonnegative_int(values: Mapping[str, str], name: str, default: int) -> int:
    value = _value(values, name)
    try:
        result = default if value is None else int(value)
    except (TypeError, ValueError) as exc:
        raise R2ConfigurationError("R2 integer setting is invalid") from exc
    if result < 0:
        raise R2ConfigurationError("R2 integer setting cannot be negative")
    return result


def _positive_float(values: Mapping[str, str], name: str, default: float) -> float:
    value = _value(values, name)
    try:
        result = default if value is None else float(value)
    except (TypeError, ValueError) as exc:
        raise R2ConfigurationError("R2 numeric setting is invalid") from exc
    if result <= 0:
        raise R2ConfigurationError("R2 numeric setting must be positive")
    return result


__all__ = [
    "R2ConfigurationError",
    "R2Infrastructure",
    "R2Settings",
    "create_r2_infrastructure",
]
