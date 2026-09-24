"""Phase 8E recovery integration over the real SQLite checkpoint stack.

These tests intentionally exercise the application loop through the durable
``AgentCheckpointService`` instead of replacing it with a unit-test fake.  A
small set of fake model roles makes process-boundary and call-count assertions
deterministic while keeping Phase 9 task-worker state out of scope.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

import pytest

from dovideo.application import (
    AgentCheckpointService,
    AgentLoopService,
    TaskKey,
)
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    CriticResult,
    ModeProfile,
    TaskStage,
    VideoChunk,
    VideoContext,
    VideoSegment,
)
from dovideo.infrastructure.persistence import (
    CheckpointRepository,
    CheckpointVersionMismatchError,
    InMemoryHotCheckpointCache,
    JsonCheckpointCodec,
    SqliteCheckpointStore,
)


def _context(goal: str = "goal", transcript: str = "claim") -> VideoContext:
    return VideoContext(
        source="video.mp4",
        userGoal=goal,
        segments=(
            VideoSegment(startMs=0, endMs=60_000, transcript=transcript),
        ),
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


class _ContextRole:
    def __init__(self, *, fail_refine: bool = False) -> None:
        self.select_calls = 0
        self.refine_calls = 0
        self.fail_refine = fail_refine

    async def select_relevant(
        self,
        context: VideoContext,
        media_id: int | None = None,
    ) -> VideoContext:
        del media_id
        self.select_calls += 1
        return context

    async def refine_for_critique(
        self,
        media_id: int | None,
        full_context: VideoContext,
        selected_context: VideoContext,
        critique: CriticResult,
    ) -> VideoContext:
        del media_id, critique
        self.refine_calls += 1
        if self.fail_refine:
            self.fail_refine = False
            raise RuntimeError("simulated process interruption")
        return selected_context or full_context


class _PlannerRole:
    def __init__(
        self,
        *,
        planned: AgentPlan | None = None,
        replanned: AgentPlan | None = None,
    ) -> None:
        self.planned = planned or _plan()
        self.replanned = replanned or _plan("replanned")
        self.plan_calls = 0
        self.replan_calls = 0

    async def plan(self, context: VideoContext, *, instruction: str = "") -> AgentPlan:
        del context, instruction
        self.plan_calls += 1
        return self.planned

    async def repair_plan(
        self,
        context: VideoContext,
        invalid_plan: AgentPlan,
        *,
        instruction: str = "",
    ) -> AgentPlan:
        del context, invalid_plan, instruction
        return self.planned

    async def replan(
        self,
        context: VideoContext,
        current_plan: AgentPlan,
        critique: CriticResult,
        *,
        instruction: str = "",
    ) -> AgentPlan:
        del context, current_plan, critique, instruction
        self.replan_calls += 1
        return self.replanned


class _ExecutorRole:
    def __init__(
        self,
        *,
        result: AnalysisResult | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.result = result or _result()
        self.failure = failure
        self.calls = 0

    async def execute(
        self,
        context: VideoContext,
        plan: AgentPlan,
        previous_critique: CriticResult | None = None,
        *,
        instruction: str = "",
    ) -> AnalysisResult:
        del context, plan, previous_critique, instruction
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.result


class _CriticRole:
    def __init__(self, *critiques: CriticResult | None) -> None:
        self.critiques = list(critiques) or [CriticResult(passed=True)]
        self.calls = 0

    async def critique(
        self,
        context: VideoContext,
        plan: AgentPlan,
        result: AnalysisResult | None,
        *,
        instruction: str = "",
    ) -> CriticResult | None:
        del context, plan, result, instruction
        self.calls += 1
        if len(self.critiques) > 1:
            return self.critiques.pop(0)
        return self.critiques[0]


class _Telemetry:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def increment(self, metric: str, amount: int = 1, **_: object) -> None:
        self.counts[metric] += amount


def _stack(
    path: Path,
    *,
    cache: InMemoryHotCheckpointCache | None = None,
    codec: JsonCheckpointCodec | None = None,
    context: _ContextRole | None = None,
    planner: _PlannerRole | None = None,
    executor: _ExecutorRole | None = None,
    critic: _CriticRole | None = None,
    max_rounds: int = 2,
) -> tuple[
    AgentLoopService,
    AgentCheckpointService,
    SqliteCheckpointStore,
    _ContextRole,
    _PlannerRole,
    _ExecutorRole,
    _CriticRole,
    _Telemetry,
]:
    db = SqliteCheckpointStore(path)
    checkpoint = AgentCheckpointService(
        CheckpointRepository(
            db,
            cache if cache is not None else InMemoryHotCheckpointCache(),
            codec if codec is not None else JsonCheckpointCodec(),
        )
    )
    context = context or _ContextRole()
    planner = planner or _PlannerRole()
    executor = executor or _ExecutorRole()
    critic = critic or _CriticRole()
    telemetry = _Telemetry()
    loop = AgentLoopService(
        context,
        planner,
        executor,
        checkpoint,
        None,
        telemetry,
        critic,
        budget_config={"maxRounds": max_rounds},
    )
    return loop, checkpoint, db, context, planner, executor, critic, telemetry


@pytest.mark.asyncio
async def test_plan_checkpoint_survives_process_failure_and_skips_planner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-failure.sqlite3"
    first_executor = _ExecutorRole(failure=RuntimeError("worker stopped"))
    first = _stack(path, executor=first_executor)
    with pytest.raises(RuntimeError, match="worker stopped"):
        await first[0].run(_context(), media_id=7)
    assert first[4].plan_calls == 1
    first[2].close()

    second = _stack(path)
    returned = await second[0].run(_context(), media_id=7)
    assert returned.critique is not None and returned.critique.passed
    assert second[4].plan_calls == 0
    assert second[5].calls == 1
    assert second[6].calls == 1
    second[2].close()


@pytest.mark.asyncio
async def test_draft_checkpoint_reopens_and_resumes_critic_without_executor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "draft.sqlite3"
    first = _stack(path)
    key = TaskKey(7, "goal")
    plan = _plan("persisted")
    draft = AgentState(goal="goal", plan=plan, result=_result(), round=1)
    await first[1].save_plan(key, plan)
    await first[1].save_execution_state(key, draft)
    first[2].close()

    second = _stack(path, max_rounds=2)
    returned = await second[0].run(_context(), media_id=7)
    assert returned.round == 1
    assert returned.critique is not None and returned.critique.passed
    assert second[4].plan_calls == 0
    assert second[5].calls == 0
    assert second[6].calls == 1
    assert second[7].counts["criticCheckpointResumes"] == 1
    second[2].close()


@pytest.mark.asyncio
async def test_failed_critic_checkpoint_restarts_next_round_with_targeted_refresh_and_replan(
    tmp_path: Path,
) -> None:
    path = tmp_path / "critic-failure.sqlite3"
    failed = CriticResult(
        passed=False,
        requiredTimestamps=(30_000,),
        missingRequirements=("quiz",),
    )
    first_context = _ContextRole(fail_refine=True)
    first = _stack(
        path,
        context=first_context,
        critic=_CriticRole(failed),
        max_rounds=2,
    )
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        await first[0].run(_context(), media_id=7)
    assert first[5].calls == 1
    assert first[6].calls == 1
    first[2].close()

    second_planner = _PlannerRole(replanned=_plan("quiz"))
    second_context = _ContextRole()
    second = _stack(
        path,
        context=second_context,
        planner=second_planner,
        max_rounds=2,
    )
    returned = await second[0].run(_context(), media_id=7)
    assert returned.round == 2
    assert returned.critique is not None and returned.critique.passed
    assert second_planner.plan_calls == 0
    assert second_planner.replan_calls == 1
    assert second_context.refine_calls == 1
    assert second[5].calls == 1
    assert second[6].calls == 1
    assert second[7].counts["criticEvidenceRefreshes"] == 1
    assert second[7].counts["planRevisions"] == 1
    second[2].close()


@pytest.mark.asyncio
async def test_passed_terminal_checkpoint_returns_without_context_or_models(
    tmp_path: Path,
) -> None:
    path = tmp_path / "terminal.sqlite3"
    first = _stack(path, max_rounds=2)
    key = TaskKey(7, "goal")
    terminal = AgentState(
        goal="goal",
        plan=_plan("terminal"),
        result=_result("done"),
        critique=CriticResult(passed=True),
        round=1,
    )
    await first[1].save_plan(key, terminal.plan)
    await first[1].save_critic_state(key, terminal)
    await first[1].save_result(key, terminal)
    first[2].close()

    second = _stack(path)
    returned = await second[0].run(_context(), media_id=7)
    assert returned == terminal
    assert second[3].select_calls == 0
    assert second[3].refine_calls == 0
    assert second[4].plan_calls == 0
    assert second[5].calls == 0
    assert second[6].calls == 0
    assert second[7].counts["terminalCheckpointHits"] == 1
    second[2].close()


class _FailOnceSqliteStore(SqliteCheckpointStore):
    """Inject a failure after a row write; SQLite must roll it back."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.fail_next = False

    def upsert_many(self, records) -> None:  # type: ignore[no-untyped-def]
        values = tuple(records)
        if not self.fail_next:
            return super().upsert_many(values)
        self.fail_next = False
        row = values[0]
        try:
            with self._lock, self.connection:
                self.connection.execute(
                    """
                    INSERT INTO agent_checkpoints
                        (media_id, checkpoint_key, stage, payload, updated_at)
                    VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    ON CONFLICT(media_id, checkpoint_key) DO UPDATE SET
                        stage = excluded.stage,
                        payload = excluded.payload,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    """,
                    (
                        int(row.media_id),
                        str(row.checkpoint_name),
                        str(row.stage),
                        row.payload,
                    ),
                )
                raise RuntimeError("injected commit failure")
        except RuntimeError:
            raise


@pytest.mark.asyncio
async def test_durable_rollback_keeps_last_valid_checkpoint_and_cache_value(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollback.sqlite3"
    db = _FailOnceSqliteStore(path)
    cache = InMemoryHotCheckpointCache()
    checkpoint = AgentCheckpointService(CheckpointRepository(db, cache))
    key = TaskKey(7, "goal")
    await checkpoint.save_plan(key, _plan("old"))
    old_payload = cache.get_hash(checkpoint.goal_key(7, "goal"), "plan")

    db.fail_next = True
    with pytest.raises(Exception):
        await checkpoint.save_plan(key, _plan("new"))
    checkpoint_name = checkpoint.goal_checkpoint("goal", AnalysisMode.GENERAL, "plan")
    persisted = db.read(7, checkpoint_name)
    assert persisted is not None and persisted.payload == old_payload
    assert (await checkpoint.load_plan(key)).tasks == ("old",)  # type: ignore[union-attr]
    assert cache.get_hash(checkpoint.goal_key(7, "goal"), "plan") == old_payload

    await checkpoint.save_plan(key, _plan("new"))
    assert (await checkpoint.load_plan(key)).tasks == ("new",)  # type: ignore[union-attr]
    db.close()


@pytest.mark.asyncio
async def test_cache_read_write_delete_outages_recover_from_durable_store(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cache-outage.sqlite3"
    cache = InMemoryHotCheckpointCache()
    first = _stack(path, cache=cache)
    key = TaskKey(7, "goal")
    await first[1].save_plan(key, _plan("durable"))
    cache.fail_reads = True
    cache.fail_writes = True
    assert (await first[1].load_plan(key)).tasks == ("durable",)  # type: ignore[union-attr]
    cache.fail_reads = False
    cache.fail_deletes = True
    await first[1].delete_media(7)
    assert first[2].records(7) == ()
    first[2].close()

    reopened = _stack(path, cache=InMemoryHotCheckpointCache(fail_reads=True))
    assert await reopened[1].load_plan(key) is None
    reopened[2].close()


@pytest.mark.asyncio
async def test_version_mismatch_is_not_reused_and_new_codec_can_replace_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "version.sqlite3"
    db = SqliteCheckpointStore(path)
    cache = InMemoryHotCheckpointCache()
    old_service = AgentCheckpointService(
        CheckpointRepository(
            db,
            cache,
            JsonCheckpointCodec(schema_version=1, prompt_version="old", embedding_version="old"),
        )
    )
    key = TaskKey(7, "goal")
    await old_service.save_plan(key, _plan("old"))
    current_service = AgentCheckpointService(
        CheckpointRepository(
            db,
            cache,
            JsonCheckpointCodec(schema_version=2, prompt_version="new", embedding_version="new"),
        )
    )
    with pytest.raises(CheckpointVersionMismatchError):
        await current_service.load_plan(key)
    assert cache.get_hash(current_service.goal_key(7, "goal"), "plan") is None
    await current_service.save_plan(key, _plan("new"))
    assert (await current_service.load_plan(key)).tasks == ("new",)  # type: ignore[union-attr]
    db.close()


@pytest.mark.asyncio
async def test_loop_goal_and_mode_namespaces_isolate_results_while_context_reuses(
    tmp_path: Path,
) -> None:
    path = tmp_path / "namespaces.sqlite3"
    first = _stack(path, max_rounds=1)
    await first[0].run(_context("goal-a"), media_id=7)
    first[2].close()

    # Content checkpoints are deliberately media-scoped and can be reused by
    # another goal, while the Agent result namespace must remain isolated.
    reopened = _stack(path, max_rounds=1)
    await reopened[1].save_context(7, _context("goal-a"))
    await reopened[1].save_chunks(
        7, (VideoChunk(startTime=0, endTime=1_000, segmentSummary="shared"),)
    )
    context = await reopened[1].load_context(7)
    assert context is not None and context.user_goal == ""
    assert await reopened[1].load_chunks(7)
    await reopened[0].run(_context("goal-b"), media_id=7)
    assert reopened[4].plan_calls == 1
    reopened[2].close()

    learning = _stack(path, max_rounds=1)
    await learning[0].run(
        _context("goal-a"),
        media_id=7,
        profile=ModeProfile(mode=AnalysisMode.LEARNING),
    )
    assert learning[4].plan_calls == 1
    learning[2].close()

    general = _stack(path, max_rounds=1)
    returned = await general[0].run(_context("goal-a"), media_id=7)
    assert returned.critique is not None and returned.critique.passed
    assert general[3].select_calls == 0
    assert general[4].plan_calls == 0
    assert general[5].calls == 0
    assert general[6].calls == 0
    general[2].close()


@pytest.mark.asyncio
async def test_reopened_sqlite_concurrent_goal_writes_have_no_partial_records(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent.sqlite3"
    first = _stack(path)
    keys = tuple(TaskKey(7, f"goal-{index}") for index in range(8))
    await asyncio.gather(
        *(first[1].save_plan(key, _plan(f"task-{index}")) for index, key in enumerate(keys))
    )
    first[2].close()

    reopened = _stack(path)
    loaded = await asyncio.gather(*(reopened[1].load_plan(key) for key in keys))
    assert tuple(plan.tasks for plan in loaded if plan is not None) == tuple(
        (f"task-{index}",) for index in range(8)
    )
    rows = reopened[2].records(7)
    assert len(rows) == 16
    assert all(row.payload is not None for row in rows if ":plan" in row.checkpoint_name)
    reopened[2].close()
