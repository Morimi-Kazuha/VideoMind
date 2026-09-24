from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from itertools import count
from typing import Any

import pytest

from dovideo.application import (
    AgentLoopService,
    AsrBranchOutcome,
    DurableAgentExecutionRecord,
    ExecutorTurn,
    ExecutorTurnKind,
    ExecutionEventType,
    ExecutionRecordConflictError,
    ExecutionRecordPersistenceError,
    ExecutionRecordService,
    ExecutionRecordStatus,
    InMemoryExecutionRecordRepository,
    MediaObservationBundle,
    OcrBranchOutcome,
    ModelToolRequest,
    ToolPolicy,
    ToolResult,
    ToolResultStatus,
    TaskKey,
    TranscriptSpan,
    VideoContextBuilder,
)
from dovideo.application.value_objects import OcrObservation
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    CriticResult,
)


def _context():
    return VideoContextBuilder().build(
        "memory://video",
        "find evidence",
        MediaObservationBundle(
            asr=AsrBranchOutcome(
                observations=(TranscriptSpan(0, 1000, "alpha evidence"),),
                attempted=1,
            ),
            ocr=OcrBranchOutcome(
                observations=(OcrObservation(500, "screen evidence", "frame-1"),),
                attempted=1,
            ),
        ),
        media_content_identity="sha256:source",
    )


def _service(
    repository: InMemoryExecutionRecordRepository | None = None,
    ids: list[str] | None = None,
) -> tuple[ExecutionRecordService, InMemoryExecutionRecordRepository]:
    store = repository or InMemoryExecutionRecordRepository()
    values = iter(ids or ["execution-1", "execution-2", "execution-3"])
    service = ExecutionRecordService(
        store,
        id_factory=lambda: next(values),
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    return service, store


@pytest.mark.asyncio
async def test_execution_identity_recovery_and_p4_lineage() -> None:
    service, store = _service()
    key = TaskKey(7, "find evidence", AnalysisMode.GENERAL)

    first = await service.start_or_resume(
        key,
        media_identity="sha256:source",
        source_revision="revision-1",
        worker_attempt=1,
        request_id="delivery-a",
    )
    resumed = await service.start_or_resume(
        key,
        media_identity="sha256:source",
        source_revision="revision-1",
        worker_attempt=2,
        request_id="delivery-a",
    )

    assert first.execution_id == resumed.execution_id
    assert first.events[0].event_type is ExecutionEventType.EXECUTION_STARTED

    failed = await service.fail(first.execution_id, RuntimeError("bounded"))
    assert failed.status is ExecutionRecordStatus.FAILED

    replay = await service.start_or_resume(
        key,
        media_identity="sha256:source",
        source_revision="revision-1",
        worker_attempt=1,
        request_id="delivery-b",
    )
    assert replay.execution_id != first.execution_id
    assert replay.parent_execution_id == first.execution_id
    assert len(store.all_records()) == 2


@pytest.mark.asyncio
async def test_event_sequence_is_durable_idempotent_and_conflicts_fail_closed() -> None:
    service, store = _service()
    key = TaskKey(7, "goal")
    record = await service.start_or_resume(
        key,
        media_identity="media",
        source_revision="revision",
    )
    plan = AgentPlan(understoodGoal="goal", tasks=("inspect",))

    first = await service.record_plan(record.execution_id, plan)
    duplicate = await service.record_plan(record.execution_id, plan)
    assert first.sequence_no == duplicate.sequence_no == 2

    with pytest.raises(ExecutionRecordConflictError):
        await service.append_event(
            record.execution_id,
            event_type=ExecutionEventType.PLAN_RECORDED,
            logical_event_id="plan.initial",
            payload={"plan": {"understoodGoal": "different"}},
        )

    next_event = await service.append_event(
        record.execution_id,
        event_type=ExecutionEventType.RETRIEVAL_SELECTED,
        logical_event_id="retrieval.initial",
        payload={"selected": []},
    )
    assert next_event.sequence_no == 3
    loaded = store.get(record.execution_id)
    assert loaded is not None
    assert [event.sequence_no for event in loaded.events] == [1, 2, 3]


@pytest.mark.asyncio
async def test_completion_persists_final_result_and_historical_critic() -> None:
    service, store = _service()
    record = await service.start_or_resume(
        TaskKey(7, "goal"),
        media_identity="media",
        source_revision="revision",
    )
    state = AgentState(
        goal="goal",
        plan=AgentPlan(understoodGoal="goal", tasks=("inspect",)),
        result=AnalysisResult(title="answer", conclusions=("claim",)),
        critique=CriticResult(passed=True),
        round=1,
    )

    completed = await service.complete(record.execution_id, state)
    assert completed.status is ExecutionRecordStatus.COMPLETED
    assert completed.replayable is True
    assert completed.events[-1].event_type is ExecutionEventType.EXECUTION_COMPLETED
    assert completed.events[-1].payload["finalResult"]["title"] == "answer"
    assert store.get(record.execution_id) == completed


@pytest.mark.asyncio
async def test_agent_loop_records_semantic_history_before_completion() -> None:
    context = _context()
    service, store = _service(ids=["agent-execution"])
    calls: list[str] = []

    class Context:
        async def select_relevant(self, value, media_id=None):
            calls.append("retrieval")
            return value

    class Planner:
        async def plan(self, value, *, instruction=""):
            calls.append("planner")
            return AgentPlan(understoodGoal=value.user_goal, tasks=("inspect",))

        async def repair_plan(self, value, invalid, *, instruction=""):
            raise AssertionError("repair was not expected")

        async def replan(self, value, current, critique, *, instruction=""):
            return current

    class Executor:
        async def execute(self, value, plan, previous_critique=None, *, instruction=""):
            calls.append("executor")
            return AnalysisResult(
                title="answer",
                conclusions=("alpha evidence",),
                evidence=(
                    AnalysisEvidence(
                        timestampMs=0,
                        source="ASR",
                        content="alpha evidence",
                        claim="alpha evidence",
                    ),
                ),
            )

    class Critic:
        async def critique(self, value, plan, result, *, instruction=""):
            calls.append("critic")
            return CriticResult(passed=True)

    loop = AgentLoopService(
        context_service=Context(),
        planner=Planner(),
        executor=Executor(),
        critic=Critic(),
        execution_record_service=service,
    )
    state = await loop.run(context, media_id=7)
    record = store.latest_for_task(TaskKey(7, context.user_goal, AnalysisMode.GENERAL))

    assert state.result is not None
    assert calls == ["retrieval", "planner", "executor", "critic"]
    assert record is not None
    assert [event.event_type for event in record.events] == [
        ExecutionEventType.EXECUTION_STARTED,
        ExecutionEventType.RETRIEVAL_SELECTED,
        ExecutionEventType.PLAN_RECORDED,
        ExecutionEventType.EXECUTOR_TURN_RECORDED,
        ExecutionEventType.CRITIC_RECORDED,
        ExecutionEventType.EVIDENCE_VERIFICATION_RECORDED,
        ExecutionEventType.EXECUTION_COMPLETED,
    ]
    assert [event.sequence_no for event in record.events] == list(range(1, 8))
    assert record.status is ExecutionRecordStatus.COMPLETED


@pytest.mark.asyncio
async def test_record_creation_failure_blocks_agent_provider_actions() -> None:
    class BrokenRepository(InMemoryExecutionRecordRepository):
        def create(self, record):
            raise OSError("credentials must not escape")

    service, _ = _service(BrokenRepository())
    calls = 0

    class Context:
        async def select_relevant(self, value, media_id=None):
            raise AssertionError("retrieval must not start")

    class Planner:
        async def plan(self, value, *, instruction=""):
            nonlocal calls
            calls += 1
            raise AssertionError("planner must not start")

    class Executor:
        async def execute(self, *args, **kwargs):
            raise AssertionError("executor must not start")

    class Critic:
        async def critique(self, *args, **kwargs):
            raise AssertionError("critic must not start")

    loop = AgentLoopService(
        context_service=Context(),
        planner=Planner(),
        executor=Executor(),
        critic=Critic(),
        execution_record_service=service,
    )
    with pytest.raises(ExecutionRecordPersistenceError):
        await loop.run(_context(), media_id=7)
    assert calls == 0


@pytest.mark.asyncio
async def test_legacy_checkpoint_is_not_promoted_to_replayable_history() -> None:
    service, store = _service(ids=["must-not-be-created"])

    class LegacyCheckpoint:
        async def load_plan(self, key):
            del key
            return AgentPlan(understoodGoal="find evidence", tasks=("old",))

        async def load_critic_state(self, key):
            del key
            return None

    class Context:
        async def select_relevant(self, value, media_id=None):
            raise AssertionError("legacy promotion must stop before retrieval")

    class Planner:
        async def plan(self, value, *, instruction=""):
            raise AssertionError("legacy promotion must stop before planner")

    class Executor:
        async def execute(self, *args, **kwargs):
            raise AssertionError("legacy promotion must stop before executor")

    class Critic:
        async def critique(self, *args, **kwargs):
            raise AssertionError("legacy promotion must stop before critic")

    loop = AgentLoopService(
        context_service=Context(),
        planner=Planner(),
        executor=Executor(),
        critic=Critic(),
        checkpoint=LegacyCheckpoint(),
        execution_record_service=service,
    )
    with pytest.raises(ExecutionRecordConflictError, match="legacy checkpoint"):
        await loop.run(_context(), media_id=7)
    assert store.all_records() == ()


@pytest.mark.asyncio
async def test_trace_projection_failure_does_not_change_durable_record() -> None:
    projected: list[dict[str, Any]] = []

    def projection(*args, **kwargs):
        projected.append(dict(kwargs))
        raise RuntimeError("redis unavailable")

    repository = InMemoryExecutionRecordRepository()
    service = ExecutionRecordService(
        repository,
        id_factory=lambda: "execution-trace-failure",
        trace_projection=projection,
    )
    record = await service.start_or_resume(
        TaskKey(7, "goal"),
        media_identity="media",
        source_revision="revision",
    )
    await service.append_event(
        record.execution_id,
        event_type=ExecutionEventType.RETRIEVAL_SELECTED,
        logical_event_id="retrieval.initial",
        payload={"selected": []},
    )
    loaded = await service.load(record.execution_id)
    assert loaded is not None
    assert loaded.latest_sequence == 2
    assert len(projected) == 2


@pytest.mark.asyncio
async def test_terminal_projection_reflects_completed_status() -> None:
    projections: list[dict[str, Any]] = []

    def projection(*args, **kwargs):
        del args
        projections.append(dict(kwargs))

    service, _store = _service(ids=["execution-terminal-projection"])
    service = ExecutionRecordService(
        service.repository,
        id_factory=lambda: "execution-terminal-projection",
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        trace_projection=projection,
    )
    record = await service.start_or_resume(
        TaskKey(7, "goal"),
        media_identity="media",
        source_revision="revision",
    )
    state = AgentState(
        goal="goal",
        plan=AgentPlan(understoodGoal="goal", tasks=("inspect",)),
        result=AnalysisResult(
            title="answer",
            conclusions=("claim",),
            evidence=(
                AnalysisEvidence(
                    timestampMs=0,
                    source="ASR",
                    content="claim",
                    claim="claim",
                ),
            ),
        ),
        critique=CriticResult(passed=True),
        round=1,
    )
    completed = await service.complete(record.execution_id, state)

    assert completed.status is ExecutionRecordStatus.COMPLETED
    assert projections[-1]["status"] == "COMPLETED"
    assert projections[-1]["latest_sequence"] == completed.latest_sequence


@pytest.mark.asyncio
async def test_tool_aware_agent_history_references_x1_without_copying_result_body() -> None:
    context = _context()
    service, store = _service(ids=["execution-tool-history"])

    class Context:
        async def select_relevant(self, value, media_id=None):
            del media_id
            return value

    class Planner:
        async def plan(self, value, *, instruction=""):
            del instruction
            return AgentPlan(understoodGoal=value.user_goal, tasks=("inspect",))

        async def repair_plan(self, value, invalid, *, instruction=""):
            raise AssertionError("repair was not expected")

        async def replan(self, value, current, critique, *, instruction=""):
            return current

    class LegacyExecutor:
        async def execute(self, *args, **kwargs):
            raise AssertionError("final-only executor must not be used")

    class TurnExecutor:
        async def execute_turn(self, value, plan, previous_critique=None, *, instruction=""):
            return ExecutorTurn(
                kind=ExecutorTurnKind.TOOL_REQUEST,
                tool_request=ModelToolRequest(
                    tool_name="video.get_segment",
                    arguments={"timestamp_ms": 0},
                ),
            )

        async def continue_after_tool(
            self,
            value,
            plan,
            tool_result,
            previous_critique=None,
            *,
            instruction="",
            tools_available=True,
        ):
            assert tool_result.status is ToolResultStatus.SUCCESS
            return ExecutorTurn(
                kind=ExecutorTurnKind.FINAL,
                final_result=AnalysisResult(
                    title="answer",
                    conclusions=("alpha evidence",),
                    evidence=(
                        AnalysisEvidence(
                            timestampMs=0,
                            source="ASR",
                            content="alpha evidence",
                            claim="alpha evidence",
                        ),
                    ),
                ),
            )

    class ToolExecutor:
        async def execute(self, tool_call, trusted_context, remaining_deadline=None):
            del trusted_context, remaining_deadline
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.tool_name,
                status=ToolResultStatus.SUCCESS,
                payload={"privateResultBody": "must stay in X1 ledger"},
            )

    class ToolCheckpoint:
        def __init__(self):
            self.ledger = None

        async def load_tool_state(self, key):
            del key
            return self.ledger

        async def save_tool_state(self, key, ledger):
            del key
            self.ledger = ledger

    class Critic:
        async def critique(self, value, plan, result, *, instruction=""):
            return CriticResult(passed=True)

    loop = AgentLoopService(
        context_service=Context(),
        planner=Planner(),
        executor=LegacyExecutor(),
        critic=Critic(),
        executor_turn=TurnExecutor(),
        tool_executor=ToolExecutor(),
        tool_checkpoint=ToolCheckpoint(),
        tool_policy=ToolPolicy(),
        execution_record_service=service,
    )
    await loop.run(context, media_id=7)

    record = store.latest_for_task(TaskKey(7, context.user_goal, AnalysisMode.GENERAL))
    assert record is not None
    event_types = [event.event_type for event in record.events]
    assert ExecutionEventType.EXECUTOR_TURN_RECORDED in event_types
    assert ExecutionEventType.TOOL_CALL_REFERENCED in event_types
    tool_event = next(
        event for event in record.events
        if event.event_type is ExecutionEventType.TOOL_CALL_REFERENCED
    )
    assert tool_event.payload["callId"] == "tool-call-1"
    assert tool_event.payload["resultStatus"] == "SUCCESS"
    serialized = str(record.model_dump(mode="json", by_alias=True))
    assert "privateResultBody" not in serialized
    assert "timestamp_ms" not in serialized


def test_legacy_execution_history_is_not_fabricated() -> None:
    repository = InMemoryExecutionRecordRepository()
    assert repository.latest_for_task(TaskKey(7, "legacy")) is None
