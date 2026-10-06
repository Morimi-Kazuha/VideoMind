"""Content preprocessing reuse, independent of task results and providers."""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from dovideo.domain import VideoContext
from dovideo.domain.provenance import sha256_canonical
from .task_lease import TaskLeaseUnavailable, check_task_lease

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContentContextKey:
    fingerprint: str
    pipeline_contract: str

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{64}", self.fingerprint):
            raise ValueError("Content fingerprint must be SHA-256")
        if not self.pipeline_contract or len(self.pipeline_contract) > 1024:
            raise ValueError("Preprocessing contract is required and bounded")

    @property
    def digest(self):
        return sha256_canonical({"sha256": self.fingerprint, "pipeline": self.pipeline_contract})

    @property
    def media_identity(self):
        return f"sha256:{self.fingerprint}:pipeline:{self.digest}"

    @property
    def artifact_source(self):
        return f"content://{self.digest}"


class ContentArtifactPort(Protocol):
    async def load(self, key: ContentContextKey) -> VideoContext | None: ...
    async def publish(self, key: ContentContextKey, context: VideoContext) -> None: ...


class ContentBuildLockPort(Protocol):
    @property
    def lease_seconds(self) -> float: ...
    async def acquire(self, key: ContentContextKey) -> object | None: ...
    async def refresh(self, key: ContentContextKey, token: object) -> bool: ...
    async def release(self, key: ContentContextKey, token: object) -> None: ...


class ContentContextPreparation:
    """Double-check under a renewable content lease, then localize the artifact.

    Initial optimization outage permits a private build. Once contention is
    known, timeout/outage returns transport recovery, avoiding unbounded builds.
    No Agent state, execution record, chunk summary, or task identity is cached.
    """

    def __init__(self, artifacts: ContentArtifactPort, lock: ContentBuildLockPort,
                 *, wait_seconds: float = 3600, poll_seconds: float = 0.25):
        if not all(math.isfinite(x) and x > 0 for x in (wait_seconds, poll_seconds, lock.lease_seconds)):
            raise ValueError("Content coordination durations must be finite and positive")
        self.artifacts, self.lock = artifacts, lock
        self.wait_seconds, self.poll_seconds = wait_seconds, poll_seconds

    async def _lookup(self, key):
        try:
            return await self.artifacts.load(key)
        except Exception:
            logger.warning("Content artifact lookup unavailable; using a cache miss")
            return None

    async def prepare(self, key: ContentContextKey, source: str, goal: str,
                      build: Callable[[], Awaitable[VideoContext]],
                      *, cacheable: Callable[[], bool] = lambda: True) -> VideoContext:
        deadline = time.monotonic() + self.wait_seconds
        contended = False
        while True:
            check_task_lease()
            context = await self._lookup(key)
            if context is not None:
                return context.model_copy(update={"source": source, "user_goal": goal})
            started = time.monotonic()
            try:
                token = await self.lock.acquire(key)
            except Exception:
                if contended:
                    raise TaskLeaseUnavailable("Content coordination unavailable after observed contention") from None
                logger.warning("Content coordination unavailable; building private media context")
                context = await build()
                return context.model_copy(update={"source": source, "user_goal": goal})
            if token is not None:
                break
            contended = True
            if time.monotonic() >= deadline:
                raise TaskLeaseUnavailable("Content build wait expired; transport retry required")
            await asyncio.sleep(min(self.poll_seconds, max(0, deadline - time.monotonic())))

        work = None
        renewal = None
        lost = False
        expiry = started + self.lock.lease_seconds

        async def renew():
            nonlocal expiry, lost
            try:
                while True:
                    await asyncio.sleep(self.lock.lease_seconds / 3)
                    began = time.monotonic()
                    async with asyncio.timeout(max(0, expiry - began)):
                        owned = await self.lock.refresh(key, token)
                    if not owned or time.monotonic() >= expiry:
                        raise TaskLeaseUnavailable("Content build lease lost")
                    expiry = began + self.lock.lease_seconds
            except Exception:
                lost = True
                work.cancel()

        async def owned_build():
            if time.monotonic() >= expiry:
                raise TaskLeaseUnavailable("Content build lease expired before lookup")
            context = await self._lookup(key)  # Another owner may have published.
            if context is None:
                if time.monotonic() >= expiry:
                    raise TaskLeaseUnavailable("Content build lease expired before preprocessing")
                context = await build()
                check_task_lease()
                if lost or time.monotonic() >= expiry:
                    raise TaskLeaseUnavailable("Content build lease expired before publication")
                if cacheable():
                    artifact = context.model_copy(update={"source": key.artifact_source, "user_goal": ""})
                    try:
                        await self.artifacts.publish(key, artifact)
                    except Exception:
                        logger.warning("Content artifact publication unavailable; keeping private context")
            return context.model_copy(update={"source": source, "user_goal": goal})

        try:
            work = asyncio.create_task(owned_build(), name="content-context-build")
            renewal = asyncio.create_task(renew(), name="content-context-renewal")
            try:
                result = await work
            except asyncio.CancelledError:
                if lost:
                    raise TaskLeaseUnavailable("Content build ownership lost; transport retry required") from None
                raise
            if lost or time.monotonic() >= expiry:
                raise TaskLeaseUnavailable("Content build lease expired")
            return result
        finally:
            tasks = [task for task in (work, renewal) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await self.lock.release(key, token)
            except Exception:
                logger.warning("Content build lease release unavailable; bounded TTL will expire")
