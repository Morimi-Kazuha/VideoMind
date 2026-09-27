"""Bounded local/dev composition for the R1 HTTP surface.

This module is deliberately an adapter, not a replacement application core.
The public API submits ``AnalysisRequest`` objects through the existing
``TaskDispatchService`` and runs the existing ``TaskWorker`` and
``AgentLoopService``.  The stores below are explicitly process-local
development implementations; SQL/MySQL, Redis, MinIO, and Qdrant wiring is
reserved for the later production slices.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from dovideo.application import (
    AnalysisRequest,
    AnalysisStatusQuery,
    AgentEvaluationService,
    AgentLoopService,
    CompletedUploadMarker,
    DispatchDisposition,
    EvidenceVerificationService,
    InMemoryAgentBudgetUsage,
    MediaObservationBundle,
    MediaRecord,
    OcrBranchOutcome,
    TaskDispatchService,
    TaskEventDeliveryService,
    TaskKey,
    TaskLifecycle,
    TaskLifecycleEvent,
    VideoChunkingService,
    VideoContextBuilder,
    VideoEvidenceRetrievalService,
    UploadSession,
    UploadSessionState,
    UploadStatus,
    MAX_CHUNK_BYTES,
    MAX_TOTAL_CHUNKS,
    MEDIA_SESSION_TTL,
    mode_profile_for,
)
from dovideo.application.ports.checkpoint import (
    AgentCheckpointPort,
    AnalysisStatusCheckpointPort,
    ContextCheckpointPort,
)
from dovideo.application.ports.tasks import (
    AgentLoopEntryPort,
    TaskActiveMarkerPort,
    TaskCompletionPort,
    TaskEventPublisherPort,
    TaskLifecyclePort,
    TaskLockPort,
    TaskResultPort,
)
from dovideo.domain import (
    AgentPlan,
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    AnalysisSection,
    AgentState,
    CriticResult,
    TaskEvent,
    TaskStage,
    TaskStatus,
    TaskStatusState,
    VideoContext,
    VideoSegment,
)
from dovideo.infrastructure.providers import (
    LocalChunkSummaryAdapter,
    LocalTfidfEmbeddingAdapter,
)
from dovideo.presentation.composition import InMemoryVectorIndex, LocalRetrievalPlanner

from .schemas import AuthData, MediaSummary, UserInfo


class R1ServiceError(Exception):
    """Expected, safe-to-expose service failure at the HTTP boundary."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        code: int | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = status_code if code is None else code
        self.headers = dict(headers or {})


@dataclass(frozen=True, slots=True)
class _LocalUser:
    user_id: int
    username: str
    nickname: str
    password_hash: str
    role: str = "USER"

    def info(self) -> dict[str, Any]:
        return {
            "id": self.user_id,
            "username": self.username,
            "nickname": self.nickname,
            "avatar": None,
            "role": self.role,
        }


class LocalAuthService:
    """Secure-enough in-memory auth for local/dev and test use only."""

    _ITERATIONS = 210_000
    _TOKEN_TTL_SECONDS = 24 * 60 * 60
    _MAX_FAILURES = 8
    _FAILURE_WINDOW_SECONDS = 10 * 60

    def __init__(self) -> None:
        self._users: dict[str, _LocalUser] = {}
        self._tokens: dict[str, tuple[int, float]] = {}
        self._failures: dict[str, tuple[int, float]] = {}
        self._next_id = 1

    def register(self, username: str, password: str, nickname: str | None) -> AuthData:
        normalized = username.strip()
        if normalized in self._users:
            raise R1ServiceError("该账号已存在", status_code=409)
        if not nickname or not nickname.strip():
            normalized_nickname = f"用户{self._next_id}"
        else:
            normalized_nickname = nickname.strip()
        user = _LocalUser(
            user_id=self._next_id,
            username=normalized,
            nickname=normalized_nickname,
            password_hash=self._hash_password(password),
        )
        self._next_id += 1
        self._users[normalized] = user
        return AuthData(
            userInfo=UserInfo.model_validate(user.info()),
            token=None,
        )

    def login(self, username: str, password: str) -> AuthData:
        normalized = username.strip()
        now = time.monotonic()
        failure_count, expires = self._failures.get(normalized, (0, 0.0))
        if expires <= now:
            self._failures.pop(normalized, None)
            failure_count = 0
        if failure_count >= self._MAX_FAILURES:
            raise R1ServiceError("登录尝试过于频繁，请稍后再试", status_code=429)
        user = self._users.get(normalized)
        if user is None or not self._matches(password, user.password_hash):
            self._failures[normalized] = (
                failure_count + 1,
                now + self._FAILURE_WINDOW_SECONDS,
            )
            raise R1ServiceError("账号或密码错误", status_code=401)
        self._failures.pop(normalized, None)
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (user.user_id, now + self._TOKEN_TTL_SECONDS)
        return AuthData(
            userInfo=UserInfo.model_validate(user.info()),
            token=token,
        )

    def require(self, authorization: str | None) -> dict[str, Any]:
        if not authorization or not authorization.startswith("Bearer "):
            raise R1ServiceError("请先登录", status_code=401)
        token = authorization[7:].strip()
        if not token:
            raise R1ServiceError("无效的登录凭证", status_code=401)
        session = self._tokens.get(token)
        if session is None:
            raise R1ServiceError("登录状态已失效", status_code=401)
        user_id, expires = session
        if expires <= time.monotonic():
            self._tokens.pop(token, None)
            raise R1ServiceError("登录状态已失效", status_code=401)
        for user in self._users.values():
            if user.user_id == user_id:
                return user.info()
        self._tokens.pop(token, None)
        raise R1ServiceError("登录状态已失效", status_code=401)

    def logout(self, authorization: str | None) -> None:
        if authorization and authorization.startswith("Bearer "):
            self._tokens.pop(authorization[7:].strip(), None)

    @classmethod
    def _hash_password(cls, password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, cls._ITERATIONS, dklen=32
        )
        encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
        return f"pbkdf2${cls._ITERATIONS}${encode(salt)}${encode(digest)}"

    @classmethod
    def _matches(cls, password: str, stored: str) -> bool:
        try:
            prefix, iterations, encoded_salt, encoded_digest = stored.split("$", 3)
            if prefix != "pbkdf2":
                return False
            padding = "=" * (-len(encoded_salt) % 4)
            salt = base64.urlsafe_b64decode(encoded_salt + padding)
            padding = "=" * (-len(encoded_digest) % 4)
            expected = base64.urlsafe_b64decode(encoded_digest + padding)
            actual = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt, int(iterations), dklen=32
            )
            return hmac.compare_digest(actual, expected)
        except (TypeError, ValueError):
            return False


class _LifecycleStore(TaskLifecyclePort):
    def __init__(self) -> None:
        self.values: dict[TaskKey, TaskLifecycle] = {}

    async def load_lifecycle(self, key: TaskKey) -> TaskLifecycle | None:
        return self.values.get(key)

    async def save_lifecycle(self, lifecycle: TaskLifecycle) -> None:
        self.values[lifecycle.key] = lifecycle


class _ActiveStore(TaskActiveMarkerPort):
    def __init__(self) -> None:
        self.values: dict[TaskKey, float] = {}

    async def reserve(self, key: TaskKey, *, ttl_seconds: float) -> bool:
        if await self.is_active(key):
            return False
        self.values[key] = time.monotonic() + max(0.0, ttl_seconds)
        return True

    async def is_active(self, key: TaskKey) -> bool:
        expires = self.values.get(key)
        if expires is None:
            return False
        if expires <= time.monotonic():
            self.values.pop(key, None)
            return False
        return True

    async def refresh(self, key: TaskKey, *, ttl_seconds: float) -> None:
        if key in self.values:
            self.values[key] = time.monotonic() + max(0.0, ttl_seconds)

    async def release(self, key: TaskKey) -> None:
        self.values.pop(key, None)


class _CompletionStore(TaskCompletionPort):
    def __init__(self) -> None:
        self.values: dict[TaskKey, float] = {}

    async def is_completed(self, key: TaskKey) -> bool:
        expires = self.values.get(key)
        if expires is None:
            return False
        if expires <= time.monotonic():
            self.values.pop(key, None)
            return False
        return True

    async def mark_completed(self, key: TaskKey, *, ttl_seconds: float) -> None:
        self.values[key] = time.monotonic() + max(0.0, ttl_seconds)

    async def clear_completed(self, key: TaskKey) -> None:
        self.values.pop(key, None)


class _LockStore(TaskLockPort):
    def __init__(self) -> None:
        self.values: dict[TaskKey, asyncio.Lock] = {}

    async def acquire(self, key: TaskKey) -> object | None:
        lock = self.values.setdefault(key, asyncio.Lock())
        if lock.locked():
            return None
        await lock.acquire()
        return object()

    async def release(self, key: TaskKey, token: object) -> None:
        del token
        lock = self.values.get(key)
        if lock is not None and lock.locked():
            lock.release()


class _AllowQuota:
    async def try_acquire(self, request: AnalysisRequest) -> bool:
        del request
        return True


class _MemoryCheckpoint(
    AgentCheckpointPort,
    AnalysisStatusCheckpointPort,
    ContextCheckpointPort,
    TaskResultPort,
):
    def __init__(self, owner: "LocalR1Services") -> None:
        self.owner = owner

    async def load_context(self, media_id: int) -> VideoContext | None:
        return self.owner.contexts.get(media_id)

    async def save_context(self, media_id: int, context: VideoContext) -> None:
        self.owner.contexts[media_id] = context

    async def load_chunks(self, media_id: int):
        return self.owner.chunks.get(media_id)

    async def save_chunks(self, media_id: int, chunks) -> None:
        self.owner.chunks[media_id] = tuple(chunks)

    async def load_plan(self, key: TaskKey) -> AgentPlan | None:
        return self.owner.plans.get(key)

    async def save_plan(self, key: TaskKey, plan: AgentPlan) -> None:
        self.owner.plans[key] = plan

    async def load_critic_state(self, key: TaskKey) -> AgentState | None:
        return self.owner.critic_states.get(key)

    async def save_critic_state(self, key: TaskKey, state: AgentState) -> None:
        self.owner.critic_states[key] = state

    async def save_execution_state(self, key: TaskKey, state: AgentState) -> None:
        self.owner.drafts[key] = state

    async def load_result(self, key: TaskKey) -> AgentState | None:
        return self.owner.results.get(key)

    async def save_result(self, key: TaskKey, state: AgentState) -> None:
        self.owner.results[key] = state

    async def load_stage(self, key: TaskKey) -> TaskStage | None:
        lifecycle = await self.owner.lifecycle.load_lifecycle(key)
        return None if lifecycle is None else lifecycle.stage


@dataclass(slots=True)
class _StoredMedia:
    record: MediaRecord
    path: Path


class _LocalMediaStore:
    """Filesystem media adapter marked for local/dev use only."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.values: dict[int, _StoredMedia] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def ingest(self, user_id: int, filename: str, payload: bytes, content_type: str | None) -> MediaRecord:
        if not payload:
            raise R1ServiceError("视频文件不能为空", status_code=400)
        async with self._lock:
            media_id = self._next_id
            self._next_id += 1
            suffix = Path(filename).suffix.lower() or ".mp4"
            path = self.root / f"{uuid4().hex}{suffix}"
            await asyncio.to_thread(path.write_bytes, payload)
            record = MediaRecord(
                user_id=user_id,
                filename=filename,
                source=str(path),
                content_hash=hashlib.sha256(payload).hexdigest(),
                media_id=media_id,
                content_type=content_type,
            )
            self.values[media_id] = _StoredMedia(record, path)
            return record

    async def get(self, media_id: int) -> MediaRecord | None:
        stored = self.values.get(media_id)
        return None if stored is None else stored.record

    async def require_owned(self, media_id: int, user_id: int) -> MediaRecord:
        record = await self.get(media_id)
        if record is None:
            raise R1ServiceError("视频不存在", status_code=404)
        if record.user_id != user_id:
            raise R1ServiceError("无权访问该视频", status_code=403)
        return record

    async def list_owned(self, user_id: int) -> tuple[MediaRecord, ...]:
        return tuple(
            item.record
            for item in sorted(self.values.values(), key=lambda item: item.record.uploaded_at, reverse=True)
            if item.record.user_id == user_id
        )

    async def delete_owned(self, media_id: int, user_id: int) -> None:
        record = await self.require_owned(media_id, user_id)
        stored = self.values.pop(record.media_id or media_id)
        try:
            await asyncio.to_thread(stored.path.unlink, True)
        except OSError:
            pass

    def path_for(self, media_id: int) -> Path | None:
        stored = self.values.get(media_id)
        return None if stored is None else stored.path


@dataclass(slots=True)
class _UploadState:
    session: UploadSession
    chunks: dict[int, bytes] = field(default_factory=dict)
    completed_media_id: int | None = None


class _LocalChunkUploadStore:
    """Bounded in-memory resumable metadata/chunks for local/dev use only."""

    def __init__(self, media: _LocalMediaStore) -> None:
        self.media = media
        self.values: dict[str, _UploadState] = {}
        self.markers: dict[str, CompletedUploadMarker] = {}

    async def init(self, user_id: int, filename: str, total_chunks: int) -> str:
        if not 1 <= total_chunks <= MAX_TOTAL_CHUNKS:
            raise R1ServiceError(f"分片数量必须在 1 到 {MAX_TOTAL_CHUNKS} 之间")
        upload_id = str(uuid4())
        now = datetime.now(timezone.utc)
        session = UploadSession(
            upload_id=upload_id,
            filename=filename,
            total_chunks=total_chunks,
            user_id=user_id,
            created_at=now,
            expires_at=now + MEDIA_SESSION_TTL,
        )
        self.values[upload_id] = _UploadState(session)
        return upload_id

    def _owned(self, upload_id: str, user_id: int) -> _UploadState:
        state = self.values.get(upload_id)
        if state is None:
            raise R1ServiceError("上传会话不存在或已过期", status_code=404)
        if state.session.user_id != user_id:
            raise R1ServiceError("无权访问该上传会话", status_code=403)
        if state.session.is_expired(datetime.now(timezone.utc)):
            self.values.pop(upload_id, None)
            raise R1ServiceError("上传会话不存在或已过期", status_code=404)
        return state

    async def status(self, upload_id: str, user_id: int) -> UploadStatus:
        state = self._owned(upload_id, user_id)
        return UploadStatus(
            session=state.session,
            uploaded_chunks=tuple(sorted(state.chunks)),
            completed_media_id=state.completed_media_id,
        )

    async def put_chunk(
        self,
        upload_id: str,
        user_id: int,
        chunk_index: int,
        total_chunks: int,
        payload: bytes,
    ) -> UploadStatus:
        state = self._owned(upload_id, user_id)
        if total_chunks != state.session.total_chunks:
            raise R1ServiceError("分片总数与上传会话不一致")
        if not 0 <= chunk_index < total_chunks:
            raise R1ServiceError("分片序号无效")
        if len(payload) > MAX_CHUNK_BYTES:
            raise R1ServiceError("单个分片不能超过 5 MB", status_code=413)
        state.chunks[chunk_index] = payload
        return await self.status(upload_id, user_id)

    async def complete(self, upload_id: str, user_id: int) -> tuple[MediaRecord, UploadStatus]:
        state = self._owned(upload_id, user_id)
        if state.completed_media_id is not None:
            record = await self.media.require_owned(state.completed_media_id, user_id)
            return record, await self.status(upload_id, user_id)
        expected = set(range(state.session.total_chunks))
        if set(state.chunks) != expected:
            raise R1ServiceError("上传分片尚未全部完成", status_code=409)
        payload = b"".join(state.chunks[index] for index in range(state.session.total_chunks))
        record = await self.media.ingest(
            user_id,
            state.session.filename,
            payload,
            "video/" + Path(state.session.filename).suffix.lstrip(".").lower(),
        )
        state.completed_media_id = record.media_id
        state.session = UploadSession(
            upload_id=state.session.upload_id,
            filename=state.session.filename,
            total_chunks=state.session.total_chunks,
            user_id=state.session.user_id,
            created_at=state.session.created_at,
            expires_at=state.session.expires_at,
            state=UploadSessionState.COMPLETED,
        )
        self.markers[upload_id] = CompletedUploadMarker(
            upload_id=upload_id,
            user_id=user_id,
            media_id=record.media_id or 0,
            expires_at=datetime.now(timezone.utc) + MEDIA_SESSION_TTL,
            filename=record.filename,
            total_chunks=state.session.total_chunks,
            created_at=state.session.created_at,
        )
        return record, await self.status(upload_id, user_id)


@dataclass(slots=True)
class _TraceData:
    trace_id: str
    task_key: TaskKey
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    stage_durations: dict[str, int] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    values: dict[str, float] = field(default_factory=dict)
    estimated_cost: float = 0.0

    def increment(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def snapshot(self) -> dict[str, Any]:
        return {
            "traceId": self.trace_id,
            "taskId": self.task_key.media_id,
            "goalDigest": _goal_digest(self.task_key),
            "startedAt": self.started_at,
            "stageDurationMs": dict(self.stage_durations),
            "counters": dict(self.counters),
            "values": dict(self.values),
            "estimatedCost": self.estimated_cost,
        }


class _LocalTraceStore:
    """Baseline trace identity/stage/counter projection; no X2 event store."""

    def __init__(self) -> None:
        self.values: dict[TaskKey, _TraceData] = {}

    def start(self, key: TaskKey) -> str:
        trace = _TraceData(str(uuid4()), key)
        trace.increment("taskSubmissions")
        self.values[key] = trace
        return trace.trace_id

    def record(self, key: TaskKey, event: TaskEvent) -> None:
        trace = self.values.get(key)
        if trace is None:
            return
        if event.stage is not None:
            stage = event.stage.value
            trace.stage_durations.setdefault(stage, 0)
            trace.increment(f"{stage}Calls")
        if event.state is TaskStatusState.COMPLETED:
            trace.values["terminal"] = 1.0
        elif event.state is TaskStatusState.FAILED:
            trace.values["terminal"] = 0.0

    def latest(self, key: TaskKey) -> dict[str, Any]:
        trace = self.values.get(key)
        return {} if trace is None else trace.snapshot()

    def delete_media(self, media_id: int) -> None:
        for key in tuple(self.values):
            if key.media_id == media_id:
                self.values.pop(key, None)


class _EventPublisher(TaskEventPublisherPort):
    """Project events then fan them out to bounded in-process SSE queues."""

    def __init__(self, owner: "LocalR1Services") -> None:
        self.owner = owner

    async def publish(self, key: TaskKey, event: TaskEvent) -> None:
        lifecycle = await self.owner.lifecycle.load_lifecycle(key)
        attempt = 0 if lifecycle is None else lifecycle.attempt
        await self.owner.delivery.publish(
            key,
            event,
            attempt=attempt,
            retryable=event.stage is TaskStage.RETRYING,
        )
        envelope = TaskLifecycleEvent(
            key=key,
            event=event,
            attempt=attempt,
            retryable=event.stage is TaskStage.RETRYING,
        )
        self.owner.trace.record(key, event)
        queues = tuple(self.owner.subscribers.get(key, ()))
        for queue in queues:
            queue.put_nowait(envelope)


class _LocalContextService:
    def __init__(self, owner: "LocalR1Services") -> None:
        self.owner = owner

    async def select_relevant(self, context: VideoContext, media_id: int | None = None) -> VideoContext:
        retrieval = None if media_id is None else self.owner.retrieval.get(media_id)
        chunks = () if media_id is None else self.owner.chunks.get(media_id, ())
        if retrieval is not None and chunks:
            selected = await retrieval.retrieve(media_id, context.user_goal, chunks)
            if selected:
                return VideoContext(
                    source=context.source,
                    userGoal=context.user_goal,
                    segments=selected,
                )
        return context

    async def refine_for_critique(
        self,
        media_id: int | None,
        full_context: VideoContext,
        selected_context: VideoContext,
        critique: CriticResult | None,
    ) -> VideoContext:
        del media_id, full_context, critique
        return selected_context


class _LocalPlanner:
    async def plan(self, context: VideoContext, *, instruction: str = "") -> AgentPlan:
        del instruction
        return AgentPlan(
            understoodGoal=context.user_goal,
            tasks=(
                "定位与分析目标相关的时间片段",
                "绑定可核验的时间戳证据",
                "生成结构化结论和建议",
            ),
        )

    async def repair_plan(self, context: VideoContext, invalid_plan: AgentPlan, *, instruction: str = "") -> AgentPlan:
        del invalid_plan, instruction
        return await self.plan(context)

    async def replan(
        self,
        context: VideoContext,
        current_plan: AgentPlan,
        critique: CriticResult,
        *,
        instruction: str = "",
    ) -> AgentPlan:
        del current_plan, critique, instruction
        return await self.plan(context)


class _LocalExecutor:
    async def execute(
        self,
        context: VideoContext,
        plan: AgentPlan,
        previous_critique: CriticResult | None = None,
        *,
        instruction: str = "",
    ) -> AnalysisResult:
        del plan, previous_critique, instruction
        segment = next(
            (item for item in context.segments if item.transcript.strip()),
            context.segments[0],
        )
        claim = f"目标“{context.user_goal}”对应的证据位于视频时间片段"
        content = segment.transcript.strip() or "该时间片段已被本地媒体适配器保留"
        return AnalysisResult(
            title="VideoMind 本地分析结果",
            conclusions=(claim,),
            evidence=(
                AnalysisEvidence(
                    timestampMs=segment.start_ms,
                    source="ASR",
                    content=content,
                    claim=claim,
                ),
            ),
            suggestions=("结合时间戳回看该片段并核验结论。",),
            sections=(
                AnalysisSection(
                    key="evidence",
                    title="时间戳证据",
                    items=(f"{segment.start_ms}ms - {segment.end_ms}ms",),
                ),
            ),
        )


class _LocalCritic:
    async def critique(
        self,
        context: VideoContext,
        plan: AgentPlan,
        result: AnalysisResult,
        *,
        instruction: str = "",
    ) -> CriticResult:
        del context, plan, instruction
        return CriticResult(
            passed=bool(result is not None and result.evidence),
            feedback=(),
        )


def _goal_digest(key: TaskKey) -> str:
    from dovideo.application.analysis_task_keys import goal_digest

    return goal_digest(key.goal, key.mode)


class LocalR1Services:
    """Composition root used by ``create_app`` for local/dev operation."""

    def __init__(self, *, work_dir: Path | None = None) -> None:
        default_dir = Path.cwd() / "work" / "r1-media"
        configured = work_dir or Path(os.environ.get("DOVIDEO_R1_MEDIA_DIR", default_dir))
        self.auth = LocalAuthService()
        from dovideo.infrastructure.ai_interaction_limiter import (
            AiInteractionRateLimitSettings,
            InMemoryAiInteractionLimiter,
        )

        self.ai_interaction_limiter = InMemoryAiInteractionLimiter(
            settings=AiInteractionRateLimitSettings.from_environment()
        )
        self.media = _LocalMediaStore(configured)
        self.uploads = _LocalChunkUploadStore(self.media)
        self.contexts: dict[int, VideoContext] = {}
        self.observations: dict[int, tuple[Any, ...]] = {}
        self.chunks: dict[int, tuple[Any, ...]] = {}
        self.retrieval: dict[int, VideoEvidenceRetrievalService] = {}
        self.embeddings: dict[int, LocalTfidfEmbeddingAdapter] = {}
        self.pipeline_stats: dict[int, dict[str, int]] = {}
        self.playback_grants: dict[str, tuple[int, int, float]] = {}
        self.plans: dict[TaskKey, AgentPlan] = {}
        self.drafts: dict[TaskKey, AgentState] = {}
        self.critic_states: dict[TaskKey, AgentState] = {}
        self.results: dict[TaskKey, AgentState] = {}
        self.feedback: dict[int, list[Any]] = defaultdict(list)
        self.evaluator = AgentEvaluationService()
        self.lifecycle = _LifecycleStore()
        self.active = _ActiveStore()
        self.completion = _CompletionStore()
        self.lock = _LockStore()
        self.checkpoint = _MemoryCheckpoint(self)
        self.delivery = TaskEventDeliveryService()
        self.subscribers: dict[TaskKey, set[asyncio.Queue[TaskLifecycleEvent]]] = defaultdict(set)
        self.trace = _LocalTraceStore()
        self.publisher = _EventPublisher(self)
        self.context_service = _LocalContextService(self)
        self.agent_loop: AgentLoopEntryPort = AgentLoopService(
            self.context_service,
            _LocalPlanner(),
            _LocalExecutor(),
            self.checkpoint,
            self.publisher,
            InMemoryAgentBudgetUsage(),
            _LocalCritic(),
        )
        self.dispatcher = TaskDispatchService(
            self.active,
            completion=self.completion,
            quota=_AllowQuota(),
            lifecycle=self.lifecycle,
            events=self.publisher,
        )
        from dovideo.application.worker import TaskWorker

        self.worker = TaskWorker(
            self.lock,
            self.active,
            self.lifecycle,
            self.checkpoint,
            self.agent_loop,
            self.checkpoint,
            events=self.publisher,
            completion=self.completion,
        )
        self.status_query = AnalysisStatusQuery(self.checkpoint, self.active)
        self.tasks: set[asyncio.Task[Any]] = set()
        self.transcription_tasks: dict[int, asyncio.Task[Any]] = {}

    async def ingest(self, user_id: int, filename: str, payload: bytes, content_type: str | None) -> MediaRecord:
        record = await self.media.ingest(user_id, filename, payload, content_type)
        media_id = record.media_id
        assert media_id is not None
        self._build_observations(record)
        self.contexts[media_id] = self._build_context(record, "")
        return record

    def _build_observations(self, record: MediaRecord) -> None:
        filename = record.filename
        from dovideo.application.value_objects import TranscriptSpan

        self.observations[record.media_id or 0] = (
            TranscriptSpan(
                0,
                60_000,
                f"Opening temporal region for {filename}; local R1 development transcript.",
            ),
            TranscriptSpan(
                300_000,
                360_000,
                f"Later temporal region for {filename}; local R1 development transcript.",
            ),
        )

    def _build_context(self, record: MediaRecord, goal: str) -> VideoContext:
        from dovideo.application.value_objects import AsrBranchOutcome

        media_id = record.media_id or 0
        observations = MediaObservationBundle(
            asr=AsrBranchOutcome(
                observations=self.observations.get(media_id, ()),
                attempted=len(self.observations.get(media_id, ())),
            ),
            ocr=OcrBranchOutcome(),
        )
        return VideoContextBuilder().build(
            record.source,
            goal,
            observations,
            media_content_identity=(
                record.content_hash or f"media-id:{media_id}"
            ),
        )

    async def _prepare_pipeline(self, media_id: int, goal: str) -> VideoContext:
        record = await self.media.get(media_id)
        if record is None:
            raise R1ServiceError("视频不存在", status_code=404)
        context = self._build_context(record, goal)
        self.contexts[media_id] = context
        if media_id not in self.retrieval:
            embedding = LocalTfidfEmbeddingAdapter(max_features=256)
            source_documents = tuple(segment.transcript for segment in context.segments)
            embedding.fit(source_documents)
            chunker = VideoChunkingService(LocalChunkSummaryAdapter(), embedding)
            chunks = await chunker.build(context.segments)
            vector = InMemoryVectorIndex()
            retrieval = VideoEvidenceRetrievalService(
                LocalRetrievalPlanner(), embedding, vector
            )
            await retrieval.index(media_id, chunks)
            self.embeddings[media_id] = embedding
            self.chunks[media_id] = chunks
            self.retrieval[media_id] = retrieval
            self.pipeline_stats[media_id] = {
                "temporalWindows": len(context.segments),
                "chunkCount": len(chunks),
                "embeddingVectors": sum(1 for chunk in chunks if chunk.embedding),
                "embeddingDimension": embedding.dimension,
            }
        return context

    @staticmethod
    def _profile(mode: AnalysisMode):
        return mode_profile_for(mode)

    async def submit_analysis(
        self,
        media_id: int,
        user_id: int,
        goal: str,
        mode: AnalysisMode,
    ) -> DispatchDisposition:
        record = await self.media.require_owned(media_id, user_id)
        await self._prepare_pipeline(media_id, goal)
        request = AnalysisRequest(record.to_ref(), goal, mode)
        if await self.checkpoint.load_result(request.task_key) is not None:
            return DispatchDisposition.DUPLICATE
        disposition = await self.dispatcher.dispatch(request)
        if disposition is DispatchDisposition.ACCEPTED:
            self.trace.start(request.task_key)
            task = asyncio.create_task(
                self._run_analysis(request),
                name=f"dovideo-r1-analysis-{media_id}",
            )
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return disposition

    async def _run_analysis(self, request: AnalysisRequest) -> None:
        await self.worker.handle(
            request,
            profile=self._profile(request.mode),
        )

    async def status(self, media_id: int, goal: str, mode: AnalysisMode) -> TaskStatus:
        return await self.status_query.current(media_id, goal, mode)

    def key(self, media_id: int, goal: str, mode: AnalysisMode) -> TaskKey:
        return TaskKey(media_id, goal, mode)

    async def plan(self, media_id: int, goal: str, mode: AnalysisMode) -> AgentPlan | None:
        return await self.checkpoint.load_plan(self.key(media_id, goal, mode))

    async def evidence_search(self, media_id: int, query: str) -> tuple[Any, ...]:
        retrieval = self.retrieval.get(media_id)
        if retrieval is None:
            return ()
        return await retrieval.search(media_id, query, self.chunks.get(media_id, ()))

    async def playback_url(self, media_id: int, user_id: int) -> str:
        await self.media.require_owned(media_id, user_id)
        token = secrets.token_urlsafe(24)
        self.playback_grants[token] = (media_id, user_id, time.monotonic() + 15 * 60)
        return f"/media/playback-file/{media_id}?access_token={token}"

    async def playback_file(self, media_id: int, token: str) -> tuple[Path, str | None]:
        grant = self.playback_grants.get(token)
        if grant is None or grant[0] != media_id or grant[2] <= time.monotonic():
            self.playback_grants.pop(token, None)
            raise R1ServiceError("播放地址已失效", status_code=401)
        record = await self.media.require_owned(media_id, grant[1])
        path = self.media.path_for(media_id)
        if path is None or not path.is_file():
            raise R1ServiceError("视频文件不存在", status_code=404)
        return path, record.content_type

    async def download_path(self, media_id: int, user_id: int) -> tuple[Path, str]:
        record = await self.media.require_owned(media_id, user_id)
        path = self.media.path_for(media_id)
        if path is None or not path.is_file():
            raise R1ServiceError("视频文件不存在", status_code=404)
        filename = Path(record.filename).stem + ".mp3"
        return path, filename

    async def follow_up(self, media_id: int, question: str, goal: str | None, mode: AnalysisMode) -> str:
        del mode
        context = self.contexts.get(media_id)
        if context is None:
            raise R1ServiceError("视频上下文尚未准备完成", status_code=409)
        selected_goal = goal or context.user_goal or "当前视频"
        segment = context.segments[0]
        return (
            f"针对“{question}”，基于目标“{selected_goal}”可先回看 "
            f"{segment.start_ms}ms 的时间戳证据：{segment.transcript}"
        )

    def route(self, goal: str) -> tuple[AnalysisMode, str]:
        """Legacy deterministic route retained for local development only."""

        normalized = goal.casefold()
        if any(word in normalized for word in ("学习", "教程", "笔记", "知识点", "learn")):
            return AnalysisMode.LEARNING, "目标包含学习/知识整理意图"
        if any(word in normalized for word in ("审查", "审核", "风险", "review", "漏洞")):
            return AnalysisMode.REVIEW, "目标包含审查或风险识别意图"
        if any(word in normalized for word in ("创作", "脚本", "标题", "creation", "script")):
            return AnalysisMode.CREATION, "目标包含创作产物意图"
        return AnalysisMode.GENERAL, "按通用模式处理分析目标"

    async def save_feedback(self, feedback: Any) -> None:
        self.feedback[feedback.media_id].append(feedback)

    def feedback_for(self, media_id: int) -> tuple[Any, ...]:
        return tuple(self.feedback.get(media_id, ()))

    async def evaluation(self, media_id: int, goal: str, mode: AnalysisMode) -> dict[str, Any]:
        key = self.key(media_id, goal, mode)
        state = self.results.get(key) or self.critic_states.get(key)
        context = self.contexts.get(media_id)
        feedback = tuple(
            item
            for item in self.feedback_for(media_id)
            if (item.goal == goal or item.corrected_goal == goal)
            and AnalysisMode.from_nullable(item.mode) is mode
        )
        return self.evaluator.evaluate(context, state, feedback)

    def trace_snapshot(self, media_id: int, goal: str, mode: AnalysisMode) -> dict[str, Any]:
        return self.trace.latest(self.key(media_id, goal, mode))

    async def start_transcription(self, media_id: int, user_id: int) -> None:
        await self.media.require_owned(media_id, user_id)
        existing = self.transcription_tasks.get(media_id)
        if existing is not None and not existing.done():
            raise R1ServiceError("文字提取任务正在处理中", status_code=409)
        key = TaskKey(media_id, "__transcription__", AnalysisMode.GENERAL)
        await self.active.reserve(key, ttl_seconds=60 * 60)
        queued = TaskLifecycle.new(key).queued("文字提取任务已排队")
        await self.lifecycle.save_lifecycle(queued)
        await self.publisher.publish(key, queued.event)
        task = asyncio.create_task(self._run_transcription(key), name=f"dovideo-r1-transcription-{media_id}")
        self.transcription_tasks[media_id] = task

    async def _run_transcription(self, key: TaskKey) -> None:
        try:
            lifecycle = await self.lifecycle.load_lifecycle(key)
            if lifecycle is None:
                return
            started = lifecycle.begin_attempt(stage=TaskStage.TRANSCRIPTION)
            await self.lifecycle.save_lifecycle(started)
            await self.publisher.publish(
                key,
                TaskEvent.of(TaskStatus.of(TaskStatusState.PROCESSING, "正在提取视频文字"), TaskStage.TRANSCRIPTION),
            )
            context = self.contexts.get(key.media_id)
            if context is None:
                raise R1ServiceError("视频上下文尚未准备完成", status_code=409)
            transcript = context.transcript_text()
            completed = started.complete(transcript)
            await self.lifecycle.save_lifecycle(completed)
            await self.publisher.publish(key, completed.event)
        except R1ServiceError as error:
            lifecycle = await self.lifecycle.load_lifecycle(key)
            if lifecycle is not None and not lifecycle.terminal:
                failed = lifecycle.fail(error.message, stage=TaskStage.FAILED)
                await self.lifecycle.save_lifecycle(failed)
                await self.publisher.publish(key, failed.event)
        finally:
            await self.active.release(key)

    async def transcription_status(self, media_id: int, user_id: int) -> TaskStatus:
        await self.media.require_owned(media_id, user_id)
        return self.delivery.current(TaskKey(media_id, "__transcription__", AnalysisMode.GENERAL))

    async def subscribe(self, key: TaskKey) -> AsyncIterator[TaskLifecycleEvent | None]:
        queue: asyncio.Queue[TaskLifecycleEvent] = asyncio.Queue()
        self.subscribers[key].add(queue)
        snapshot = self.delivery.snapshot(key)
        initial = TaskLifecycleEvent(
            key=key,
            event=TaskEvent.of(snapshot.status, snapshot.stage),
            attempt=snapshot.attempt,
            retryable=snapshot.retryable,
        )
        try:
            yield initial
            if initial.terminal:
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield None
                    continue
                yield event
                if event.terminal:
                    return
        finally:
            subscribers = self.subscribers.get(key)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self.subscribers.pop(key, None)

    async def delete_media(self, media_id: int, user_id: int) -> None:
        await self.media.delete_owned(media_id, user_id)
        self.contexts.pop(media_id, None)
        self.observations.pop(media_id, None)
        self.chunks.pop(media_id, None)
        self.retrieval.pop(media_id, None)
        self.embeddings.pop(media_id, None)
        self.pipeline_stats.pop(media_id, None)
        self.trace.delete_media(media_id)
        for token, grant in tuple(self.playback_grants.items()):
            if grant[0] == media_id:
                self.playback_grants.pop(token, None)
        for key in tuple(self.results):
            if key.media_id == media_id:
                self.results.pop(key, None)
                self.plans.pop(key, None)
                self.drafts.pop(key, None)
                self.critic_states.pop(key, None)

    async def shutdown(self) -> None:
        pending = tuple(task for task in self.tasks if not task.done())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        transcription = tuple(task for task in self.transcription_tasks.values() if not task.done())
        for task in transcription:
            task.cancel()
        if transcription:
            await asyncio.gather(*transcription, return_exceptions=True)


def media_summary(record: MediaRecord) -> MediaSummary:
    return MediaSummary(
        id=record.media_id or 0,
        filename=record.filename,
        status=record.status.value,
        coverUrl=None,
        uploadTime=record.uploaded_at.isoformat(),
    )


__all__ = ["LocalAuthService", "LocalR1Services", "R1ServiceError", "media_summary"]
