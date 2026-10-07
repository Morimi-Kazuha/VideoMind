"""Phase 8B AgentCheckpointService/key-namespace acceptance tests."""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

import pytest

from dovideo.application import (
    AgentBudgetConfig,
    AgentCheckpointService,
    AgentLoopService,
    AnalysisTaskKeys,
    CheckpointService,
    InMemoryAgentBudgetUsage,
    TaskKey,
    goal_digest,
    normalize_content_hash,
)
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisMode,
    AnalysisEvidence,
    AnalysisResult,
    TaskStage,
    VideoChunk,
    VideoContext,
    VideoSegment,
)
from dovideo.infrastructure.persistence import (
    CheckpointRepository,
    InMemoryHotCheckpointCache,
    SqliteCheckpointStore,
)


def _context(goal: str = "goal") -> VideoContext:
    return VideoContext(
        source="video.mp4",
        userGoal=goal,
        segments=(VideoSegment(startMs=0, endMs=60_000, transcript="claim"),),
    )


def _plan(task: str = "task") -> AgentPlan:
    return AgentPlan(understoodGoal="goal", tasks=(task,))


def _result(claim: str = "claim") -> AnalysisResult:
    return AnalysisResult(
        title="title",
        conclusions=(claim,),
        evidence=(
            AnalysisEvidence(
                timestampMs=1_000,
                source="ASR",
                content="claim",
                claim=claim,
            ),
        ),
    )


@pytest.fixture()
def service(tmp_path: Path) -> tuple[AgentCheckpointService, SqliteCheckpointStore, InMemoryHotCheckpointCache]:
    db = SqliteCheckpointStore(tmp_path / "agent.sqlite3")
    cache = InMemoryHotCheckpointCache()
    return AgentCheckpointService(CheckpointRepository(db, cache)), db, cache


def test_java_key_helpers_match_golden_values() -> None:
    import hashlib

    trimmed = "a goal"
    assert goal_digest("  a goal  ") == hashlib.sha256(trimmed.encode("utf-8")).hexdigest()
    assert goal_digest("  a goal  ", None) == goal_digest("a goal", AnalysisMode.GENERAL)
    expected_learning = hashlib.sha256("LEARNING\u241fa goal".encode("utf-8")).hexdigest()
    assert goal_digest("a goal", AnalysisMode.LEARNING) == expected_learning
    assert normalize_content_hash(9, "ABCDEF0123456789ABCDEF0123456789") == "abcdef0123456789abcdef0123456789"
    assert normalize_content_hash(9, "not-md5") == "media-9"
    assert AnalysisTaskKeys.goalDigest("a goal", AnalysisMode.REVIEW) == goal_digest("a goal", AnalysisMode.REVIEW)
    assert AnalysisTaskKeys.active("media-9", "digest") == "analysis:active:media-9:digest"
    assert AnalysisTaskKeys.lock("media-9", "digest") == "lock:analysis:media-9:digest"
    assert AnalysisTaskKeys.completed("media-9", "digest") == "analysis:completed:media-9:digest"
    assert AnalysisTaskKeys.attempts("media-9", "digest") == "analysis:attempts:media-9:digest"
    assert AnalysisTaskKeys.contextOwner("media-9") == "analysis:context-owner:media-9"
    assert AnalysisTaskKeys.contextLock("media-9") == "lock:analysis-context:media-9"


@pytest.mark.asyncio
async def test_chunk_payload_incompatible_is_miss_but_storage_outage_propagates():
    from dovideo.infrastructure.persistence.errors import CheckpointDeserializationError, CheckpointReadError
    class Repository:
        error = CheckpointDeserializationError("invalid chunk payload")
        def read(self, *args):
            raise self.error
    repository = Repository()
    adapter = AgentCheckpointService(repository)
    assert await adapter.load_chunks(7) is None
    with pytest.raises(CheckpointDeserializationError):
        await adapter.load_context(7)
    repository.error = CheckpointReadError("database unavailable")
    with pytest.raises(CheckpointReadError):
        await adapter.load_chunks(7)


@pytest.mark.asyncio
async def test_invalid_durable_chunk_range_is_rebuildable(service):
    adapter, _db, _cache = service
    adapter.repository.write(7, "media:chunks", "media:stage", adapter.checkpoint_key(7),
                             "chunks", TaskStage.CHUNKS_COMPLETED,
                             [{"startTime": 100, "endTime": 50, "chunkingVersion": "old"}])
    assert await adapter.load_chunks(7) is None


@pytest.mark.asyncio
async def test_context_is_media_scoped_and_saved_goal_is_empty(service) -> None:  # type: ignore[no-untyped-def]
    adapter, db, _cache = service
    source = _context("first goal")
    await adapter.save_context(7, source)
    loaded = await adapter.load_context(7)
    assert loaded is not None
    assert loaded.source == source.source
    assert loaded.user_goal == ""
    assert loaded.segments == source.segments
    assert db.read(7, "media:context").stage == TaskStage.CONTEXT_COMPLETED.value


@pytest.mark.asyncio
async def test_chunks_are_tuple_copied_and_media_scoped(service) -> None:  # type: ignore[no-untyped-def]
    adapter, _db, _cache = service
    chunks = [VideoChunk(startTime=0, endTime=1000, segmentSummary="summary")]
    await adapter.save_chunks(7, chunks)  # type: ignore[arg-type]
    chunks.append(VideoChunk(startTime=1000, endTime=2000, segmentSummary="extra"))
    loaded = await adapter.load_chunks(7)
    assert loaded is not None and isinstance(loaded, tuple)
    assert len(loaded) == 1
    assert await adapter.load_chunks(8) is None


@pytest.mark.asyncio
async def test_goal_plan_critic_result_and_stage_methods_use_exact_names(service) -> None:  # type: ignore[no-untyped-def]
    adapter, db, cache = service
    key = TaskKey(7, "goal", AnalysisMode.LEARNING)
    plan = _plan()
    draft = AgentState(goal="goal", plan=plan, result=_result(), round=1)
    passed = AgentState(
        goal="goal",
        plan=plan,
        result=_result(),
        critique={"passed": True},
        round=1,
    )
    await adapter.save_plan(key, plan)
    assert await adapter.load_plan(key) == plan
    await adapter.save_execution_state(key, draft)
    assert await adapter.load_critic_state(key) == draft
    await adapter.save_critic_state(key, passed)
    assert (await adapter.load_critic_state(key)).critique.passed  # type: ignore[union-attr]
    await adapter.save_result(key, passed)
    assert await adapter.load_result(key) == passed
    await adapter.save_stage(key, TaskStage.ANALYSIS_COMPLETED)
    assert await adapter.load_stage(key) is TaskStage.ANALYSIS_COMPLETED

    digest = goal_digest("goal", AnalysisMode.LEARNING)
    names = {row.checkpoint_name for row in db.records(7)}
    assert f"goal:{digest}:plan" in names
    assert f"goal:{digest}:criticState" in names
    assert f"goal:{digest}:result" in names
    assert f"goal:{digest}:stage" in names
    assert cache.get_hash(f"agent:checkpoint:7:goal:{digest}", "result") is not None


@pytest.mark.asyncio
async def test_default_mode_and_modes_never_cross_goal_or_mode(service) -> None:  # type: ignore[no-untyped-def]
    adapter, db, _cache = service
    general = TaskKey(7, "same", AnalysisMode.GENERAL)
    learning = TaskKey(7, "same", AnalysisMode.LEARNING)
    other_goal = TaskKey(7, "other", AnalysisMode.GENERAL)
    await adapter.save_plan(general, _plan("general"))
    await adapter.save_plan(learning, _plan("learning"))
    await adapter.save_plan(other_goal, _plan("other"))
    assert (await adapter.load_plan(general)).tasks == ("general",)  # type: ignore[union-attr]
    assert (await adapter.load_plan(learning)).tasks == ("learning",)  # type: ignore[union-attr]
    assert (await adapter.load_plan(other_goal)).tasks == ("other",)  # type: ignore[union-attr]
    assert len(db.records(7)) == 6  # 3 payload rows + 3 shared-per-goal stage rows
    assert adapter.goal_key(7, "same", None) != adapter.goal_key(7, "same", AnalysisMode.LEARNING)


@pytest.mark.asyncio
async def test_cache_outage_still_reads_reopened_durable_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "reopen.sqlite3"
    first_db = SqliteCheckpointStore(path)
    first_cache = InMemoryHotCheckpointCache()
    first = AgentCheckpointService(CheckpointRepository(first_db, first_cache))
    key = TaskKey(3, "goal")
    await first.save_plan(key, _plan("persisted"))
    first_db.close()

    second_db = SqliteCheckpointStore(path)
    outage = InMemoryHotCheckpointCache(fail_reads=True, fail_writes=True)
    second = AgentCheckpointService(CheckpointRepository(second_db, outage))
    assert await second.load_plan(key) == _plan("persisted")
    assert await second.load_stage(key) is TaskStage.PLAN_COMPLETED
    second_db.close()


class _ContextFake:
    async def select_relevant(self, context: VideoContext, media_id: int | None = None) -> VideoContext:
        return context


class _PlannerFake:
    def __init__(self) -> None:
        self.plan_calls = 0

    async def plan(self, context: VideoContext, *, instruction: str = "") -> AgentPlan:
        self.plan_calls += 1
        return _plan()

    async def repair_plan(self, context, invalid_plan, *, instruction=""):  # type: ignore[no-untyped-def]
        return _plan()

    async def replan(self, context, current_plan, critique, *, instruction=""):  # type: ignore[no-untyped-def]
        return current_plan


class _ExecutorFake:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, context, plan, previous_critique=None, *, instruction=""):  # type: ignore[no-untyped-def]
        self.calls += 1
        return _result()


class _CriticFake:
    def __init__(self) -> None:
        self.calls = 0

    async def critique(self, context, plan, result, *, instruction=""):  # type: ignore[no-untyped-def]
        self.calls += 1
        return {"passed": True}


class _Telemetry:
    def __init__(self) -> None:
        self.counts = Counter()

    def increment(self, metric: str, amount: int = 1, **_kwargs: object) -> None:
        self.counts[metric] += amount


@pytest.mark.asyncio
async def test_agent_loop_budget_resume_uses_reopened_service_without_executor(tmp_path: Path) -> None:
    path = tmp_path / "loop.sqlite3"
    db = SqliteCheckpointStore(path)
    checkpoint = AgentCheckpointService(CheckpointRepository(db, InMemoryHotCheckpointCache()))
    planner = _PlannerFake()
    executor = _ExecutorFake()
    critic = _CriticFake()
    usage = InMemoryAgentBudgetUsage()
    telemetry = _Telemetry()
    loop = AgentLoopService(
        _ContextFake(), planner, executor, checkpoint, None, telemetry, critic,
        budget_config={"maxRounds": 1, "maxEstimatedTokens": 10},
        usage_source=usage,
    )

    original_execute = executor.execute

    async def execute_then_exhaust(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = await original_execute(*args, **kwargs)
        usage.record(estimated_tokens=11)
        return result

    executor.execute = execute_then_exhaust  # type: ignore[method-assign]
    with pytest.raises(AgentLoopService.BudgetExceededError):
        await loop.run(_context(), media_id=7)
    assert executor.calls == 1
    assert critic.calls == 0
    db.close()

    reopened = SqliteCheckpointStore(path)
    resumed_checkpoint = AgentCheckpointService(
        CheckpointRepository(reopened, InMemoryHotCheckpointCache())
    )
    resumed_planner = _PlannerFake()
    resumed_executor = _ExecutorFake()
    resumed_critic = _CriticFake()
    resumed = AgentLoopService(
        _ContextFake(), resumed_planner, resumed_executor, resumed_checkpoint,
        None, _Telemetry(), resumed_critic,
        budget_config={"maxRounds": 1, "maxEstimatedTokens": 10},
        usage_source=InMemoryAgentBudgetUsage(),
    )
    returned = await resumed.run(_context(), media_id=7)
    assert returned.critique is not None and returned.critique.passed
    assert resumed_executor.calls == 0
    assert resumed_critic.calls == 1
    reopened.close()
