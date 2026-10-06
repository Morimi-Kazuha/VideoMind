"""Per-delivery renewable lease; no broker or storage-provider dependencies."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from contextvars import ContextVar
from typing import Awaitable, Callable, TypeVar

from .ports.tasks import TaskLockPort
from .value_objects import TaskKey

_logger = logging.getLogger(__name__)
_current: ContextVar[TaskLeaseKeeper | None] = ContextVar("analysis_task_lease", default=None)
_Result = TypeVar("_Result")


class TaskLeaseUnavailable(RuntimeError):
    """Transport recovery is required; the old owner must not write failure."""


class TaskLeaseLost(TaskLeaseUnavailable):
    """The provider explicitly rejected this owner's token."""


class TaskLeaseExpired(TaskLeaseUnavailable):
    """Ownership could not be established before the last known expiry."""


def check_task_lease() -> None:
    """Guard a persistence boundary, including AgentLoop's own checkpoints.

    Outside a worker delivery this is a no-op. Context propagates to provider
    child tasks and to_thread, without modifying AgentLoop business logic.
    This is a local guard, not a database fencing transaction.
    """
    lease = _current.get()
    if lease is not None:
        lease.check()


class TaskLeaseKeeper:
    def __init__(self, lock: TaskLockPort, key: TaskKey, token: object, *, acquired_at: float) -> None:
        self.lock, self.key, self.token = lock, key, token
        self.duration = lock.lease_seconds
        if self.duration is not None and (
            isinstance(self.duration, bool) or not isinstance(self.duration, (int, float))
            or not math.isfinite(self.duration) or self.duration <= 0
        ):
            raise ValueError("task lease duration must be finite and positive")
        self.deadline = math.inf if self.duration is None else acquired_at + self.duration
        self.failure: TaskLeaseUnavailable | None = None

    def check(self) -> None:
        if self.failure is None and time.monotonic() >= self.deadline:
            self.failure = TaskLeaseExpired("analysis lease renewal deadline elapsed")
        if self.failure is not None:
            raise self.failure

    @property
    def valid(self) -> bool:
        try:
            self.check()
            return True
        except TaskLeaseUnavailable:
            return False

    async def _renew(self, work: asyncio.Task) -> None:
        assert self.duration is not None
        interval = self.duration / 3
        delay = interval
        try:
            while True:
                await asyncio.sleep(delay)
                self.check()
                started = time.monotonic()
                try:
                    # A stuck provider cannot keep the coroutine waiting past
                    # the conservative last known lease deadline.
                    async with asyncio.timeout(min(interval, self.deadline - started)):
                        owned = await self.lock.refresh(self.key, self.token)
                except TaskLeaseUnavailable:
                    raise
                except Exception:
                    self.check()
                    _logger.warning("Analysis lease refresh failed; retrying within known lease")
                    delay = min(interval / 2, self.deadline - time.monotonic())
                    continue
                self.check()
                if not owned:
                    raise TaskLeaseLost("analysis lease owner token was rejected")
                # Count network latency against validity, including acquisition.
                self.deadline = started + self.duration
                delay = interval
        except TaskLeaseUnavailable as error:
            self.failure = error
            work.cancel()
        except Exception as error:
            self.failure = TaskLeaseUnavailable("analysis lease renewal stopped unexpectedly")
            self.failure.__cause__ = error
            work.cancel()

    async def run(self, operation: Callable[[], Awaitable[_Result]]) -> _Result:
        async def execute() -> _Result:
            binding = _current.set(self)
            try:
                self.check()
                result = await operation()
                self.check()
                return result
            finally:
                _current.reset(binding)

        work = asyncio.create_task(execute(), name="analysis-lease-work")
        renewal = (
            asyncio.create_task(self._renew(work), name="analysis-lease-renewal")
            if self.duration is not None else None
        )
        try:
            result = await work
            self.check()
            return result
        except asyncio.CancelledError:
            if self.failure is not None:
                raise self.failure from None
            raise
        finally:
            tasks = [task for task in (renewal, work) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Retrieve every exception and join cancellation before lock release.
            await asyncio.gather(*tasks, return_exceptions=True)
