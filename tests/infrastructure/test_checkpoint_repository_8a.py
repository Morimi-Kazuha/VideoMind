"""Offline acceptance tests for the Phase 8A checkpoint repository core."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisResult,
    VideoChunk,
    VideoContext,
    VideoSegment,
)
from dovideo.domain.tasks import TaskStage
from dovideo.infrastructure.persistence import (
    CheckpointDeserializationError,
    CheckpointReadError,
    CheckpointRepository,
    CheckpointVersionMismatchError,
    CheckpointWriteError,
    InMemoryHotCheckpointCache,
    JsonCheckpointCodec,
    SqliteCheckpointStore,
)


def _plan(name: str = "task") -> AgentPlan:
    return AgentPlan(understoodGoal="goal", tasks=(name,))


def _state() -> AgentState:
    return AgentState(
        goal="goal",
        plan=_plan(),
        result=AnalysisResult(
            title="title",
            conclusions=("conclusion",),
            evidence=(AnalysisEvidence(timestampMs=1000, source="ASR", content="x"),),
        ),
    )


def _repo(
    *,
    db: SqliteCheckpointStore | None = None,
    cache: InMemoryHotCheckpointCache | None = None,
    codec: JsonCheckpointCodec | None = None,
) -> tuple[CheckpointRepository, SqliteCheckpointStore, InMemoryHotCheckpointCache]:
    store = db or SqliteCheckpointStore()
    hot = cache or InMemoryHotCheckpointCache()
    return CheckpointRepository(store, hot, codec or JsonCheckpointCodec()), store, hot


def test_cache_hit_avoids_second_durable_read() -> None:
    class CountingStore(SqliteCheckpointStore):
        reads = 0

        def read(self, media_id: int, checkpoint_name: str):  # type: ignore[no-untyped-def]
            self.reads += 1
            return super().read(media_id, checkpoint_name)

    db = CountingStore()
    repo, _db, cache = _repo(db=db)
    repo.write_standalone(7, "plan", "agent:7", "plan", TaskStage.PLAN_COMPLETED, _plan())
    assert repo.read(7, "plan", "agent:7", "plan", AgentPlan) == _plan()
    assert repo.read(7, "plan", "agent:7", "plan", AgentPlan) == _plan()
    assert db.reads == 0
    assert cache.get_hash("agent:7", "plan") is not None


def test_cache_miss_reads_durable_and_warms_cache() -> None:
    repo, db, cache = _repo()
    payload = repo.codec.encode(_plan("from-db"))
    db.upsert(1, "plan", TaskStage.PLAN_COMPLETED, payload)
    assert repo.read(1, "plan", "key", "plan", AgentPlan).tasks == ("from-db",)
    assert cache.get_hash("key", "plan") == payload
    assert cache.get_hash("key", "stage") == TaskStage.PLAN_COMPLETED.value


def test_malformed_cached_payload_is_evicted_then_falls_back() -> None:
    repo, db, cache = _repo()
    payload = repo.codec.encode(_plan())
    db.upsert(2, "plan", TaskStage.PLAN_COMPLETED, payload)
    cache.set_hash("key", "plan", "not-json")
    assert repo.read(2, "plan", "key", "plan", AgentPlan) == _plan()
    assert cache.get_hash("key", "plan") == payload


def test_malformed_cached_stage_is_evicted_then_falls_back() -> None:
    repo, db, cache = _repo()
    db.upsert(3, "stage", TaskStage.CRITIC_PASSED, None)
    cache.set_hash("key", "stage", "old-stage")
    assert repo.read_stage(3, "stage", "key") is TaskStage.CRITIC_PASSED
    assert cache.get_hash("key", "stage") == TaskStage.CRITIC_PASSED.value


def test_cache_read_and_write_outage_does_not_break_durable_flow() -> None:
    cache = InMemoryHotCheckpointCache(fail_reads=True, fail_writes=True)
    repo, db, _ = _repo(cache=cache)
    repo.write_standalone(4, "plan", "key", "plan", TaskStage.PLAN_COMPLETED, _plan())
    assert db.read(4, "plan") is not None
    assert repo.read(4, "plan", "key", "plan", AgentPlan) == _plan()


def test_cache_delete_outage_does_not_break_durable_delete() -> None:
    cache = InMemoryHotCheckpointCache(fail_deletes=True)
    repo, db, _ = _repo(cache=cache)
    repo.write_standalone(5, "plan", "key", "plan", TaskStage.PLAN_COMPLETED, _plan())
    repo.delete(5, "plan", "key")
    assert db.read(5, "plan") is None


def test_durable_failure_is_typed_and_cache_is_not_touched() -> None:
    class BrokenStore:
        def upsert(self, *_args):
            raise OSError("disk unavailable")

        def read(self, *_args):
            return None

    cache = InMemoryHotCheckpointCache()
    repo = CheckpointRepository(BrokenStore(), cache)
    with pytest.raises(CheckpointWriteError):
        repo.write_standalone(6, "plan", "key", "plan", TaskStage.PLAN_COMPLETED, _plan())
    assert cache.snapshot("key") == {}


def test_durable_commit_happens_before_cache_write() -> None:
    class OrderedStore:
        def __init__(self, hot):
            self.hot = hot
            self.rows = {}
            self.observations = []

        def upsert(self, media_id, name, stage, payload):
            self.rows[(media_id, name)] = (stage, payload)
            self.observations.append(dict(self.hot.snapshot("key")))

        def read(self, media_id, name):
            row = self.rows.get((media_id, name))
            if row is None:
                return None
            from dovideo.infrastructure.persistence import CheckpointRecord

            return CheckpointRecord(media_id, name, *row)

    cache = InMemoryHotCheckpointCache()
    store = OrderedStore(cache)
    repo = CheckpointRepository(store, cache)
    repo.write_standalone(8, "plan", "key", "plan", TaskStage.PLAN_COMPLETED, _plan())
    assert store.observations == [{}]
    assert cache.get_hash("key", "plan") is not None


def test_stage_only_and_payload_plus_stage_paths() -> None:
    repo, db, cache = _repo()
    repo.write_stage(9, "stage", "key", TaskStage.EXECUTOR_COMPLETED)
    assert repo.read_stage(9, "stage", "key") is TaskStage.EXECUTOR_COMPLETED
    repo.write_standalone(9, "plan", "key", "plan", TaskStage.PLAN_COMPLETED, _plan())
    row = db.read(9, "plan")
    assert row is not None and row.payload is not None
    assert cache.get_hash("key", "stage") == TaskStage.PLAN_COMPLETED.value


def test_prefix_and_media_delete() -> None:
    repo, db, cache = _repo()
    repo.write_standalone(10, "goal:a", "key-a", "a", TaskStage.PLAN_COMPLETED, _plan("a"))
    repo.write_standalone(10, "goal:b", "key-b", "b", TaskStage.PLAN_COMPLETED, _plan("b"))
    repo.write_standalone(10, "other", "key-c", "c", TaskStage.PLAN_COMPLETED, _plan("c"))
    repo.delete_prefix(10, "goal:")
    assert db.read(10, "goal:a") is None
    assert db.read(10, "goal:b") is None
    assert db.read(10, "other") is not None
    repo.delete_media(10)
    assert db.records(10) == ()
    assert cache.snapshot("key-c") == {}


def test_sqlite_file_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    codec = JsonCheckpointCodec()
    first = SqliteCheckpointStore(path)
    first.upsert(11, "plan", TaskStage.PLAN_COMPLETED, codec.encode(_plan("persisted")))
    first.close()
    second = SqliteCheckpointStore(path)
    assert second.read(11, "plan").payload == codec.encode(_plan("persisted"))
    second.close()


def test_version_mismatch_is_cache_miss_and_evicts_old_field() -> None:
    old = JsonCheckpointCodec(prompt_version="old")
    current = JsonCheckpointCodec(prompt_version="current")
    db = SqliteCheckpointStore()
    cache = InMemoryHotCheckpointCache()
    repo = CheckpointRepository(db, cache, current)
    db.upsert(12, "plan", TaskStage.PLAN_COMPLETED, current.encode(_plan("new")))
    cache.set_hash("key", "plan", old.encode(_plan("old")))
    assert repo.read(12, "plan", "key", "plan", AgentPlan).tasks == ("new",)
    assert cache.get_hash("key", "plan") == current.encode(_plan("new"))


def test_version_mismatch_in_durable_payload_is_typed() -> None:
    old = JsonCheckpointCodec(prompt_version="old")
    repo, db, _ = _repo(codec=JsonCheckpointCodec(prompt_version="current"))
    db.upsert(13, "plan", TaskStage.PLAN_COMPLETED, old.encode(_plan()))
    with pytest.raises(CheckpointVersionMismatchError):
        repo.read(13, "plan", "key", "plan", AgentPlan)


def test_alias_json_roundtrip_for_agent_and_video_models() -> None:
    codec = JsonCheckpointCodec()
    context = VideoContext(
        source="video.mp4",
        userGoal="goal",
        segments=(VideoSegment(startMs=0, endMs=1000, transcript="hello"),),
    )
    chunk = VideoChunk(
        startTime=0,
        endTime=1000,
        segmentSummary="summary",
        rawSegments=context.segments,
    )
    for value, target in (
        (_plan(), AgentPlan),
        (_state(), AgentState),
        (context, VideoContext),
        (chunk, VideoChunk),
    ):
        encoded = codec.encode(value)
        assert "understoodGoal" in encoded or "userGoal" in encoded or "startTime" in encoded or "goal" in encoded
        assert codec.decode(encoded, target) == value


def test_basic_concurrent_upserts_and_reads() -> None:
    db = SqliteCheckpointStore(":memory:")
    codec = JsonCheckpointCodec()

    def put(index: int) -> None:
        db.upsert(index, "plan", TaskStage.PLAN_COMPLETED, codec.encode(_plan(str(index))))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(put, range(32)))
    assert len(db.records()) == 32


def test_connection_injection_uses_the_supplied_sqlite_connection() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    store = SqliteCheckpointStore(connection=connection)
    store.upsert(14, "stage", TaskStage.CRITIC_PASSED, None)
    assert store.read(14, "stage").stage == TaskStage.CRITIC_PASSED.value
    store.close()
    connection.close()

