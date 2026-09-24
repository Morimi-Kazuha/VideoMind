"""Phase 9B durable dead-letter handoff adapter.

This adapter composes the existing Phase 8 ``CheckpointRepository``.  It
does not add a table, a second serializer, or a provider-specific client:
the repository's durable-first/cache-aside semantics are the source of
truth for pending handoffs.
"""

from __future__ import annotations

import asyncio
from typing import Any

from dovideo.application.analysis_task_keys import goal_digest
from dovideo.application.dead_letter_handoff import PendingDeadLetterHandoff
from dovideo.application.value_objects import TaskKey
from dovideo.domain import AnalysisMode, TaskStage


PENDING_DEAD_LETTER_FIELD = "deadLetterHandoff"


class CheckpointDeadLetterHandoffStore:
    """Store pending handoffs in the existing goal-level checkpoint space."""

    def __init__(self, repository: Any, *, ttl_seconds: float | None = None) -> None:
        if repository is None:
            raise ValueError("a checkpoint repository is required")
        self.repository = repository
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def checkpoint_name(key: TaskKey) -> str:
        digest = goal_digest(key.goal, key.mode)
        return f"goal:{digest}:{PENDING_DEAD_LETTER_FIELD}"

    @staticmethod
    def redis_key(key: TaskKey) -> str:
        digest = goal_digest(key.goal, key.mode)
        return f"agent:checkpoint:{key.media_id}:goal:{digest}"

    async def save_pending(self, handoff: PendingDeadLetterHandoff) -> None:
        if not isinstance(handoff, PendingDeadLetterHandoff):
            raise TypeError("handoff must be a PendingDeadLetterHandoff")
        await asyncio.to_thread(
            self.repository.write_standalone,
            handoff.media_id,
            self.checkpoint_name(handoff.task_key),
            self.redis_key(handoff.task_key),
            PENDING_DEAD_LETTER_FIELD,
            TaskStage.DEAD_LETTERED,
            handoff,
        )

    async def load_pending(self, key: TaskKey) -> PendingDeadLetterHandoff | None:
        if not isinstance(key, TaskKey):
            raise TypeError("key must be a TaskKey")
        return await asyncio.to_thread(
            self.repository.read,
            key.media_id,
            self.checkpoint_name(key),
            self.redis_key(key),
            PENDING_DEAD_LETTER_FIELD,
            PendingDeadLetterHandoff,
        )

    async def clear_pending(self, key: TaskKey) -> None:
        if not isinstance(key, TaskKey):
            raise TypeError("key must be a TaskKey")
        # Delete only the pending durable record.  Passing redis_key to the
        # generic delete would remove every goal-level hot-cache field.
        await asyncio.to_thread(
            self.repository.delete,
            key.media_id,
            self.checkpoint_name(key),
        )
        try:
            evict = getattr(self.repository, "_cache_delete_fields")
            await asyncio.to_thread(
                evict,
                self.redis_key(key),
                PENDING_DEAD_LETTER_FIELD,
            )
        except Exception:
            # Durable clear succeeded; a stale cache entry is not authoritative
            # and the repository will evict it on the next typed read.
            return

    # Short aliases make the adapter easy to inject into small composition
    # roots while the explicit methods satisfy TaskDeadLetterHandoffPort.
    save = save_pending
    load = load_pending
    clear = clear_pending


DurableDeadLetterHandoffStore = CheckpointDeadLetterHandoffStore
RepositoryDeadLetterHandoffStore = CheckpointDeadLetterHandoffStore


__all__ = [
    "CheckpointDeadLetterHandoffStore",
    "DurableDeadLetterHandoffStore",
    "PENDING_DEAD_LETTER_FIELD",
    "RepositoryDeadLetterHandoffStore",
]
