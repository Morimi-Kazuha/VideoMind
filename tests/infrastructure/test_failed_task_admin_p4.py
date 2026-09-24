from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine, select

from dovideo.infrastructure.persistence.sqlalchemy import (
    FailedAnalysisTaskReplayRow,
    FailedTaskRecord,
    SqlAlchemyFailedTaskStore,
    create_schema,
)


def _record(media_id: int, goal: str) -> FailedTaskRecord:
    return FailedTaskRecord(
        media_id=media_id,
        action="START_ANALYSIS",
        mode="GENERAL",
        content_hash=f"{media_id:032x}",
        user_goal=goal,
        attempt_count=3,
        error_type="ValueError",
        error_message="original failure remains private",
        status="DEAD_LETTER_PENDING",
    )


@pytest.fixture
def failed_store(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'failed-task-admin.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    create_schema(engine)
    store = SqlAlchemyFailedTaskStore(engine)
    yield store, engine
    engine.dispose()


def test_failed_task_page_is_bounded_ordered_and_keeps_historical_failure(failed_store):
    store, _engine = failed_store
    first = store.record(_record(11, "first goal"))
    second = store.record(_record(12, "second goal"))

    page, total = store.list_failed(limit=1, offset=0)

    assert total == 2
    assert len(page) == 1
    assert page[0].task_id == second.task_id
    assert store.get_failed(first.task_id).user_goal == "first goal"
    with pytest.raises(ValueError):
        store.list_failed(limit=101, offset=0)
    assert store.get_failed(9999) is None
    assert store.reserve_replay(9999, "missing-task-key-0001").conflict == "NOT_FOUND"


def test_replay_attempts_are_idempotent_append_only_and_status_is_durable(failed_store):
    store, engine = failed_store
    original = store.record(_record(21, "preserve this goal"))
    key_one = "operator-request-key-0001"
    key_two = "operator-request-key-0002"

    first = store.reserve_replay(original.task_id, key_one).attempt
    duplicate = store.reserve_replay(original.task_id, key_one).attempt
    assert first is not None and duplicate is not None
    assert duplicate.attempt_id == first.attempt_id
    assert duplicate.attempt_number == 1
    assert store.mark_replay_attempt(first.attempt_id, "DISPATCHED").status == "DISPATCHED"
    assert store.reserve_replay(original.task_id, key_two).conflict == "IN_PROGRESS"

    store.mark_replay_attempt(first.attempt_id, "FAILED_AGAIN", error_type="ValueError")
    second = store.reserve_replay(original.task_id, key_two).attempt
    assert second is not None
    assert second.attempt_id != first.attempt_id
    assert second.attempt_number == 2

    replay_rows = store.list_replay_attempts(original.task_id, limit=20)
    assert [item.attempt_id for item in replay_rows] == [second.attempt_id, first.attempt_id]
    historical = store.get_failed(original.task_id)
    assert historical.user_goal == "preserve this goal"
    assert historical.error_type == "ValueError"
    assert historical.error_message == "original failure remains private"
    assert historical.replay_attempt_count == 2
    assert historical.replay_status == "REQUESTED"

    with engine.connect() as connection:
        persisted_digest = connection.scalar(
            select(FailedAnalysisTaskReplayRow.idempotency_digest).where(
                FailedAnalysisTaskReplayRow.attempt_id == first.attempt_id
            )
        )
    assert persisted_digest == hashlib.sha256(key_one.encode("utf-8")).hexdigest()
    assert key_one not in persisted_digest


def test_dispatch_failure_can_resume_same_attempt_with_same_idempotency_key(failed_store):
    store, _engine = failed_store
    original = store.record(_record(31, "recover dispatch"))
    key = "retry-dispatch-key-0001"

    attempt = store.reserve_replay(original.task_id, key).attempt
    assert attempt is not None
    store.mark_replay_attempt(attempt.attempt_id, "DISPATCH_FAILED", error_type="BrokerError")

    resumed = store.reserve_replay(original.task_id, key).attempt

    assert resumed is not None
    assert resumed.attempt_id == attempt.attempt_id
    assert resumed.attempt_number == 1
    assert resumed.status == "REQUESTED"

