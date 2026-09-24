"""Phase 8C lifecycle tests for the Java-compatible checkpoint service."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from dovideo.application import (
    AgentCheckpointService,
    AgentFeedbackPersistenceError,
)
from dovideo.domain import (
    AgentFeedback,
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    TaskStage,
)
from dovideo.application.value_objects import TaskKey
from dovideo.infrastructure.persistence import (
    CheckpointRepository,
    InMemoryHotCheckpointCache,
    SqliteCheckpointStore,
)


def _plan(task: str = "task") -> AgentPlan:
    return AgentPlan(understoodGoal="goal", tasks=(task,))


def _result() -> AnalysisResult:
    return AnalysisResult(
        title="title",
        conclusions=("conclusion",),
        evidence=(
            AnalysisEvidence(
                timestampMs=1_000,
                source="ASR",
                content="claim",
                claim="conclusion",
            ),
        ),
    )


def _state(plan: AgentPlan | None = None) -> AgentState:
    return AgentState(
        goal="goal",
        plan=plan or _plan(),
        result=_result(),
        critique={"passed": True},
        round=1,
    )


@pytest.fixture()
def service(tmp_path: Path):  # type: ignore[no-untyped-def]
    db = SqliteCheckpointStore(tmp_path / "agent.sqlite3")
    cache = InMemoryHotCheckpointCache()
    adapter = AgentCheckpointService(CheckpointRepository(db, cache))
    return adapter, db, cache


def test_feedback_validation_and_java_normalization() -> None:
    clock_value = datetime(2026, 1, 2, tzinfo=timezone.utc)
    feedback = AgentFeedback(
        mediaId=7,
        goal="  summarize the video  ",
        mode=None,
        errorType="  EVIDENCE  ",
        comment="  needs another timestamp  ",
        correctedGoal="  corrected goal  ",
        correctedTasks=("  first  ", " ", None, "second"),
        evidenceTimestamp=0,
    )

    normalized = feedback.normalized(clock=lambda: clock_value)
    assert feedback.goal == "  summarize the video  "
    assert normalized.goal == "summarize the video"
    assert normalized.mode == AnalysisMode.GENERAL.name
    assert normalized.error_type == "EVIDENCE"
    assert normalized.comment == "needs another timestamp"
    assert normalized.corrected_goal == "corrected goal"
    assert normalized.corrected_tasks == ("first", "second")
    assert normalized.created_at == clock_value


def test_feedback_uses_java_utf16_limits() -> None:
    supplementary_250 = "\U0001F600" * 250  # 500 Java UTF-16 code units
    AgentFeedback(mediaId=1, goal=supplementary_250, correctedTasks=(supplementary_250,))
    with pytest.raises(ValueError):
        AgentFeedback(mediaId=1, goal=supplementary_250 + "\U0001F600")
    with pytest.raises(ValueError):
        AgentFeedback(mediaId=1, goal="goal", correctedTasks=(supplementary_250 + "x",))
    with pytest.raises(ValueError):
        AgentFeedback(mediaId=1, goal="goal", correctedTasks=("x",) * 6)


@pytest.mark.asyncio
async def test_feedback_hot_list_caps_skips_malformed_and_expires(service) -> None:  # type: ignore[no-untyped-def]
    adapter, _db, cache = service
    feedback = AgentFeedback(mediaId=7, goal="goal", correctedTasks=("task",))
    for _ in range(205):
        await adapter.save_feedback(feedback)

    key = adapter.feedback_key(7)
    cache.right_push(key, "{ malformed feedback")
    loaded = await adapter.load_feedback(7)
    assert len(loaded) == 200
    assert all(item.media_id == 7 for item in loaded)
    assert 0 < cache.ttl(key) <= 30 * 24 * 60 * 60


@pytest.mark.asyncio
async def test_feedback_cache_failure_is_typed(service) -> None:  # type: ignore[no-untyped-def]
    adapter, db, cache = service
    cache.fail_writes = True
    with pytest.raises(AgentFeedbackPersistenceError):
        await adapter.save_feedback(AgentFeedback(mediaId=7, goal="goal"))
    assert db.records(7) == ()


@pytest.mark.asyncio
async def test_revision_begin_clears_goal_state_then_applies_plan_and_is_idempotent(service) -> None:  # type: ignore[no-untyped-def]
    adapter, db, _cache = service
    key = TaskKey(7, "goal", AnalysisMode.LEARNING)
    await adapter.save_plan(key, _plan("old"))
    await adapter.save_result(key, _state(_plan("old")))
    await adapter.stage_revision(7, "goal", _plan("new"), AnalysisMode.LEARNING)

    assert await adapter.begin_staged_revision(7, "goal", AnalysisMode.LEARNING)
    assert (await adapter.load_plan(key)).tasks == ("new",)  # type: ignore[union-attr]
    assert await adapter.load_result(key) is None
    assert await adapter.load_stage(key) is TaskStage.PLAN_COMPLETED
    revision = db.read(7, adapter.revision_checkpoint("goal", AnalysisMode.LEARNING))
    assert revision is not None and revision.stage == TaskStage.REVISION_APPLIED.value
    assert await adapter.begin_staged_revision(7, "goal", AnalysisMode.LEARNING)


@pytest.mark.asyncio
async def test_revision_missing_cancel_complete_and_mode_isolation(service) -> None:  # type: ignore[no-untyped-def]
    adapter, db, _cache = service
    assert not await adapter.begin_staged_revision(8, "goal", AnalysisMode.LEARNING)
    learning = TaskKey(8, "goal", AnalysisMode.LEARNING)
    review = TaskKey(8, "goal", AnalysisMode.REVIEW)
    await adapter.save_plan(learning, _plan("learning"))
    await adapter.save_plan(review, _plan("review"))
    await adapter.stage_revision(8, "goal", _plan("revised"), AnalysisMode.LEARNING)
    assert await adapter.begin_staged_revision(8, "goal", AnalysisMode.LEARNING)
    assert (await adapter.load_plan(review)).tasks == ("review",)  # type: ignore[union-attr]
    await adapter.stage_revision(8, "goal", _plan("cancelled"), AnalysisMode.LEARNING)
    await adapter.cancel_staged_revision(8, "goal", AnalysisMode.LEARNING)
    assert await adapter.begin_staged_revision(8, "goal", AnalysisMode.LEARNING) is False
    await adapter.stage_revision(8, "goal", _plan("completed"), AnalysisMode.LEARNING)
    await adapter.complete_staged_revision(8, "goal", AnalysisMode.LEARNING)
    assert db.read(8, adapter.revision_checkpoint("goal", AnalysisMode.LEARNING)) is None


@pytest.mark.asyncio
async def test_revision_stage_can_be_retried_after_persist_failure(service) -> None:  # type: ignore[no-untyped-def]
    _adapter, db, cache = service

    class FailOnceRepository:
        def __init__(self) -> None:
            self.cache = cache
            self._failed = False

        def write_standalone(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if not self._failed:
                self._failed = True
                raise OSError("transient durable failure")
            return CheckpointRepository(db, cache).write_standalone(*args, **kwargs)

        def __getattr__(self, name: str):
            return getattr(CheckpointRepository(db, cache), name)

    retryable = AgentCheckpointService(FailOnceRepository())
    with pytest.raises(Exception):
        await retryable.stage_revision(9, "goal", _plan("revised"))
    await retryable.stage_revision(9, "goal", _plan("revised"))
    assert await retryable.begin_staged_revision(9, "goal")


@pytest.mark.asyncio
async def test_failure_persists_durable_stage_and_safe_cache_metadata(service) -> None:  # type: ignore[no-untyped-def]
    adapter, db, cache = service
    error = RuntimeError("secret token should not be persisted")
    await adapter.save_failure(7, "goal", TaskStage.EXECUTOR_STARTED, error)
    digest_key = adapter.goal_key(7, "goal")
    row = db.read(7, adapter.goal_checkpoint("goal", None, "stage"))
    assert row is not None and row.stage == TaskStage.FAILED.value
    assert cache.snapshot(digest_key) == {
        "stage": TaskStage.FAILED.value,
        "failedStage": TaskStage.EXECUTOR_STARTED.value,
        "errorType": "RuntimeError",
    }
    assert "secret" not in repr(cache.snapshot(digest_key))
    assert 0 < cache.ttl(digest_key) <= 7 * 24 * 60 * 60


@pytest.mark.asyncio
async def test_failure_cache_outage_does_not_break_durable_stage(service) -> None:  # type: ignore[no-untyped-def]
    adapter, db, cache = service
    cache.fail_writes = True
    await adapter.save_failure(7, "goal", TaskStage.CRITIC_STARTED, ValueError("bad"))
    row = db.read(7, adapter.goal_checkpoint("goal", None, "stage"))
    assert row is not None and row.stage == TaskStage.FAILED.value


@pytest.mark.asyncio
async def test_delete_media_removes_durable_rows_and_known_cache_keys(service) -> None:  # type: ignore[no-untyped-def]
    adapter, db, cache = service
    await adapter.save_plan(TaskKey(7, "goal", AnalysisMode.LEARNING), _plan())
    await adapter.save_feedback(AgentFeedback(mediaId=7, goal="goal"))
    await adapter.stage_revision(7, "goal", _plan("revised"), AnalysisMode.LEARNING)
    await adapter.delete_media(7)
    assert db.records(7) == ()
    assert cache.snapshot(adapter.goal_key(7, "goal", AnalysisMode.LEARNING)) == {}
    assert cache.list_snapshot(adapter.feedback_key(7)) == []
    assert cache.set_snapshot(adapter.goal_index_key(7)) == set()


@pytest.mark.asyncio
async def test_delete_media_survives_cache_delete_outage(service) -> None:  # type: ignore[no-untyped-def]
    adapter, db, cache = service
    await adapter.save_plan(TaskKey(7, "goal"), _plan())
    cache.fail_deletes = True
    await adapter.delete_media(7)
    assert db.records(7) == ()
