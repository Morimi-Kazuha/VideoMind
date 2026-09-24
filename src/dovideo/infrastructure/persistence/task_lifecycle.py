"""SQLAlchemy/Redis-backed task lifecycle snapshots for the R3 worker.

This adapter stores the existing :class:`TaskLifecycle` value in the existing
durable-first checkpoint repository.  It does not introduce another task
state machine: the application lifecycle object remains the only authority
for status and business delivery attempts.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from dovideo.application.analysis_task_keys import goal_digest
from dovideo.application.ports.tasks import TaskLifecyclePort
from dovideo.application.task_lifecycle import TaskLifecycle
from dovideo.application.value_objects import TaskKey
from dovideo.domain import AnalysisMode, TaskStage, TaskStatus, TaskStatusState


LIFECYCLE_CHECKPOINT_PREFIX = "task:lifecycle"
LIFECYCLE_FIELD = "lifecycle"
DEFAULT_LIFECYCLE_MAX_ATTEMPTS = 3


class TaskLifecyclePersistenceError(RuntimeError):
    """Raised when a lifecycle snapshot cannot be read or written safely."""


class CheckpointTaskLifecycleStore(TaskLifecyclePort):
    """Persist lifecycle snapshots through the existing R2 repository.

    ``repository`` is the same ``CheckpointRepository`` used by
    ``AgentCheckpointService``.  Its durable SQLAlchemy store is written first
    and its Redis hash is only a read-through cache, preserving the R2 source
    of truth and cache-loss recovery rules.
    """

    def __init__(
        self,
        repository: Any,
        *,
        redis_client: Any | None = None,
        max_attempts: int = DEFAULT_LIFECYCLE_MAX_ATTEMPTS,
        failure_once_key: str | None = None,
    ) -> None:
        if repository is None:
            raise ValueError("a checkpoint repository is required")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("max_attempts must be an integer")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self.repository = repository
        self.redis_client = redis_client
        self.max_attempts = max_attempts
        self.failure_once_key = failure_once_key

    @classmethod
    def from_environment(
        cls,
        repository: Any,
        *,
        redis_client: Any | None = None,
    ) -> "CheckpointTaskLifecycleStore":
        """Create the normal production store and optional explicit test hook."""

        return cls(
            repository,
            redis_client=redis_client,
            max_attempts=_positive_int(
                os.environ.get("DOVIDEO_TASK_MAX_ATTEMPTS"),
                DEFAULT_LIFECYCLE_MAX_ATTEMPTS,
            ),
            failure_once_key=os.environ.get("DOVIDEO_R3_FAIL_COMPLETION_SAVE_KEY") or None,
        )

    @staticmethod
    def checkpoint_name(key: TaskKey) -> str:
        return f"{LIFECYCLE_CHECKPOINT_PREFIX}:{key.media_id}:{goal_digest(key.goal, key.mode)}"

    @classmethod
    def redis_key(cls, key: TaskKey) -> str:
        return cls.checkpoint_name(key)

    async def load_lifecycle(self, key: TaskKey) -> TaskLifecycle | None:
        try:
            value = await asyncio.to_thread(
                self.repository.read,
                key.media_id,
                self.checkpoint_name(key),
                self.redis_key(key),
                LIFECYCLE_FIELD,
                object,
            )
        except Exception as exc:
            raise TaskLifecyclePersistenceError("读取任务生命周期失败") from exc
        if value is None:
            return None
        try:
            return self._from_payload(key, value)
        except Exception as exc:
            raise TaskLifecyclePersistenceError("任务生命周期快照格式无效") from exc

    async def save_lifecycle(self, lifecycle: TaskLifecycle) -> None:
        if not isinstance(lifecycle, TaskLifecycle):
            raise TypeError("lifecycle must be a TaskLifecycle")
        if self._should_inject_completion_failure(lifecycle):
            # This is an opt-in crash-consistency test hook.  It fails before
            # the completion row is written, after TaskWorker has already
            # saved the result, so the next delivery must recover the result
            # without invoking AgentLoop again.
            raise RuntimeError("injected completion lifecycle save failure")
        try:
            await asyncio.to_thread(
                self.repository.write_standalone,
                lifecycle.key.media_id,
                self.checkpoint_name(lifecycle.key),
                self.redis_key(lifecycle.key),
                LIFECYCLE_FIELD,
                lifecycle.stage.value if lifecycle.stage is not None else "",
                self._payload(lifecycle),
            )
        except Exception as exc:
            raise TaskLifecyclePersistenceError("保存任务生命周期失败") from exc

    def _should_inject_completion_failure(self, lifecycle: TaskLifecycle) -> bool:
        if lifecycle.state is not TaskStatusState.COMPLETED:
            return False
        configured = self.failure_once_key
        if not configured:
            return False
        configured_digest = goal_digest(lifecycle.key.goal, lifecycle.key.mode)
        if configured not in {
            configured_digest,
            f"{lifecycle.key.media_id}:{configured_digest}",
            self.checkpoint_name(lifecycle.key),
        }:
            return False
        if self.redis_client is None:
            return True
        marker = f"r3:inject:completion-save:{lifecycle.key.media_id}:{configured_digest}"
        return bool(
            self.redis_client.set(
                marker,
                "1",
                nx=True,
                ex=24 * 60 * 60,
            )
        )

    @staticmethod
    def _payload(lifecycle: TaskLifecycle) -> dict[str, Any]:
        return {
            "key": {
                "mediaId": lifecycle.key.media_id,
                "goal": lifecycle.key.goal,
                "mode": lifecycle.key.mode.value,
            },
            "status": {
                "state": (
                    None
                    if lifecycle.status.state is None
                    else lifecycle.status.state.value
                ),
                "result": lifecycle.status.result,
                "message": lifecycle.status.message,
            },
            "stage": None if lifecycle.stage is None else lifecycle.stage.value,
            "attempt": lifecycle.attempt,
            "maxAttempts": lifecycle.max_attempts,
            "retryable": lifecycle.retryable,
            "requestId": lifecycle.request_id,
        }

    @staticmethod
    def _from_payload(expected_key: TaskKey, value: Any) -> TaskLifecycle:
        if not isinstance(value, dict):
            raise ValueError("lifecycle payload must be an object")
        identity = value.get("key")
        if not isinstance(identity, dict):
            raise ValueError("lifecycle key is missing")
        loaded_key = TaskKey(
            int(identity["mediaId"]),
            str(identity["goal"]),
            AnalysisMode.from_nullable(identity.get("mode")),
        )
        if loaded_key != expected_key:
            raise ValueError("lifecycle key does not match checkpoint key")
        status_value = value.get("status")
        if not isinstance(status_value, dict):
            raise ValueError("lifecycle status is missing")
        state_raw = status_value.get("state")
        state = None if state_raw is None else TaskStatusState(str(state_raw))
        stage_raw = value.get("stage")
        stage = None if stage_raw is None else TaskStage.from_value(str(stage_raw))
        if stage_raw is not None and stage is None:
            raise ValueError("lifecycle stage is invalid")
        return TaskLifecycle(
            key=loaded_key,
            status=TaskStatus(
                state=state,
                result=status_value.get("result"),
                message=status_value.get("message"),
            ),
            stage=stage,
            attempt=int(value.get("attempt", 0)),
            max_attempts=int(value.get("maxAttempts", DEFAULT_LIFECYCLE_MAX_ATTEMPTS)),
            retryable=bool(value.get("retryable", False)),
            request_id=(
                None
                if value.get("requestId") is None
                else str(value.get("requestId"))
            ),
        )


def _positive_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError("DOVIDEO_TASK_MAX_ATTEMPTS must be an integer") from exc
    if result < 1:
        raise ValueError("DOVIDEO_TASK_MAX_ATTEMPTS must be positive")
    return result


__all__ = [
    "CheckpointTaskLifecycleStore",
    "DEFAULT_LIFECYCLE_MAX_ATTEMPTS",
    "LIFECYCLE_CHECKPOINT_PREFIX",
    "LIFECYCLE_FIELD",
    "TaskLifecyclePersistenceError",
]
