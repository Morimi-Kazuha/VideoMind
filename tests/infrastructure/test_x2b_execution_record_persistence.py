from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine

from dovideo.application import (
    ExecutionEventType,
    ExecutionRecordConflictError,
    ExecutionRecordService,
    ExecutionRecordStatus,
    TaskKey,
)
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    CriticResult,
)
from dovideo.infrastructure.persistence.sqlalchemy import (
    SqlAlchemyExecutionRecordRepository,
    create_schema,
)


def _engine(path):
    return create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )


@pytest.mark.asyncio
async def test_execution_record_is_ordered_idempotent_and_recoverable_after_reopen(
    tmp_path,
) -> None:
    path = tmp_path / "execution-record.sqlite3"
    engine = _engine(path)
    create_schema(engine)
    first = ExecutionRecordService(
        SqlAlchemyExecutionRecordRepository(engine),
        id_factory=lambda: "execution-sqlite-1",
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    key = TaskKey(7, "find evidence", AnalysisMode.GENERAL)

    record = await first.start_or_resume(
        key,
        media_identity="sha256:media",
        source_revision="revision-1",
        worker_attempt=1,
        request_id="delivery-a",
    )
    plan = AgentPlan(understoodGoal=key.goal, tasks=("inspect",))
    await first.record_plan(record.execution_id, plan)
    selected = await first.record_retrieval_selection(
        record.execution_id,
        (
            {
                "segmentId": "segment-a",
                "chunkId": "chunk-a",
                "sourceRevision": "revision-1",
                "startMs": 0,
                "endMs": 1000,
            },
            {
                "segmentId": "segment-b",
                "chunkId": "chunk-b",
                "sourceRevision": "revision-1",
                "startMs": 1000,
                "endMs": 2000,
            },
        ),
        purpose="initial",
        logical_event_id="retrieval.initial",
    )
    assert selected.sequence_no == 3

    reopened_engine = _engine(path)
    reopened = ExecutionRecordService(
        SqlAlchemyExecutionRecordRepository(reopened_engine),
        id_factory=lambda: "must-not-be-used",
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    duplicate = await reopened.record_retrieval_selection(
        record.execution_id,
        (
            {
                "segmentId": "segment-a",
                "chunkId": "chunk-a",
                "sourceRevision": "revision-1",
                "startMs": 0,
                "endMs": 1000,
            },
            {
                "segmentId": "segment-b",
                "chunkId": "chunk-b",
                "sourceRevision": "revision-1",
                "startMs": 1000,
                "endMs": 2000,
            },
        ),
        purpose="initial",
        logical_event_id="retrieval.initial",
    )
    assert duplicate.sequence_no == selected.sequence_no

    with pytest.raises(ExecutionRecordConflictError):
        await reopened.append_event(
            record.execution_id,
            event_type=ExecutionEventType.RETRIEVAL_SELECTED,
            logical_event_id="retrieval.initial",
            payload={"selected": [{"segmentId": "conflicting"}]},
        )

    next_event = await reopened.append_event(
        record.execution_id,
        event_type=ExecutionEventType.CRITIC_RECORDED,
        logical_event_id="critic.round.1",
        agent_round=1,
        stage="CRITIC",
        payload={"critic": {"passed": True}},
    )
    assert next_event.sequence_no == 4

    state = AgentState(
        goal=key.goal,
        plan=plan,
        result=AnalysisResult(
            title="answer",
            conclusions=("claim",),
            evidence=(
                AnalysisEvidence(
                    timestampMs=0,
                    source="ASR",
                    content="claim",
                    claim="claim",
                    sourceRevision="revision-1",
                ),
            ),
        ),
        critique=CriticResult(passed=True),
        round=1,
    )
    completed = await reopened.complete(record.execution_id, state)
    assert completed.status is ExecutionRecordStatus.COMPLETED
    assert completed.replayable is True
    assert [event.sequence_no for event in completed.events] == [1, 2, 3, 4, 5]

    final_engine = _engine(path)
    final = SqlAlchemyExecutionRecordRepository(final_engine).get(record.execution_id)
    assert final is not None
    assert final.status is ExecutionRecordStatus.COMPLETED
    assert final.latest_sequence == 5
    assert final.events[2].payload["selected"][0]["segmentId"] == "segment-a"
    assert "claim" in str(final.events[-1].payload["finalResult"])

    engine.dispose()
    reopened_engine.dispose()
    final_engine.dispose()
