"""Offline Phase 8D adapter tests (DB-API fakes, Redis fake, SQLite)."""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dovideo.application import MediaRecord, MediaStatus
from dovideo.infrastructure.media import InMemoryObjectStorage, MediaIngestService
from dovideo.infrastructure.persistence import (
    CheckpointRepository,
    CheckpointWriteError,
    InMemoryHotCheckpointCache,
    MySqlCheckpointStore,
    MySqlMediaRecordRepository,
    RedisCheckpointCache,
    SqliteMediaRecordRepository,
)
from dovideo.infrastructure.persistence.models import CheckpointRecord


def _record(media_id: int | None = None, *, source: str = "memory://video") -> MediaRecord:
    return MediaRecord(
        media_id=media_id,
        user_id=7,
        filename="video.mp4",
        source=source,
        content_hash="ABCDEF",
        status=MediaStatus.COMPLETED,
        uploaded_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        content_type="video/mp4",
    )


class _FakeDb:
    def __init__(self) -> None:
        self.checkpoints: dict[tuple[int, str], tuple[str, str | None]] = {}
        self.media: dict[int, tuple[int, str, str, str, str | None, str, object]] = {}
        self.connections: list[_FakeConnection] = []
        self.next_media_id = 40
        self.fail_next = False

    def connect(self) -> "_FakeConnection":
        connection = _FakeConnection(self)
        self.connections.append(connection)
        return connection


class _FakeConnection:
    def __init__(self, database: _FakeDb) -> None:
        self.database = database
        self.before_checkpoints = copy.deepcopy(database.checkpoints)
        self.before_media = copy.deepcopy(database.media)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> "_FakeCursor":
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        self.database.checkpoints = self.before_checkpoints
        self.database.media = self.before_media

    def close(self) -> None:
        self.closed = True


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.database = connection.database
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.result: list[tuple[object, ...]] = []
        self.lastrowid: int | None = None
        self.closed = False

    def execute(self, sql: str, params=()) -> None:  # type: ignore[no-untyped-def]
        values = tuple(params or ())
        self.calls.append((sql, values))
        compact = " ".join(sql.split()).lower()
        if self.database.fail_next:
            self.database.fail_next = False
            raise OSError("database secret should not escape")
        self.result = []
        if "select media_id, checkpoint_key" in compact:
            media_id, checkpoint_key = values
            row = self.database.checkpoints.get((int(media_id), str(checkpoint_key)))
            self.result = [] if row is None else [(media_id, checkpoint_key, row[0], row[1], "2026-01-01T00:00:00Z")]
            return
        if compact.startswith("select last_insert_id"):
            self.result = [(self.database.next_media_id,)]
            return
        if compact.startswith("select id, user_id") and "from media_files" in compact:
            media_id = int(values[0])
            row = self.database.media.get(media_id)
            self.result = [] if row is None else [(row[0], *row[1:])]
            return
        if compact.startswith("insert into agent_checkpoints"):
            media_id, key, stage, payload = values
            self.database.checkpoints[(int(media_id), str(key))] = (str(stage), payload)
            return
        if compact.startswith("insert into media_files"):
            if compact.startswith("insert into media_files (id,"):
                media_id, user_id, filename, status, source, content_hash, uploaded_at = values
            else:
                user_id, filename, status, source, content_hash, uploaded_at = values
                self.database.next_media_id += 1
                media_id = self.database.next_media_id
            self.database.media[int(media_id)] = (
                int(media_id), str(user_id), str(filename), str(status), str(source),
                None if content_hash is None else str(content_hash), uploaded_at,
            )
            self.lastrowid = int(media_id)
            return
        if compact.startswith("delete from agent_checkpoints"):
            if "checkpoint_key =" in compact:
                media_id, key = values
                self.database.checkpoints.pop((int(media_id), str(key)), None)
            elif "like concat" in compact:
                media_id, prefix = values
                for key in tuple(self.database.checkpoints):
                    if key[0] == int(media_id) and key[1].startswith(str(prefix)):
                        self.database.checkpoints.pop(key, None)
            else:
                media_id = int(values[0])
                for key in tuple(self.database.checkpoints):
                    if key[0] == media_id:
                        self.database.checkpoints.pop(key, None)
            return
        if compact.startswith("delete from media_files"):
            self.database.media.pop(int(values[0]), None)
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):  # type: ignore[no-untyped-def]
        return self.result[0] if self.result else None

    def fetchall(self):  # type: ignore[no-untyped-def]
        return list(self.result)

    def close(self) -> None:
        self.closed = True


class _FakeRedis:
    def __init__(self, *, bytes_mode: bool = True) -> None:
        self.hashes: dict[str, dict[str, object]] = {}
        self.lists: dict[str, list[object]] = {}
        self.sets: dict[str, set[object]] = {}
        self.expirations: dict[str, int] = {}
        self.bytes_mode = bytes_mode
        self.fail = False
        self.deleted: list[str] = []

    def _maybe_fail(self) -> None:
        if self.fail:
            raise RuntimeError("redis credential must not escape")

    def _out(self, value):  # type: ignore[no-untyped-def]
        if self.bytes_mode and isinstance(value, str):
            return value.encode()
        return value

    def hget(self, name, key):  # type: ignore[no-untyped-def]
        self._maybe_fail()
        return self._out(self.hashes.get(name, {}).get(str(key)))

    def hset(self, name, key, value):  # type: ignore[no-untyped-def]
        self._maybe_fail()
        self.hashes.setdefault(name, {})[str(key)] = value
        return 1

    def hdel(self, name, *keys):  # type: ignore[no-untyped-def]
        self._maybe_fail()
        for key in keys:
            self.hashes.get(name, {}).pop(str(key), None)
        return len(keys)

    def hgetall(self, name):  # type: ignore[no-untyped-def]
        self._maybe_fail()
        return {self._out(key): self._out(value) for key, value in self.hashes.get(name, {}).items()}

    def delete(self, *names):  # type: ignore[no-untyped-def]
        self._maybe_fail()
        for name in names:
            self.deleted.append(name)
            self.hashes.pop(name, None)
            self.lists.pop(name, None)
            self.sets.pop(name, None)
            self.expirations.pop(name, None)
        return len(names)

    def expire(self, name, ttl):  # type: ignore[no-untyped-def]
        self._maybe_fail()
        self.expirations[name] = int(ttl)
        return True

    def rpush(self, name, value):  # type: ignore[no-untyped-def]
        self._maybe_fail()
        self.lists.setdefault(name, []).append(value)
        return len(self.lists[name])

    def ltrim(self, name, start, stop):  # type: ignore[no-untyped-def]
        self._maybe_fail()
        values = self.lists.get(name, [])
        first = max(0, len(values) + start if start < 0 else start)
        last = min(len(values) - 1, len(values) + stop if stop < 0 else stop)
        self.lists[name] = values[first : last + 1] if first <= last else []
        return True

    def lrange(self, name, start, stop):  # type: ignore[no-untyped-def]
        self._maybe_fail()
        values = self.lists.get(name, [])
        first = max(0, len(values) + start if start < 0 else start)
        last = min(len(values) - 1, len(values) + stop if stop < 0 else stop)
        return [self._out(value) for value in values[first : last + 1]] if first <= last else []

    def sadd(self, name, *values):  # type: ignore[no-untyped-def]
        self._maybe_fail()
        target = self.sets.setdefault(name, set())
        before = len(target)
        target.update(values)
        return len(target) - before

    def smembers(self, name):  # type: ignore[no-untyped-def]
        self._maybe_fail()
        return {self._out(value) for value in self.sets.get(name, set())}

    def ttl(self, name):  # type: ignore[no-untyped-def]
        return self.expirations.get(name, -1)


def test_mysql_checkpoint_store_is_parameterized_transactional_and_closes_factory_connections() -> None:
    database = _FakeDb()
    store = MySqlCheckpointStore(database.connect)
    store.upsert(7, "goal:plan", "PLAN_COMPLETED", '{"ok":true}')
    assert store.read(7, "goal:plan").payload == '{"ok":true}'  # type: ignore[union-attr]
    # Every SQL write receives a separate parameter tuple; no value is
    # interpolated into the statement.
    write_connection = database.connections[0]
    assert write_connection.commits == 1
    assert write_connection.closed
    database.fail_next = True
    with pytest.raises(CheckpointWriteError) as caught:
        store.upsert(7, "secret-key", "PLAN_COMPLETED", "secret-payload")
    assert caught.value.__cause__ is not None
    assert "secret" not in str(caught.value)
    assert database.connections[-1].rollbacks == 1
    assert database.connections[-1].closed


def test_mysql_checkpoint_store_prefix_and_media_deletes_are_parameterized() -> None:
    database = _FakeDb()
    store = MySqlCheckpointStore(database.connect)
    store.upsert(7, "goal:a", "A", "a")
    store.upsert(7, "goal:b", "B", "b")
    store.upsert(7, "other", "C", "c")
    store.delete_prefix(7, "goal:")
    assert store.read(7, "goal:a") is None
    assert store.read(7, "other") is not None
    store.delete_media(7)
    assert store.read(7, "other") is None


def test_redis_checkpoint_cache_decodes_bytes_and_tracks_indexes() -> None:
    client = _FakeRedis()
    cache = RedisCheckpointCache(client)
    cache.set_hash("key", "field", "value")
    assert cache.get_hash("key", "field") == "value"
    cache.right_push("feedback", "one")
    cache.right_push("feedback", "two")
    assert cache.list_range("feedback") == ["one", "two"]
    cache.set_add("set", "a")
    assert cache.set_members("set") == {"a"}
    cache.register_checkpoint_key(7, "goal:plan", "goal-key")
    assert "goal-key" in cache.set_members("agent:checkpoint:7:keys")
    cache.delete_media(7)
    assert "goal-key" in client.deleted


@pytest.mark.asyncio
async def test_repository_with_redis_adapter_keeps_durable_checkpoint_when_cache_is_down() -> None:
    database = _FakeDb()
    durable = MySqlCheckpointStore(database.connect)
    redis = _FakeRedis()
    redis_cache = RedisCheckpointCache(redis)
    repository = CheckpointRepository(durable, redis_cache)
    repository.upsert(7, "plan", "PLAN_COMPLETED", '{"payload":1}', redis_key="key", field="plan")
    redis.fail = True
    assert repository.read_payload(7, "plan", "key", "plan") == '{"payload":1}'


@pytest.mark.asyncio
async def test_sqlite_media_reopen_assignment_upsert_delete_and_media_ref(tmp_path: Path) -> None:
    path = tmp_path / "media.sqlite3"
    repository = SqliteMediaRecordRepository(path)
    saved = await repository.save(_record())
    assert saved.media_id is not None
    assert (await repository.get(saved.media_id)) == saved
    assert (await repository.get_media(saved.media_id)).source == saved.source  # type: ignore[union-attr]
    explicit = await repository.save(_record(saved.media_id, source="memory://updated"))
    assert explicit.source == "memory://updated"
    repository.close()

    reopened = SqliteMediaRecordRepository(path)
    assert (await reopened.get(saved.media_id)).source == "memory://updated"  # type: ignore[union-attr]
    assert (await reopened.get(saved.media_id)).content_type == "video/mp4"  # type: ignore[union-attr]
    await reopened.delete(saved.media_id)
    assert await reopened.get(saved.media_id) is None
    reopened.close()


@pytest.mark.asyncio
async def test_sqlite_media_repository_is_safe_for_concurrent_saves() -> None:
    repository = SqliteMediaRecordRepository()
    saved = await asyncio.gather(*(repository.save(_record()) for _ in range(16)))
    assert len({record.media_id for record in saved}) == 16
    repository.close()


@pytest.mark.asyncio
async def test_mysql_media_mapping_roundtrip_and_rollback() -> None:
    database = _FakeDb()
    repository = MySqlMediaRecordRepository(database.connect)
    saved = await repository.save(_record())
    assert saved.media_id == 41
    loaded = await repository.get(saved.media_id)
    assert loaded is not None
    assert loaded.source == "memory://video"
    assert loaded.content_hash == "abcdef"
    assert loaded.content_type is None  # V1 has no content_type column.
    await repository.save(_record(saved.media_id, source="memory://updated"))
    assert (await repository.get(saved.media_id)).source == "memory://updated"  # type: ignore[union-attr]
    database.fail_next = True
    with pytest.raises(Exception):
        await repository.save(_record())
    assert database.connections[-1].rollbacks == 1
    await repository.delete(saved.media_id)
    assert await repository.get(saved.media_id) is None


@pytest.mark.asyncio
async def test_phase3c_ingest_uses_sqlite_media_repository_and_rolls_object_back() -> None:
    objects = InMemoryObjectStorage()
    records = SqliteMediaRecordRepository()
    ingester = MediaIngestService(objects, records)
    saved = await ingester.ingest_file(b"payload", 7, filename="video.mp4")
    assert saved.media_id is not None
    assert await records.get(saved.media_id) == saved
    assert len(objects.objects) == 1

    class BrokenRepository(SqliteMediaRecordRepository):
        def _save_sync(self, record):  # type: ignore[no-untyped-def]
            raise RuntimeError("record write failed")

    broken_objects = InMemoryObjectStorage()
    with pytest.raises(RuntimeError, match="record write failed"):
        await MediaIngestService(broken_objects, BrokenRepository()).ingest_file(
            b"payload", 7, filename="video.mp4"
        )
    assert broken_objects.objects == {}
    records.close()
