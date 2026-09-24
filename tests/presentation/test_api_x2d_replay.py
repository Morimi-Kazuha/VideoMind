from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from dovideo.application import (
    ExecutionRecordService,
    ExecutionRecordStatus,
    ExecutorTurn,
    ExecutorTurnKind,
    HistoricalAgentReplayService,
    HistoricalReplayAccessService,
    InMemoryExecutionRecordRepository,
    ModelToolRequest,
    PolicyDecision,
    TaskKey,
    ToolResult,
    ToolResultStatus,
    canonical_tool_arguments_digest,
    empty_tool_state_ledger,
)
from dovideo.application.tool_contracts import GetSegmentArguments
from dovideo.application.tool_state import (
    DurableToolCallState,
    DurableToolExecutionState,
)
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    CriticResult,
    PROVENANCE_VERSION,
)
from dovideo.presentation.api import create_app
from dovideo.presentation.api.runtime import R1ServiceError


REVISION = "revision-x2d"


class _Auth:
    def require(self, authorization):
        values = {
            "Bearer owner-token": {"id": 7, "role": "USER"},
            "Bearer other-token": {"id": 8, "role": "USER"},
            "Bearer operator-token": {"id": 2, "role": "OPERATOR"},
            "Bearer admin-token": {"id": 1, "role": "ADMIN"},
        }
        if authorization not in values:
            raise R1ServiceError("请先登录", status_code=401)
        return values[authorization]


class _Media:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def require_owned(self, media_id: int, user_id: int):
        self.calls.append((media_id, user_id))
        if media_id != 7 or user_id != 7:
            raise LookupError("not owner")
        return SimpleNamespace(user_id=7)


class _Services:
    def __init__(self, access, media: _Media) -> None:
        self.auth = _Auth()
        self.media = media
        self.replay_access = access
        self.dispatch_calls = 0
        self.failed_task_replay_calls = 0

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def _result() -> AnalysisResult:
    return AnalysisResult(
        title="historical answer",
        conclusions=("alpha evidence",),
        evidence=(
            AnalysisEvidence(
                timestampMs=1_000,
                source="ASR",
                content="alpha evidence",
                claim="alpha evidence",
                sourceRevision=REVISION,
                segmentId="segment-1",
                sourceItemIds=("item-1",),
                sourceProvenanceVersion=PROVENANCE_VERSION,
            ),
        ),
    )


async def _completed_record(service: ExecutionRecordService, key: TaskKey):
    record = await service.start_or_resume(
        key,
        media_identity="media-7",
        source_revision=REVISION,
    )
    await service.record_retrieval_selection(
        record.execution_id,
        [
            {
                "segmentId": "segment-1",
                "chunkId": "chunk-1",
                "sourceRevision": REVISION,
                "startMs": 0,
                "endMs": 2_000,
                "sourceItemIds": ["item-1"],
            }
        ],
        purpose="initial",
        logical_event_id="retrieval.initial",
    )
    plan = AgentPlan(understoodGoal=key.goal, tasks=("inspect",))
    await service.record_plan(
        record.execution_id,
        plan,
        logical_event_id="plan.initial",
        agent_round=0,
    )
    final = _result()
    await service.record_executor_turn(
        record.execution_id,
        SimpleNamespace(kind="FINAL", final_result=final),
        logical_event_id="executor.final",
        agent_round=0,
    )
    critic = CriticResult(passed=True, feedback=("grounded",))
    await service.record_critic(
        record.execution_id,
        critic,
        logical_event_id="critic.final",
        agent_round=0,
    )
    await service.record_evidence_verification(
        record.execution_id,
        final,
        critic,
        logical_event_id="evidence.final",
        agent_round=0,
        source_revision=REVISION,
    )
    return await service.complete(
        record.execution_id,
        AgentState(
            goal=key.goal,
            plan=plan,
            result=final,
            critique=critic,
            round=0,
        ),
    )


class _HistoricalLedgerReader:
    def __init__(self, ledger) -> None:
        self.ledger = ledger
        self.loads: list[TaskKey] = []
        self.saves = 0

    async def load_tool_state(self, key: TaskKey):
        self.loads.append(key)
        return self.ledger

    async def save_tool_state(self, key, state):
        del key, state
        self.saves += 1


def _stored_tool_state(key: TaskKey) -> DurableToolCallState:
    request = ModelToolRequest(
        tool_name="video.get_segment",
        arguments={"timestamp_ms": 100},
    )
    arguments = GetSegmentArguments(timestamp_ms=100)
    digest = canonical_tool_arguments_digest(arguments)
    state = DurableToolCallState(
        task_key=key,
        agent_round=1,
        request_index=1,
        call_id="tool-call-1",
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
            call_id="tool-call-1",
            tool_name="video.get_segment",
            status=ToolResultStatus.SUCCESS,
            payload={"segmentId": "segment-1"},
        ),
    )


async def _completed_tool_record(service: ExecutionRecordService, key: TaskKey):
    record = await service.start_or_resume(
        key,
        media_identity="media-7",
        source_revision=REVISION,
    )
    await service.record_retrieval_selection(
        record.execution_id,
        [
            {
                "segmentId": "segment-1",
                "chunkId": "chunk-1",
                "sourceRevision": REVISION,
                "startMs": 0,
                "endMs": 2_000,
                "sourceItemIds": ["item-1"],
            }
        ],
        purpose="initial",
        logical_event_id="retrieval.initial",
    )
    plan = AgentPlan(understoodGoal=key.goal, tasks=("inspect",))
    await service.record_plan(record.execution_id, plan, logical_event_id="plan.initial")
    state = _stored_tool_state(key)
    request = state.request
    await service.record_executor_turn(
        record.execution_id,
        ExecutorTurn(kind=ExecutorTurnKind.TOOL_REQUEST, tool_request=request),
        logical_event_id="executor.tool.1",
        agent_round=1,
        request_index=1,
        args_digest=state.canonical_args_digest,
    )
    result = state.tool_result
    assert result is not None
    await service.record_tool_reference(
        record.execution_id,
        logical_event_id="tool.tool-call-1.result",
        agent_round=1,
        call_id="tool-call-1",
        request_index=1,
        tool_name="video.get_segment",
        args_digest=state.canonical_args_digest or "",
        policy_decision=PolicyDecision.ALLOW.value,
        reason_code=None,
        result_status=result.status.value,
    )
    final = _result()
    await service.record_executor_turn(
        record.execution_id,
        ExecutorTurn(kind=ExecutorTurnKind.FINAL, final_result=final),
        logical_event_id="executor.final",
        agent_round=1,
    )
    critic = CriticResult(passed=True, feedback=("grounded",))
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
    completed = await service.complete(
        record.execution_id,
        AgentState(
            goal=key.goal,
            plan=plan,
            result=final,
            critique=critic,
            round=1,
        ),
    )
    return completed, empty_tool_state_ledger(key).with_record(state)


def _composition():
    repository = InMemoryExecutionRecordRepository()
    records = ExecutionRecordService(
        repository,
        id_factory=lambda: "execution-x2d-owner",
        clock=lambda: datetime(2026, 9, 22, tzinfo=timezone.utc),
    )
    key = TaskKey(7, "inspect the video", AnalysisMode.GENERAL)
    completed = asyncio.run(_completed_record(records, key))
    replay = HistoricalAgentReplayService(records)
    media = _Media()
    access = HistoricalReplayAccessService(replay, records, media)
    return _Services(access, media), completed


def test_x2d_owner_read_privileged_initiation_and_cross_user_boundary():
    services, completed = _composition()
    owner = {"Authorization": "Bearer owner-token"}
    operator = {"Authorization": "Bearer operator-token"}
    admin = {"Authorization": "Bearer admin-token"}
    before = services.replay_access._execution_records.repository.get(completed.execution_id)

    with TestClient(create_app(services=services)) as client:
        metadata = client.get(
            f"/analysis/executions/{completed.execution_id}", headers=owner
        )
        assert metadata.status_code == 200
        assert metadata.json()["data"]["executionId"] == completed.execution_id
        assert metadata.json()["data"]["historicalReplayAvailable"] is True

        first = client.get(
            f"/analysis/executions/{completed.execution_id}/replay", headers=owner
        )
        second = client.get(
            f"/analysis/executions/{completed.execution_id}/replay", headers=owner
        )
        assert first.status_code == second.status_code == 200
        assert first.json()["data"]["resultDigest"] == second.json()["data"]["resultDigest"]
        assert first.json()["data"]["historicalFinalResult"]["title"] == "historical answer"
        assert "mediaIdentity" not in first.json()["data"]
        assert "state" not in first.json()["data"]

        privileged = client.post(
            f"/admin/executions/{completed.execution_id}/replay", headers=operator
        )
        assert privileged.status_code == 200
        assert privileged.json()["data"]["resultDigest"] == first.json()["data"]["resultDigest"]
        privileged_admin = client.post(
            f"/admin/executions/{completed.execution_id}/replay", headers=admin
        )
        assert privileged_admin.status_code == 200
        assert privileged_admin.json()["data"]["resultDigest"] == first.json()["data"]["resultDigest"]

        denied_read = client.get(
            f"/analysis/executions/{completed.execution_id}/replay",
            headers={"Authorization": "Bearer other-token"},
        )
        denied_metadata = client.get(
            f"/analysis/executions/{completed.execution_id}",
            headers={"Authorization": "Bearer other-token"},
        )
        denied_initiation = client.post(
            f"/admin/executions/{completed.execution_id}/replay", headers=owner
        )
        assert denied_read.status_code == denied_metadata.status_code == 404
        assert denied_initiation.status_code == 403

        assert services.media.calls == [
            (7, 7),
            (7, 7),
            (7, 7),
            (7, 8),
            (7, 8),
        ]
        assert services.dispatch_calls == 0
        assert services.failed_task_replay_calls == 0
    assert services.replay_access._execution_records.repository.get(completed.execution_id) == before


def test_x2d_unknown_and_incompatible_history_are_safe_api_failures():
    services, completed = _composition()
    headers = {"Authorization": "Bearer operator-token"}
    repository = services.replay_access._execution_records.repository
    incompatible = completed.model_copy(
        update={
            "execution_id": "execution-x2d-incompatible",
            "execution_contract_version": "future-contract",
            "events": tuple(
                event.model_copy(update={"execution_id": "execution-x2d-incompatible"})
                for event in completed.events
            ),
        }
    )
    repository.create(incompatible)

    with TestClient(create_app(services=services)) as client:
        missing = client.post(
            "/admin/executions/does-not-exist/replay", headers=headers
        )
        assert missing.status_code == 404
        invalid = client.post(
            "/admin/executions/execution-x2d-incompatible/replay", headers=headers
        )
        assert invalid.status_code == 409
        assert "future-contract" not in invalid.text
        assert "traceback" not in invalid.text.lower()


def test_x2d_replay_api_has_no_provider_or_internal_material():
    services, completed = _composition()
    with TestClient(create_app(services=services)) as client:
        response = client.get(
            f"/analysis/executions/{completed.execution_id}/replay",
            headers={"Authorization": "Bearer owner-token"},
        )
    assert response.status_code == 200
    text = response.text.lower()
    for forbidden in ("providerkey", "authorization", "credentials", "rawresponse", "traceback"):
        assert forbidden not in text


def test_x2d_replay_access_does_not_require_redis_or_current_checkpoint():
    services, completed = _composition()
    # The composition intentionally gives X2-C no checkpoint/Redis adapter.
    # A completed final record remains directly replayable from X2-B.
    assert services.replay_access._historical_replay._tool_checkpoint is None
    with TestClient(create_app(services=services)) as client:
        response = client.get(
            f"/analysis/executions/{completed.execution_id}/replay",
            headers={"Authorization": "Bearer owner-token"},
        )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == ExecutionRecordStatus.COMPLETED.value


def test_x2d_tool_history_joins_the_durable_ledger_without_tool_execution():
    repository = InMemoryExecutionRecordRepository()
    records = ExecutionRecordService(
        repository,
        id_factory=lambda: "execution-x2d-tool",
        clock=lambda: datetime(2026, 9, 22, tzinfo=timezone.utc),
    )
    key = TaskKey(7, "historical tool task", AnalysisMode.GENERAL)
    completed, ledger = asyncio.run(_completed_tool_record(records, key))
    reader = _HistoricalLedgerReader(ledger)
    media = _Media()
    access = HistoricalReplayAccessService(
        HistoricalAgentReplayService(records, tool_checkpoint=reader),
        records,
        media,
    )
    services = _Services(access, media)

    before = repository.get(completed.execution_id)
    with TestClient(create_app(services=services)) as client:
        response = client.get(
            f"/analysis/executions/{completed.execution_id}/replay",
            headers={"Authorization": "Bearer owner-token"},
        )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["historicalToolCalls"][0]["toolResult"]["payload"] == {
        "segmentId": "segment-1"
    }
    assert reader.loads == [key]
    assert reader.saves == 0
    assert repository.get(completed.execution_id) == before
    assert services.dispatch_calls == 0


def test_x2d_failed_history_is_returned_without_fabricating_a_final_result():
    repository = InMemoryExecutionRecordRepository()
    records = ExecutionRecordService(
        repository,
        id_factory=lambda: "execution-x2d-failed",
        clock=lambda: datetime(2026, 9, 22, tzinfo=timezone.utc),
    )
    key = TaskKey(7, "failed historical task", AnalysisMode.GENERAL)
    started = asyncio.run(
        records.start_or_resume(
            key,
            media_identity="media-7",
            source_revision=REVISION,
        )
    )
    failed = asyncio.run(records.fail(started.execution_id, ValueError("provider detail")))
    media = _Media()
    access = HistoricalReplayAccessService(
        HistoricalAgentReplayService(records),
        records,
        media,
    )

    with TestClient(create_app(services=_Services(access, media))) as client:
        response = client.get(
            f"/analysis/executions/{failed.execution_id}/replay",
            headers={"Authorization": "Bearer owner-token"},
        )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == ExecutionRecordStatus.FAILED.value
    assert data["failure"]["errorType"] == "ValueError"
    assert data["historicalFinalResult"] is None
