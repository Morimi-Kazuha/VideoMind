"""Durable-first Agent checkpoint repository.

The repository follows the Java boundary closely: durable storage is the
recovery source of truth and a hash-shaped cache is only a read-through
optimization.  Cache failures are isolated, and cache writes happen only
after the durable operation returns successfully.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from dovideo.domain import TaskStage

from .cache import DEFAULT_CHECKPOINT_TTL_SECONDS
from .codec import JsonCheckpointCodec
from .errors import (
    CheckpointDeserializationError,
    CheckpointDeleteError,
    CheckpointError,
    CheckpointReadError,
    CheckpointVersionMismatchError,
    CheckpointWriteError,
)
from .models import CheckpointRecord


_MISSING = object()


def _stage_value(stage: TaskStage | str | None) -> str:
    if isinstance(stage, TaskStage):
        return stage.value
    return "" if stage is None else str(stage)


def _payload_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode("utf-8")
    raise TypeError("checkpoint payload must be text or UTF-8 bytes")


def _record(value: Any) -> CheckpointRecord:
    if isinstance(value, CheckpointRecord):
        return value
    if isinstance(value, Mapping):
        media_id = value.get("media_id", value.get("mediaId"))
        checkpoint_name = value.get(
            "checkpoint_name",
            value.get("checkpointName", value.get("checkpoint_key")),
        )
        if checkpoint_name is None:
            checkpoint_name = value.get("checkpointKey")
        return CheckpointRecord(
            media_id=int(media_id),
            checkpoint_name=str(checkpoint_name),
            stage=_stage_value(value.get("stage")),
            payload=(
                None
                if value.get("payload") is None
                else _payload_text(value.get("payload"))
            ),
            updated_at=value.get("updated_at", value.get("updatedAt")),
        )
    # A small amount of attribute tolerance keeps DB-driver row wrappers
    # replaceable without coupling this repository to a specific ORM.
    media_id = getattr(value, "media_id", getattr(value, "mediaId", None))
    checkpoint_name = getattr(
        value,
        "checkpoint_name",
        getattr(value, "checkpointName", getattr(value, "checkpoint_key", None)),
    )
    if checkpoint_name is None:
        checkpoint_name = getattr(value, "checkpointKey", None)
    if media_id is not None and checkpoint_name is not None:
        payload = getattr(value, "payload", None)
        return CheckpointRecord(
            media_id=int(media_id),
            checkpoint_name=str(checkpoint_name),
            stage=_stage_value(getattr(value, "stage", None)),
            payload=None if payload is None else _payload_text(payload),
            updated_at=getattr(value, "updated_at", getattr(value, "updatedAt", None)),
        )
    raise TypeError("durable adapter returned an invalid checkpoint record")


class CheckpointRepository:
    """Read-through/cache-aside repository for generic checkpoint payloads."""

    def __init__(
        self,
        durable: Any | None = None,
        cache: Any | None = None,
        codec: Any | None = None,
        *,
        durable_store: Any | None = None,
        hot_cache: Any | None = None,
        serializer: Any | None = None,
        ttl_seconds: float = DEFAULT_CHECKPOINT_TTL_SECONDS,
        cache_ttl_seconds: float | None = None,
    ) -> None:
        self.durable = durable if durable is not None else durable_store
        if self.durable is None:
            raise ValueError("a durable checkpoint store is required")
        self.cache = cache if cache is not None else hot_cache
        self.codec = codec if codec is not None else serializer or JsonCheckpointCodec()
        self.ttl_seconds = float(
            ttl_seconds if cache_ttl_seconds is None else cache_ttl_seconds
        )

        # Public aliases make composition roots explicit without changing the
        # underlying port objects.
        self.durable_store = self.durable
        self.hot_cache = self.cache
        self.serializer = self.codec

    # ------------------------------------------------------------------
    # Durable record operations
    # ------------------------------------------------------------------
    def read_record(self, media_id: int, checkpoint_name: str) -> CheckpointRecord | None:
        try:
            reader = getattr(self.durable, "read", None)
            if not callable(reader):
                reader = getattr(self.durable, "find", None)
            if not callable(reader):
                raise TypeError("durable adapter has no read operation")
            value = reader(media_id, checkpoint_name)
            return None if value is None else _record(value)
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointReadError(
                f"读取 Agent Checkpoint 失败: {checkpoint_name}"
            ) from exc

    def upsert_record(
        self,
        media_id: int,
        checkpoint_name: str,
        stage: TaskStage | str | None = None,
        payload: str | None = None,
        *,
        redis_key: str | None = None,
        field: str | None = None,
    ) -> None:
        """Persist one record, then optionally warm its cache field."""

        self._durable_upsert(media_id, checkpoint_name, stage, payload)
        if redis_key is not None and field is not None:
            self._cache_write_field(
                redis_key,
                field,
                payload,
                _stage_value(stage),
                media_id=media_id,
                checkpoint_name=checkpoint_name,
            )

    def upsert(
        self,
        media_id: int,
        checkpoint_name: str,
        stage: TaskStage | str | None = None,
        payload: str | None = None,
        *,
        value: Any = _MISSING,
        redis_key: str | None = None,
        field: str | None = None,
    ) -> None:
        """Convenient record upsert accepting either raw or typed payload."""

        if value is not _MISSING:
            if payload is not None:
                raise ValueError("provide payload or value, not both")
            payload = self.codec.encode(value)
        self.upsert_record(
            media_id,
            checkpoint_name,
            stage,
            payload,
            redis_key=redis_key,
            field=field,
        )

    def _durable_upsert(
        self,
        media_id: int,
        checkpoint_name: str,
        stage: TaskStage | str | None,
        payload: str | None,
    ) -> None:
        try:
            writer = getattr(self.durable, "upsert", None)
            if not callable(writer):
                raise TypeError("durable adapter has no upsert operation")
            writer(media_id, checkpoint_name, _stage_value(stage), payload)
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointWriteError("保存 Agent Checkpoint 失败") from exc

    def _durable_upsert_many(self, records: Sequence[CheckpointRecord]) -> None:
        """Use an adapter transaction when available, otherwise preserve order."""

        try:
            writer_many = getattr(self.durable, "upsert_many", None)
            if callable(writer_many):
                writer_many(records)
                return
            for item in records:
                self._durable_upsert(
                    item.media_id,
                    item.checkpoint_name,
                    item.stage,
                    item.payload,
                )
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointWriteError("保存 Agent Checkpoint 失败") from exc

    def _durable_delete(self, media_id: int, checkpoint_name: str) -> None:
        try:
            deleter = getattr(self.durable, "delete", None)
            if not callable(deleter):
                raise TypeError("durable adapter has no delete operation")
            deleter(media_id, checkpoint_name)
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointDeleteError(
                f"删除 Agent Checkpoint 失败: {checkpoint_name}"
            ) from exc

    # ------------------------------------------------------------------
    # Java-shaped typed read/write operations
    # ------------------------------------------------------------------
    def read(
        self,
        media_id: int,
        checkpoint_name: str,
        redis_key: str | None = None,
        field: str | None = None,
        target_type: Any = None,
        *,
        model_type: Any = None,
        type: Any = None,
        model: Any = None,
    ) -> Any:
        """Read a raw payload or deserialize it into ``target_type``.

        With only ``media_id`` and ``checkpoint_name`` this returns the
        durable :class:`CheckpointRecord`; with cache coordinates but no type
        it returns the raw JSON payload.  The Java-compatible five-argument
        form returns the requested model and performs read-through caching.
        """

        if model_type is not None:
            if target_type is not None:
                raise ValueError("provide target_type or model_type, not both")
            target_type = model_type
        if type is not None:
            if target_type is not None:
                raise ValueError("provide only one checkpoint target type")
            target_type = type
        if model is not None:
            if target_type is not None:
                raise ValueError("provide only one checkpoint target type")
            target_type = model
        if redis_key is None and field is None and target_type is None:
            return self.read_record(media_id, checkpoint_name)
        if target_type is None:
            return self.read_payload(media_id, checkpoint_name, redis_key, field)

        if self.cache is not None and redis_key is not None and field is not None:
            try:
                cached = self._cache_get(redis_key, field)
                if cached is not None:
                    try:
                        return self.codec.decode(_payload_text(cached), target_type)
                    except Exception:
                        # A malformed, old-version, or wrong-model cache entry
                        # is not authoritative.  Evict and continue to durable.
                        self._cache_evict(redis_key, field)
            except Exception:
                self._cache_evict(redis_key, field)

        record = self.read_record(media_id, checkpoint_name)
        if record is None or record.payload is None:
            return None
        try:
            value = self.codec.decode(record.payload, target_type)
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointDeserializationError(
                f"读取 Agent Checkpoint 失败: {checkpoint_name}"
            ) from exc
        if redis_key is not None and field is not None:
            self._cache_write_field(
                redis_key,
                field,
                record.payload,
                record.stage,
                media_id=media_id,
                checkpoint_name=checkpoint_name,
            )
        return value

    def read_value(
        self,
        media_id: int,
        checkpoint_name: str,
        redis_key: str,
        field: str,
        target_type: Any,
    ) -> Any:
        return self.read(media_id, checkpoint_name, redis_key, field, target_type)

    def read_payload(
        self,
        media_id: int,
        checkpoint_name: str,
        redis_key: str | None = None,
        field: str | None = None,
    ) -> str | None:
        """Read the encoded payload with optional cache-aside coordinates."""

        if self.cache is not None and redis_key is not None and field is not None:
            try:
                cached = self._cache_get(redis_key, field)
                if cached is not None:
                    return _payload_text(cached)
            except Exception:
                self._cache_evict(redis_key, field)
        record = self.read_record(media_id, checkpoint_name)
        if record is None or record.payload is None:
            return None
        if redis_key is not None and field is not None:
            self._cache_write_field(
                redis_key,
                field,
                record.payload,
                record.stage,
                media_id=media_id,
                checkpoint_name=checkpoint_name,
            )
        return record.payload

    def read_stage(
        self,
        media_id: int,
        checkpoint_name: str,
        redis_key: str | None = None,
    ) -> TaskStage | None:
        """Read a stage, evicting malformed cached values before fallback."""

        if self.cache is not None and redis_key is not None:
            try:
                cached = self._cache_get(redis_key, "stage")
                if cached is not None:
                    try:
                        return self._parse_stage(cached)
                    except Exception:
                        self._cache_evict(redis_key, "stage")
            except Exception:
                self._cache_evict(redis_key, "stage")

        record = self.read_record(media_id, checkpoint_name)
        if record is None:
            return None
        stage = self._parse_stage(record.stage)
        if redis_key is not None:
            self._cache_write_stage(
                redis_key,
                record.stage,
                media_id=media_id,
                checkpoint_name=checkpoint_name,
            )
        return stage

    @staticmethod
    def _parse_stage(value: Any) -> TaskStage:
        if isinstance(value, TaskStage):
            return value
        stage = TaskStage.from_value(None if value is None else str(value))
        if stage is None:
            raise CheckpointDeserializationError(
                f"Agent Checkpoint stage is malformed: {value!r}"
            )
        return stage

    def write(
        self,
        media_id: int,
        checkpoint_name: str,
        stage_checkpoint_name: str,
        redis_key: str,
        field: str,
        stage: TaskStage | str,
        value: Any,
    ) -> None:
        """Persist payload and stage rows, then warm both cache fields."""

        payload = self.codec.encode(value)
        stage_text = _stage_value(stage)
        self._durable_upsert_many(
            (
                CheckpointRecord(media_id, checkpoint_name, stage_text, payload),
                CheckpointRecord(media_id, stage_checkpoint_name, stage_text, None),
            )
        )
        self._cache_write_field(
            redis_key,
            field,
            payload,
            stage_text,
            media_id=media_id,
            checkpoint_name=checkpoint_name,
        )

    def write_standalone(
        self,
        media_id: int,
        checkpoint_name: str,
        redis_key: str,
        field: str,
        stage: TaskStage | str,
        value: Any,
    ) -> None:
        payload = self.codec.encode(value)
        stage_text = _stage_value(stage)
        self._durable_upsert(media_id, checkpoint_name, stage_text, payload)
        self._cache_write_field(
            redis_key,
            field,
            payload,
            stage_text,
            media_id=media_id,
            checkpoint_name=checkpoint_name,
        )

    def write_stage(
        self,
        media_id: int,
        checkpoint_name: str,
        redis_key: str,
        stage: TaskStage | str,
    ) -> None:
        stage_text = _stage_value(stage)
        # This deliberately completes durable upsert before touching cache.
        self._durable_upsert(media_id, checkpoint_name, stage_text, None)
        self._cache_write_stage(
            redis_key,
            stage_text,
            media_id=media_id,
            checkpoint_name=checkpoint_name,
        )

    # ------------------------------------------------------------------
    # Deletes (durable first, cache best effort)
    # ------------------------------------------------------------------
    def delete(
        self,
        media_id: int,
        checkpoint_name: str,
        redis_key: str | None = None,
    ) -> None:
        self._durable_delete(media_id, checkpoint_name)
        if redis_key is not None:
            self._cache_delete_key(redis_key)

    def delete_prefix(
        self,
        media_id: int,
        checkpoint_prefix: str,
        *,
        redis_key: str | None = None,
    ) -> None:
        try:
            deleter = getattr(self.durable, "delete_prefix", None)
            if not callable(deleter):
                deleter = getattr(self.durable, "delete_by_prefix", None)
            if not callable(deleter):
                raise TypeError("durable adapter has no prefix delete operation")
            deleter(media_id, checkpoint_prefix)
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointDeleteError(
                f"删除 Agent Checkpoint 前缀失败: {checkpoint_prefix}"
            ) from exc
        self._cache_delete_prefix(media_id, checkpoint_prefix, redis_key)

    def delete_media(
        self,
        media_id: int,
        redis_keys: Sequence[str] | None = None,
        *,
        cache_keys: Sequence[str] | None = None,
    ) -> None:
        try:
            deleter = getattr(self.durable, "delete_media", None)
            if not callable(deleter):
                deleter = getattr(self.durable, "delete_by_media_id", None)
            if not callable(deleter):
                raise TypeError("durable adapter has no media delete operation")
            deleter(media_id)
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointDeleteError(
                f"删除 media Agent Checkpoint 失败: {media_id}"
            ) from exc
        keys = tuple(redis_keys or cache_keys or ())
        for key in keys:
            self._cache_delete_key(key)
        self._cache_delete_media(media_id)

    delete_by_prefix = delete_prefix
    delete_by_media_id = delete_media
    delete_media_id = delete_media
    write_standalone_checkpoint = write_standalone

    # ------------------------------------------------------------------
    # Best-effort cache adapter compatibility helpers
    # ------------------------------------------------------------------
    def _cache_get(self, redis_key: str, field: str) -> Any:
        if self.cache is None:
            return None
        for name in ("get_hash", "hget", "get"):
            method = getattr(self.cache, name, None)
            if callable(method):
                return method(redis_key, field)
        if isinstance(self.cache, Mapping):
            return self.cache.get((redis_key, field))
        raise TypeError("hot cache has no hash read operation")

    def _cache_set(self, redis_key: str, field: str, value: Any) -> None:
        if self.cache is None:
            return
        for name in ("set_hash", "hset", "put"):
            method = getattr(self.cache, name, None)
            if callable(method):
                method(redis_key, field, value)
                return
        raise TypeError("hot cache has no hash write operation")

    def _cache_expire(self, redis_key: str) -> None:
        if self.cache is None:
            return
        method = getattr(self.cache, "expire", None)
        if callable(method):
            method(redis_key, self.ttl_seconds)

    def _cache_delete_fields(self, redis_key: str, *fields: str) -> None:
        if self.cache is None:
            return
        for name in ("delete_hash", "hdel"):
            method = getattr(self.cache, name, None)
            if callable(method):
                method(redis_key, *fields)
                return
        method = getattr(self.cache, "delete", None)
        if callable(method):
            method(redis_key, *fields)
            return
        raise TypeError("hot cache has no hash delete operation")

    def _cache_delete_key(self, redis_key: str) -> None:
        if self.cache is None:
            return
        try:
            for name in ("delete_key", "delete"):
                method = getattr(self.cache, name, None)
                if callable(method):
                    method(redis_key)
                    return
        except Exception:
            # Cache deletion is best effort after durable success.
            return

    def _cache_evict(self, redis_key: str, *fields: str) -> None:
        try:
            self._cache_delete_fields(redis_key, *fields)
        except Exception:
            pass

    def _cache_write_field(
        self,
        redis_key: str,
        field: str,
        payload: str | None,
        stage: str | None,
        *,
        media_id: int | None = None,
        checkpoint_name: str | None = None,
    ) -> None:
        if self.cache is None or payload is None:
            if self.cache is not None and stage:
                self._cache_write_stage(
                    redis_key,
                    stage,
                    media_id=media_id,
                    checkpoint_name=checkpoint_name,
                )
            return
        fields = [field]
        if stage:
            fields.append("stage")
        try:
            self._cache_set(redis_key, field, payload)
            if stage:
                self._cache_set(redis_key, "stage", stage)
            self._cache_expire(redis_key)
            self._cache_register(media_id, checkpoint_name, redis_key)
        except Exception:
            self._cache_evict(redis_key, *fields)

    def _cache_write_stage(
        self,
        redis_key: str,
        stage: str,
        *,
        media_id: int | None = None,
        checkpoint_name: str | None = None,
    ) -> None:
        if self.cache is None:
            return
        try:
            self._cache_set(redis_key, "stage", stage)
            self._cache_expire(redis_key)
            self._cache_register(media_id, checkpoint_name, redis_key)
        except Exception:
            self._cache_evict(redis_key, "stage")

    def _cache_register(
        self,
        media_id: int | None,
        checkpoint_name: str | None,
        redis_key: str,
    ) -> None:
        if self.cache is None or media_id is None:
            return
        method = getattr(self.cache, "register_checkpoint_key", None)
        if callable(method):
            method(media_id, checkpoint_name or "", redis_key)
            return
        method = getattr(self.cache, "register_media_key", None)
        if callable(method):
            method(media_id, redis_key)

    def _cache_delete_prefix(
        self,
        media_id: int,
        checkpoint_prefix: str,
        redis_key: str | None,
    ) -> None:
        if self.cache is None:
            return
        try:
            method = getattr(self.cache, "delete_prefix", None)
            if callable(method):
                method(media_id, checkpoint_prefix)
            elif redis_key is not None:
                self._cache_delete_key(redis_key)
        except Exception:
            pass

    def _cache_delete_media(self, media_id: int) -> None:
        if self.cache is None:
            return
        try:
            method = getattr(self.cache, "delete_media", None)
            if callable(method):
                method(media_id)
        except Exception:
            pass


AgentCheckpointRepository = CheckpointRepository


__all__ = ["AgentCheckpointRepository", "CheckpointRepository"]
