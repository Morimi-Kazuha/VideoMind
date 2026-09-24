from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from dovideo.application import (
    DurableToolCallState,
    DurableToolExecutionState,
    ExecutionEventType,
    ExecutionRecordService,
    ExecutionRecordStatus,
    ExecutorTurn,
    ExecutorTurnKind,
    HistoricalAgentReplayService,
    HistoricalReplayResult,
    InMemoryExecutionRecordRepository,
    ModelToolRequest,
    PolicyDecision,
    ReplayArtifactUnavailableError,
    ReplayIncompleteError,
    ReplayIncompatibleVersionError,
    ReplayIntegrityError,
    ReplayNotFoundError,
    TaskKey,
    ToolResult,
    ToolResultStatus,
    canonical_tool_arguments_digest,
    empty_tool_state_ledger,
)
from dovideo.application.tool_contracts import GetSegmentArguments
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    CriticResult,
    PROVENANCE_VERSION,
)


REVISION = "revision-1"


def _service(
    repository: InMemoryExecutionRecordRepository | None = None,
    *,
    execution_id: str = "execution-x2c",
) -> tuple[ExecutionRecordService, InMemoryExecutionRecordRepository]:
    store = repository or InMemoryExecutionRecordRepository()
    return (
        ExecutionRecordService(
            store,
            id_factory=lambda: execution_id,
            clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        store,
    )


def _result(*, title: str = "answer", revision: str = REVISION) -> AnalysisResult:
    return AnalysisResult(
        title=title,
        conclusions=("alpha evidence",),
        evidence=(
            AnalysisEvidence(
                timestampMs=1000,
                source="ASR",
                content="alpha evidence",
                claim="alpha evidence",
                sourceRevision=revision,
                segmentId="segment-1",
                sourceItemIds=("item-1",),
                sourceProvenanceVersion=PROVENANCE_VERSION,
            ),
        ),
    )


def _selected(revision: str = REVISION) -> list[dict[str, Any]]:
    return [
        {
            "segmentId": "segment-1",
            "chunkId": "chunk-1",
            "sourceRevision": revision,
            "startMs": 0,
            "endMs": 2_000,
            "sourceItemIds": ["item-1"],
        }
    ]


async def _completed_final(
    *,
    service: ExecutionRecordService,
    key: TaskKey,
    result: AnalysisResult | None = None,
    include_second_plan: bool = False,
) -> Any:
    record = await service.start_or_resume(
        key,
        media_identity="media-7",
        source_revision=REVISION,
    )
    await service.record_retrieval_selection(
        record.execution_id,
        _selected(),
        purpose="initial",
        logical_event_id="retrieval.initial",
        agent_round=0,
        query_digest="digest-initial",
    )
    await service.record_plan(
        record.execution_id,
        AgentPlan(understoodGoal=key.goal, tasks=("inspect",)),
        logical_event_id="plan.initial",
        agent_round=0,
    )
    if include_second_plan:
        await service.record_retrieval_selection(
            record.execution_id,
            _selected(),
            purpose="replan",
            logical_event_id="retrieval.replan",
            agent_round=1,
            query_digest="digest-replan",
        )
        await service.record_plan(
            record.execution_id,
            AgentPlan(understoodGoal=key.goal, tasks=("inspect", "verify")),
            event_type=ExecutionEventType.PLAN_REPLANNED,
            logical_event_id="plan.replanned",
            agent_round=1,
        )
    final = result or _result()
    await service.record_executor_turn(
        record.execution_id,
        ExecutorTurn(kind=ExecutorTurnKind.FINAL, final_result=final),
        logical_event_id="executor.final",
        agent_round=1 if include_second_plan else 0,
    )
    critic = CriticResult(passed=True, feedback=("grounded",))
    await service.record_critic(
        record.execution_id,
        critic,
        logical_event_id="critic.final",
        agent_round=1 if include_second_plan else 0,
    )
    await service.record_evidence_verification(
        record.execution_id,
        final,
        critic,
        logical_event_id="evidence.final",
        agent_round=1 if include_second_plan else 0,
        source_revision=REVISION,
    )
    return await service.complete(
        record.execution_id,
        AgentState(
            goal=key.goal,
            plan=AgentPlan(
                understoodGoal=key.goal,
                tasks=("inspect", "verify") if include_second_plan else ("inspect",),
            ),
            result=final,
            critique=critic,
            round=1 if include_second_plan else 0,
        ),
    )


class _LedgerReader:
    def __init__(self, ledger=None):
        self.ledger = ledger
        self.loads: list[TaskKey] = []
        self.saves = 0

    async def load_tool_state(self, key: TaskKey):
        self.loads.append(key)
        return self.ledger

    async def save_tool_state(self, key, state):
        del key, state
        self.saves += 1


def _stored_tool_state(key: TaskKey, request_index: int = 1) -> DurableToolCallState:
    request = ModelToolRequest(
        tool_name="video.get_segment",
        arguments={"timestamp_ms": request_index * 100},
    )
    arguments = GetSegmentArguments(timestamp_ms=request_index * 100)
    digest = canonical_tool_arguments_digest(arguments)
    state = DurableToolCallState(
        task_key=key,
        agent_round=request_index,
        request_index=request_index,
        call_id=f"tool-call-{request_index}",
        tool_name="video.get_segment",
        request=request,
        execution_state=DurableToolExecutionState.REQUESTED,
    )
    state = state.transition(
        DurableToolExecutionState.AUTHORIZED,
        validated_arguments=arguments,
        canonical_args_digest=digest,
        policy_decision=PolicyDecision.ALLOW,
    )
    state = state.transition(DurableToolExecutionState.EXECUTING)
    return state.transition(
        DurableToolExecutionState.RESULT_STORED,
            tool_result=ToolResult(
            call_id=f"tool-call-{request_index}",
            tool_name="video.get_segment",
            status=ToolResultStatus.SUCCESS,
            payload={"segmentId": f"segment-{request_index}"},
        ),
    )


async def _completed_tool(
    service: ExecutionRecordService,
    key: TaskKey,
    *,
    tool_count: int = 1,
) -> tuple[Any, _LedgerReader]:
    record = await service.start_or_resume(
        key,
        media_identity="media-7",
        source_revision=REVISION,
    )
    await service.record_retrieval_selection(
        record.execution_id,
        _selected(),
        purpose="initial",
        logical_event_id="retrieval.initial",
    )
    await service.record_plan(
        record.execution_id,
        AgentPlan(understoodGoal=key.goal, tasks=("inspect",)),
        logical_event_id="plan.initial",
    )
    ledger = empty_tool_state_ledger(key)
    for request_index in range(1, tool_count + 1):
        request = ModelToolRequest(
            tool_name="video.get_segment",
            arguments={"timestamp_ms": request_index * 100},
        )
        state = _stored_tool_state(key, request_index=request_index)
        await service.record_executor_turn(
            record.execution_id,
            ExecutorTurn(kind=ExecutorTurnKind.TOOL_REQUEST, tool_request=request),
            logical_event_id=f"executor.round.{request_index}.tool.{request_index}",
            agent_round=request_index,
            request_index=request_index,
            args_digest=state.canonical_args_digest,
        )
        result = state.tool_result
        assert result is not None
        await service.record_tool_reference(
            record.execution_id,
            logical_event_id=f"tool.tool-call-{request_index}.result",
            agent_round=request_index,
            call_id=f"tool-call-{request_index}",
            request_index=request_index,
            tool_name="video.get_segment",
            args_digest=state.canonical_args_digest or "",
            policy_decision=PolicyDecision.ALLOW.value,
            reason_code=None,
            result_status=result.status.value,
        )
        ledger = ledger.with_record(state)
    final = _result(title="tool answer")
    await service.record_executor_turn(
        record.execution_id,
        ExecutorTurn(kind=ExecutorTurnKind.FINAL, final_result=final),
        logical_event_id="executor.final",
        agent_round=tool_count,
    )
    critic = CriticResult(passed=True)
    await service.record_critic(
        record.execution_id,
        critic,
        logical_event_id="critic.final",
        agent_round=tool_count,
    )
    await service.record_evidence_verification(
        record.execution_id,
        final,
        critic,
        logical_event_id="evidence.final",
        agent_round=tool_count,
        source_revision=REVISION,
    )
    completed = await service.complete(
        record.execution_id,
        AgentState(
            goal=key.goal,
            plan=AgentPlan(understoodGoal=key.goal, tasks=("inspect",)),
            result=final,
            critique=critic,
            round=tool_count,
        ),
    )
    reader = _LedgerReader(ledger)
    return completed, reader


@pytest.mark.asyncio
async def test_final_replay_is_providerless_deterministic_and_consumes_recorded_dtos() -> None:
    service, store = _service()
    key = TaskKey(7, "find evidence", AnalysisMode.GENERAL)
    completed = await _completed_final(service=service, key=key)
    before = store.get(completed.execution_id)
    assert before is not None

    replay = HistoricalAgentReplayService(store)
    first = await replay.replay(completed.execution_id, expected_task_key=key)
    second = await replay.replay(completed.execution_id, expected_task_key=key)

    assert isinstance(first, HistoricalReplayResult)
    assert first.status is ExecutionRecordStatus.COMPLETED
    assert first.historical_plan is not None
    assert first.historical_final_result == _result()
    assert first.historical_critic_results[-1].passed is True
    assert first.historical_evidence_outcomes[-1].passed is True
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert store.get(completed.execution_id) == before


@pytest.mark.asyncio
async def test_tool_replay_reads_x1_ledger_without_executing_or_saving() -> None:
    service, store = _service(execution_id="execution-tool")
    key = TaskKey(7, "inspect segment")
    completed, reader = await _completed_tool(service, key)

    result = await HistoricalAgentReplayService(
        store,
        tool_checkpoint=reader,
    ).replay(completed.execution_id, expected_task_key=key)

    assert len(result.historical_tool_calls) == 1
    tool = result.historical_tool_calls[0]
    assert tool.tool_result is not None
    assert tool.tool_result.payload == {"segmentId": "segment-1"}
    assert reader.loads == [key]
    assert reader.saves == 0
    serialized = result.model_dump(mode="json", by_alias=True)
    assert "arguments" not in str(serialized)
    assert "prompt" not in str(serialized).lower()


@pytest.mark.asyncio
async def test_multiple_tool_turns_replay_in_request_and_sequence_order() -> None:
    service, store = _service(execution_id="execution-two-tools")
    key = TaskKey(7, "inspect two segments")
    completed, reader = await _completed_tool(service, key, tool_count=2)

    result = await HistoricalAgentReplayService(
        store,
        tool_checkpoint=reader,
    ).replay(completed.execution_id)

    assert [call.request_index for call in result.historical_tool_calls] == [1, 2]
    assert [call.call_id for call in result.historical_tool_calls] == [
        "tool-call-1",
        "tool-call-2",
    ]
    assert [turn.request_index for turn in result.historical_executor_turns if turn.request_index] == [1, 2]
    assert result.historical_tool_calls[1].tool_result is not None
    assert result.historical_tool_calls[1].tool_result.payload == {"segmentId": "segment-2"}
    assert reader.saves == 0


@pytest.mark.asyncio
async def test_replay_preserves_plan_evolution_and_shuffled_storage_order() -> None:
    service, store = _service(execution_id="execution-replan")
    key = TaskKey(7, "replan goal")
    completed = await _completed_final(
        service=service,
        key=key,
        include_second_plan=True,
    )
    record = store.get(completed.execution_id)
    assert record is not None
    shuffled = record.model_copy(update={"events": tuple(reversed(record.events))})
    store._records[completed.execution_id] = shuffled  # type: ignore[attr-defined]

    result = await HistoricalAgentReplayService(store).replay(completed.execution_id)

    assert [event.sequence_no for event in result.events] == list(
        range(1, len(record.events) + 1)
    )
    assert len(result.historical_plans) == 2
    assert result.historical_plan == result.historical_plans[-1]
    assert len(result.historical_retrievals) == 2


@pytest.mark.asyncio
async def test_failed_record_replays_bounded_failure_history_without_fabricating_final() -> None:
    service, store = _service(execution_id="execution-failed")
    key = TaskKey(7, "failed goal")
    record = await service.start_or_resume(
        key,
        media_identity="media-7",
        source_revision=REVISION,
    )
    failed = await service.fail(record.execution_id, ValueError("provider details hidden"))

    result = await HistoricalAgentReplayService(store).replay(failed.execution_id)

    assert result.status is ExecutionRecordStatus.FAILED
    assert result.historical_final_result is None
    assert result.historical_failure is not None
    assert result.historical_failure.error_type == "ValueError"


@pytest.mark.asyncio
async def test_failed_replay_preserves_historical_evidence_failure() -> None:
    service, store = _service(execution_id="execution-evidence-failed")
    key = TaskKey(7, "evidence failure")
    record = await service.start_or_resume(
        key,
        media_identity="media-7",
        source_revision=REVISION,
    )
    await service.record_retrieval_selection(
        record.execution_id,
        _selected(),
        purpose="initial",
        logical_event_id="retrieval.initial",
    )
    await service.record_plan(
        record.execution_id,
        AgentPlan(understoodGoal=key.goal, tasks=("inspect",)),
        logical_event_id="plan.initial",
    )
    final = _result()
    await service.record_executor_turn(
        record.execution_id,
        ExecutorTurn(kind=ExecutorTurnKind.FINAL, final_result=final),
        logical_event_id="executor.final",
        agent_round=1,
    )
    critic = CriticResult(
        passed=False,
        feedback=("missing grounded evidence",),
        requiredTimestamps=(1000,),
    )
    await service.record_critic(
        record.execution_id,
        critic,
        logical_event_id="critic.final",
        agent_round=1,
    )
    await service.record_evidence_verification(
        record.execution_id,
        final,
        critic,
        logical_event_id="evidence.final",
        agent_round=1,
        source_revision=REVISION,
    )
    failed = await service.fail(record.execution_id, RuntimeError("guard rejected"))

    replayed = await HistoricalAgentReplayService(store).replay(failed.execution_id)

    assert replayed.status is ExecutionRecordStatus.FAILED
    assert replayed.historical_evidence_outcomes[-1].passed is False
    assert replayed.historical_evidence_outcomes[-1].required_timestamps == (1000,)
    assert replayed.historical_final_result == final


@pytest.mark.asyncio
async def test_unknown_event_and_forbidden_payload_fail_closed() -> None:
    service, store = _service(execution_id="execution-unknown")
    key = TaskKey(7, "unknown event")
    completed = await _completed_final(service=service, key=key)
    record = store.get(completed.execution_id)
    assert record is not None
    unknown = record.events[1].model_copy(
        update={"event_type": "FUTURE_REPLAY_EVENT"}
    )
    store._records[record.execution_id] = record.model_copy(
        update={"events": (record.events[0], unknown, *record.events[2:])}
    )  # type: ignore[attr-defined]
    with pytest.raises(ReplayIntegrityError):
        await HistoricalAgentReplayService(store).replay(record.execution_id)

    forbidden = record.events[1].model_copy(
        update={"payload": {**record.events[1].payload, "providerKey": "redacted"}}
    )
    store._records[record.execution_id] = record.model_copy(
        update={"events": (record.events[0], forbidden, *record.events[2:])}
    )  # type: ignore[attr-defined]
    with pytest.raises(ReplayIntegrityError):
        await HistoricalAgentReplayService(store).replay(record.execution_id)


@pytest.mark.asyncio
async def test_replay_fails_closed_for_missing_incomplete_legacy_and_version_records() -> None:
    service, store = _service(execution_id="execution-incomplete")
    key = TaskKey(7, "incomplete goal")
    record = await service.start_or_resume(
        key,
        media_identity="media-7",
        source_revision=REVISION,
    )
    replay = HistoricalAgentReplayService(store)

    with pytest.raises(ReplayIncompleteError):
        await replay.replay(record.execution_id)
    with pytest.raises(ReplayNotFoundError):
        await replay.replay("legacy-or-missing")

    incompatible = record.model_copy(
        update={"status": ExecutionRecordStatus.FAILED, "completed_at": record.created_at, "replayable": False,
                "execution_contract_version": "future-contract",
                "events": record.events + (record.events[0].model_copy(update={
                    "sequence_no": 2,
                    "event_type": ExecutionEventType.EXECUTION_FAILED,
                    "logical_event_id": "execution.failed",
                    "stage": "AGENT",
                    "payload": {"classification": "failed", "errorType": "ValueError", "retryable": False},
                }),)}
    )
    store._records[record.execution_id] = incompatible  # type: ignore[attr-defined]
    with pytest.raises(ReplayIncompatibleVersionError):
        await replay.replay(record.execution_id)


@pytest.mark.asyncio
async def test_replay_rejects_sequence_and_provenance_corruption() -> None:
    service, store = _service(execution_id="execution-corrupt")
    key = TaskKey(7, "corrupt goal")
    completed = await _completed_final(service=service, key=key)
    record = store.get(completed.execution_id)
    assert record is not None
    replay = HistoricalAgentReplayService(store)

    corrupt_sequence = record.model_copy(
        update={"events": tuple(
            event.model_copy(update={"sequence_no": event.sequence_no + (1 if event.sequence_no == 2 else 0)})
            for event in record.events
        )}
    )
    store._records[record.execution_id] = corrupt_sequence  # type: ignore[attr-defined]
    with pytest.raises(ReplayIntegrityError):
        await replay.replay(record.execution_id)

    bad_revision = record.events[1].model_copy(
        update={
            "payload": {
                **record.events[1].payload,
                "selected": [{**record.events[1].payload["selected"][0], "sourceRevision": "other"}],
            }
        }
    )
    store._records[record.execution_id] = record.model_copy(
        update={"events": (record.events[0], bad_revision, *record.events[2:])}
    )
    with pytest.raises(ReplayIntegrityError):
        await replay.replay(record.execution_id)


@pytest.mark.asyncio
async def test_tool_replay_requires_matching_durable_result_and_does_not_read_redis() -> None:
    service, store = _service(execution_id="execution-tool-missing")
    key = TaskKey(7, "missing tool")
    completed, reader = await _completed_tool(service, key)
    replay = HistoricalAgentReplayService(store, tool_checkpoint=_LedgerReader(None))
    with pytest.raises(ReplayArtifactUnavailableError):
        await replay.replay(completed.execution_id)

    record = store.get(completed.execution_id)
    assert record is not None
    tool_event_index = next(
        index
        for index, event in enumerate(record.events)
        if event.event_type is ExecutionEventType.TOOL_CALL_REFERENCED
    )
    tool_event = record.events[tool_event_index]
    corrupt_tool_event = tool_event.model_copy(
        update={"payload": {**tool_event.payload, "argsDigest": "0" * 64}}
    )
    store._records[completed.execution_id] = record.model_copy(
        update={
            "events": (
                *record.events[:tool_event_index],
                corrupt_tool_event,
                *record.events[tool_event_index + 1 :],
            )
        }
    )  # type: ignore[attr-defined]
    with pytest.raises(ReplayIntegrityError):
        await HistoricalAgentReplayService(store, tool_checkpoint=reader).replay(
            completed.execution_id
        )
    assert reader.saves == 0
