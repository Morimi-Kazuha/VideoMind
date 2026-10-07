"""Bounded administrative inspection and replay of terminal failed tasks.

This service deliberately delegates execution to the existing production
submission boundary. It never calls ``TaskWorker`` directly and never creates
a replacement task identity.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from dovideo.application.analysis_task_keys import normalize_content_hash
from dovideo.domain import AnalysisMode, TaskStage, TaskStatusState

from .value_objects import DispatchDisposition, TaskKey


_CONCRETE_MODES = frozenset(
    {"GENERAL", "LEARNING", "REVIEW", "CREATION"}
)
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{16,128}\Z")


class FailedTaskAdminError(RuntimeError):
    """Safe expected error at the failed-task administrative boundary."""

    status_code = 409


class FailedTaskAdminNotFound(FailedTaskAdminError):
    status_code = 404


class FailedTaskAdminBadRequest(FailedTaskAdminError):
    status_code = 400


class FailedTaskAdminConflict(FailedTaskAdminError):
    status_code = 409


class FailedTaskAdminRateLimited(FailedTaskAdminError):
    status_code = 429


class FailedTaskAdminUnavailable(FailedTaskAdminError):
    status_code = 503


@dataclass(frozen=True, slots=True)
class FailedTaskAdminView:
    record: Any
    replay_attempts: tuple[Any, ...]
    replay_status: str
    lifecycle: Any | None
    replay_eligible: bool | None = None
    ineligible_reason: str | None = None


@dataclass(frozen=True, slots=True)
class FailedTaskAdminPage:
    items: tuple[FailedTaskAdminView, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class FailedTaskReplayResult:
    view: FailedTaskAdminView
    accepted: bool


class FailedTaskAdminService:
    """Operator-only application service over the existing R4 components."""

    def __init__(
        self,
        failed_store: Any,
        media: Any,
        lifecycle: Any,
        checkpoint: Any,
        handoff: Any,
        submit_analysis: Callable[..., Awaitable[DispatchDisposition]],
    ) -> None:
        self._failed_store = failed_store
        self._media = media
        self._lifecycle = lifecycle
        self._checkpoint = checkpoint
        self._handoff = handoff
        self._submit_analysis = submit_analysis

    async def list_failed(self, *, limit: int = 50, offset: int = 0) -> FailedTaskAdminPage:
        try:
            records, total = await asyncio.to_thread(
                self._failed_store.list_failed,
                limit=limit,
                offset=offset,
            )
            views = tuple([await self._view(record, history_limit=1) for record in records])
            return FailedTaskAdminPage(views, int(total), limit, offset)
        except FailedTaskAdminError:
            raise
        except Exception as exc:
            raise FailedTaskAdminUnavailable("失败任务暂不可读取") from exc

    async def inspect(self, task_id: int) -> FailedTaskAdminView:
        record = await self._get_record(task_id)
        view = await self._view(record, history_limit=20, inspect_eligibility=True)
        return view

    async def replay(self, task_id: int, idempotency_key: str) -> FailedTaskReplayResult:
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise FailedTaskAdminBadRequest("Idempotency-Key 格式无效")
        record = await self._get_record(task_id)

        existing = await self._store_call(
            self._failed_store.get_replay_attempt,
            task_id,
            idempotency_key,
        )
        if existing is not None:
            return await self._resume_idempotent(record, existing, idempotency_key)

        key, media_record, lifecycle = await self._assert_replayable(record)
        await self._sync_latest_attempt(record, lifecycle)
        reservation = await self._store_call(
            self._failed_store.reserve_replay,
            task_id,
            idempotency_key,
        )
        if reservation.conflict == "NOT_FOUND":
            raise FailedTaskAdminNotFound("失败任务不存在")
        if reservation.conflict == "IN_PROGRESS":
            raise FailedTaskAdminConflict("该失败任务已有重放正在进行")
        if reservation.conflict == "ALREADY_SUCCEEDED":
            raise FailedTaskAdminConflict("该任务已有成功重放，不能再次重放")
        attempt = reservation.attempt
        if attempt is None:
            raise FailedTaskAdminUnavailable("失败任务重放状态暂不可用")
        if not _same_mode_key(record, key):
            await self._mark(attempt.attempt_id, "CONFLICT")
            raise FailedTaskAdminConflict("失败任务身份无法安全重建")
        return await self._dispatch(record, attempt, key, media_record)

    async def _resume_idempotent(
        self,
        record: Any,
        attempt: Any,
        idempotency_key: str,
    ) -> FailedTaskReplayResult:
        if attempt.status == "SUCCEEDED":
            return FailedTaskReplayResult(
                await self._view(record, history_limit=20),
                accepted=False,
            )
        if attempt.status in {"FAILED_AGAIN", "CONFLICT"}:
            raise FailedTaskAdminConflict("该重放请求已结束；再次重放须使用新的幂等键")

        lifecycle = await self._load_lifecycle(record)
        if lifecycle is not None and lifecycle.request_id == attempt.attempt_id:
            if lifecycle.state is TaskStatusState.COMPLETED:
                await self._mark(attempt.attempt_id, "SUCCEEDED")
                return FailedTaskReplayResult(
                    await self._view(record, history_limit=20),
                    accepted=False,
                )
            if lifecycle.state is TaskStatusState.FAILED:
                if lifecycle.stage is TaskStage.DISPATCH_FAILED:
                    await self._mark(attempt.attempt_id, "DISPATCH_FAILED")
                elif lifecycle.stage is TaskStage.DEAD_LETTERED:
                    await self._mark(attempt.attempt_id, "FAILED_AGAIN")
                    raise FailedTaskAdminConflict("该任务在重放后再次失败")
            if lifecycle.state in {TaskStatusState.QUEUED, TaskStatusState.PROCESSING}:
                await self._mark(attempt.attempt_id, "DISPATCHED")
                return FailedTaskReplayResult(
                    await self._view(record, history_limit=20),
                    accepted=True,
                )

        if attempt.status == "DISPATCHED":
            raise FailedTaskAdminConflict("该重放已投递，当前状态需先完成恢复核验")

        key, media_record, current = await self._assert_replayable(
            record,
            allow_dispatch_failed_attempt=attempt,
        )
        reservation = await self._store_call(
            self._failed_store.reserve_replay,
            int(record.task_id),
            idempotency_key,
        )
        if reservation.conflict:
            raise FailedTaskAdminConflict("该失败任务已有其他重放正在进行")
        resumed = reservation.attempt
        if resumed is None:
            raise FailedTaskAdminUnavailable("失败任务重放状态暂不可用")
        return await self._dispatch(record, resumed, key, media_record)

    async def _dispatch(
        self,
        record: Any,
        attempt: Any,
        key: TaskKey,
        media_record: Any,
    ) -> FailedTaskReplayResult:
        try:
            disposition = await self._submit_analysis(
                key.media_id,
                int(media_record.user_id),
                key.goal,
                key.mode,
                request_id=attempt.attempt_id,
            )
        except FailedTaskAdminError:
            raise
        except Exception as exc:
            await self._mark(attempt.attempt_id, "DISPATCH_FAILED", type(exc).__name__)
            # Preserve established API errors (404 on deleted media, etc.)
            # without returning driver, broker, or configuration details.
            if hasattr(exc, "status_code") and int(getattr(exc, "status_code")) in {400, 403, 404, 409}:
                raise
            raise FailedTaskAdminUnavailable("重放暂未能进入分析队列") from exc

        if disposition is DispatchDisposition.ACCEPTED:
            await self._mark(attempt.attempt_id, "DISPATCHED")
            return FailedTaskReplayResult(
                await self._view(record, history_limit=20),
                accepted=True,
            )
        if disposition is DispatchDisposition.DUPLICATE:
            current = await self._load_lifecycle(record)
            if current is not None and current.request_id == attempt.attempt_id:
                if current.state is TaskStatusState.COMPLETED:
                    await self._mark(attempt.attempt_id, "SUCCEEDED")
                    return FailedTaskReplayResult(
                        await self._view(record, history_limit=20),
                        accepted=False,
                    )
                if current.state in {TaskStatusState.QUEUED, TaskStatusState.PROCESSING}:
                    await self._mark(attempt.attempt_id, "DISPATCHED")
                    return FailedTaskReplayResult(
                        await self._view(record, history_limit=20),
                        accepted=True,
                    )
                if current.state is TaskStatusState.FAILED and current.stage is TaskStage.DEAD_LETTERED:
                    await self._mark(attempt.attempt_id, "FAILED_AGAIN")
                    raise FailedTaskAdminConflict("该任务在重放后再次失败")
            await self._mark(attempt.attempt_id, "CONFLICT")
            raise FailedTaskAdminConflict("相同任务已有其他提交，未重复投递")
        if disposition is DispatchDisposition.RATE_LIMITED:
            # Legacy/custom submitter compatibility, not a current dispatch quota.
            await self._mark(attempt.attempt_id, "DISPATCH_FAILED", "DispatchRateLimited")
            raise FailedTaskAdminRateLimited("重放提交受现有任务配额限制")

        await self._mark(attempt.attempt_id, "DISPATCH_FAILED", "DispatchUnavailable")
        raise FailedTaskAdminUnavailable("重放暂未能进入分析队列")

    async def _assert_replayable(
        self,
        record: Any,
        *,
        allow_dispatch_failed_attempt: Any | None = None,
    ) -> tuple[TaskKey, Any, Any]:
        key = _record_task_key(record)
        if key is None:
            raise FailedTaskAdminConflict("失败记录缺少可重建的任务身份")
        if record.action != "START_ANALYSIS":
            raise FailedTaskAdminConflict("该失败记录不是可重放的分析任务")
        if str(record.status).upper() not in {"FAILED", "DEAD_LETTER_PENDING", "DEAD_LETTERED"}:
            raise FailedTaskAdminConflict("失败记录当前状态不可重放")

        try:
            media_record = await self._media.get(key.media_id)
            if media_record is None:
                raise FailedTaskAdminConflict("原视频已不存在，不能重放")
            if not getattr(media_record, "user_id", None) or int(media_record.user_id) <= 0:
                raise FailedTaskAdminConflict("原视频所有权关系无效，不能重放")
            current_hash = normalize_content_hash(key.media_id, media_record.content_hash)
            if current_hash != str(record.content_hash):
                raise FailedTaskAdminConflict("原视频内容已变化，不能重放该历史任务")

            lifecycle = await self._lifecycle.load_lifecycle(key)
            dispatch_failed_retry = (
                allow_dispatch_failed_attempt is not None
                and lifecycle is not None
                and lifecycle.request_id == allow_dispatch_failed_attempt.attempt_id
                and lifecycle.state is TaskStatusState.FAILED
                and lifecycle.stage is TaskStage.DISPATCH_FAILED
            )
            if not dispatch_failed_retry and (
                lifecycle is None
                or lifecycle.state is not TaskStatusState.FAILED
                or lifecycle.stage is not TaskStage.DEAD_LETTERED
            ):
                raise FailedTaskAdminConflict("任务生命周期不是终态失败/死信状态")
            if await self._checkpoint.load_result(key) is not None:
                raise FailedTaskAdminConflict("已有成功结果，不能重放")
            if await self._handoff.load_pending(key) is not None:
                raise FailedTaskAdminConflict("失败任务的 DLQ handoff 尚未完成")
            return key, media_record, lifecycle
        except FailedTaskAdminError:
            raise
        except Exception as exc:
            raise FailedTaskAdminUnavailable("失败任务资格暂不可核验") from exc

    async def _sync_latest_attempt(self, record: Any, lifecycle: Any | None) -> None:
        attempts = await self._store_call(
            self._failed_store.list_replay_attempts,
            int(record.task_id),
            limit=1,
        )
        if not attempts:
            return
        latest = attempts[0]
        if latest.status != "DISPATCHED" or lifecycle is None:
            return
        if lifecycle.request_id != latest.attempt_id:
            return
        if lifecycle.state is TaskStatusState.COMPLETED:
            await self._mark(latest.attempt_id, "SUCCEEDED")
        elif lifecycle.state is TaskStatusState.FAILED and lifecycle.stage is TaskStage.DEAD_LETTERED:
            await self._mark(latest.attempt_id, "FAILED_AGAIN")

    async def _view(
        self,
        record: Any,
        *,
        history_limit: int,
        inspect_eligibility: bool = False,
    ) -> FailedTaskAdminView:
        refreshed = await self._store_call(
            self._failed_store.get_failed,
            int(record.task_id),
        )
        if refreshed is not None:
            record = refreshed
        attempts = await self._store_call(
            self._failed_store.list_replay_attempts,
            int(record.task_id),
            limit=history_limit,
        )
        latest = attempts[0] if attempts else None
        lifecycle = await self._load_lifecycle(record)
        status = "NEVER_REPLAYED" if latest is None else latest.status
        if latest is not None:
            if lifecycle is not None and lifecycle.request_id == latest.attempt_id:
                if lifecycle.state is TaskStatusState.COMPLETED:
                    await self._mark(latest.attempt_id, "SUCCEEDED")
                    status = "SUCCEEDED"
                elif lifecycle.state is TaskStatusState.FAILED and lifecycle.stage is TaskStage.DEAD_LETTERED:
                    await self._mark(latest.attempt_id, "FAILED_AGAIN")
                    status = "FAILED_AGAIN"
                elif lifecycle.state in {TaskStatusState.QUEUED, TaskStatusState.PROCESSING}:
                    status = "RUNNING"
                elif lifecycle.state is TaskStatusState.FAILED and lifecycle.stage is TaskStage.DISPATCH_FAILED:
                    await self._mark(latest.attempt_id, "DISPATCH_FAILED")
                    status = "DISPATCH_FAILED"
            status = {
                "REQUESTED": "REQUESTED",
                "DISPATCHED": "DISPATCHED",
                "RUNNING": "RUNNING",
                "DISPATCH_FAILED": "DISPATCH_FAILED",
                "SUCCEEDED": "SUCCEEDED",
                "FAILED_AGAIN": "FAILED_AGAIN",
                "CONFLICT": "CONFLICT",
            }.get(status, "UNKNOWN")

        eligible: bool | None = None
        reason: str | None = None
        if inspect_eligibility:
            try:
                await self._assert_replayable(record)
                eligible = True
            except FailedTaskAdminConflict as exc:
                eligible = False
                reason = str(exc)
            except FailedTaskAdminUnavailable:
                eligible = False
                reason = "暂时无法核验"
        return FailedTaskAdminView(
            record=record,
            replay_attempts=attempts,
            replay_status=status,
            lifecycle=lifecycle,
            replay_eligible=eligible,
            ineligible_reason=reason,
        )

    async def _load_lifecycle(self, record: Any) -> Any | None:
        key = _record_task_key(record)
        if key is None:
            return None
        try:
            return await self._lifecycle.load_lifecycle(key)
        except Exception as exc:
            raise FailedTaskAdminUnavailable("任务生命周期暂不可读取") from exc

    async def _get_record(self, task_id: int) -> Any:
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
            raise FailedTaskAdminBadRequest("失败任务编号无效")
        record = await self._store_call(self._failed_store.get_failed, task_id)
        if record is None:
            raise FailedTaskAdminNotFound("失败任务不存在")
        return record

    async def _store_call(self, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(operation, *args, **kwargs)
        except FailedTaskAdminError:
            raise
        except ValueError as exc:
            raise FailedTaskAdminBadRequest("失败任务请求无效") from exc
        except Exception as exc:
            raise FailedTaskAdminUnavailable("失败任务存储暂不可用") from exc

    async def _mark(self, attempt_id: str, status: str, error_type: str | None = None) -> Any:
        return await self._store_call(
            self._failed_store.mark_replay_attempt,
            attempt_id,
            status,
            error_type=error_type,
        )


def _record_task_key(record: Any) -> TaskKey | None:
    try:
        mode_value = str(record.mode).strip().upper()
        if mode_value not in _CONCRETE_MODES:
            return None
        media_id = int(record.media_id)
        goal = record.user_goal
        if media_id <= 0 or not isinstance(goal, str) or not goal.strip() or len(goal) > 500:
            return None
        return TaskKey(media_id, goal, AnalysisMode(mode_value))
    except (AttributeError, TypeError, ValueError):
        return None


def _same_mode_key(record: Any, key: TaskKey) -> bool:
    return _record_task_key(record) == key


__all__ = [
    "FailedTaskAdminBadRequest",
    "FailedTaskAdminConflict",
    "FailedTaskAdminError",
    "FailedTaskAdminNotFound",
    "FailedTaskAdminPage",
    "FailedTaskAdminRateLimited",
    "FailedTaskAdminService",
    "FailedTaskAdminUnavailable",
    "FailedTaskAdminView",
    "FailedTaskReplayResult",
]
