"""Explicit R2-backed FastAPI storage composition.

R2 owns durable identity/media/checkpoints and provider-backed objects. The
R1 local task transport remains a separate explicit profile; this module never
constructs the local filesystem or in-memory persistence adapters.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import secrets
import time
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from uuid import uuid4

from dovideo.application import (
    AgentCheckpointService,
    DispatchDisposition,
    MediaRecord,
    TaskKey,
    UploadStatus,
    filename_suffix,
    normalize_video_filename,
)
from dovideo.application.errors import MediaUnauthorized
from dovideo.domain import AgentPlan, AnalysisMode, TaskStatus, TaskStatusState
from dovideo.application import AnalysisRequest
from dovideo.infrastructure import (
    AiInteractionLimiter,
    R2Infrastructure,
    RedisAgentTelemetry,
    RedisAuthService,
    create_r2_infrastructure,
)
from dovideo.infrastructure.media.uploads import ChunkUploadService

from .runtime import R1ServiceError


class ProductionMediaStore:
    """MediaRecordPort plus the ownership operations used by the API."""

    def __init__(self, infrastructure: R2Infrastructure) -> None:
        self.infrastructure = infrastructure
        self.repository = infrastructure.media_repository

    async def get(self, media_id: int) -> MediaRecord | None:
        return await self.repository.get(media_id)

    async def list_owned(self, user_id: int) -> tuple[MediaRecord, ...]:
        return await self.repository.list_owned(user_id)

    async def save(self, record: MediaRecord) -> MediaRecord:
        return await self.repository.save(record)

    async def delete(self, media_id: int) -> None:
        await self.repository.delete(media_id)

    async def require_owned(self, media_id: int, user_id: int) -> MediaRecord:
        record = await self.get(media_id)
        if record is None:
            raise R1ServiceError("视频不存在", status_code=404)
        if record.user_id != user_id:
            raise R1ServiceError("无权访问该视频", status_code=403)
        return record

    async def ingest(
        self,
        user_id: int,
        filename: str,
        payload: bytes,
        content_type: str | None,
    ) -> MediaRecord:
        normalized = normalize_video_filename(filename)
        if not payload:
            raise R1ServiceError("上传文件不能为空", status_code=400)

        async def source() -> AsyncIterable[bytes]:
            for offset in range(0, len(payload), 1024 * 1024):
                yield payload[offset : offset + 1024 * 1024]

        object_name = f"media/{uuid4().hex}{filename_suffix(normalized)}"
        try:
            object_source = await self.infrastructure.object_storage.put_object(
                source(),
                object_name=object_name,
                content_type=content_type or mimetypes.guess_type(normalized)[0],
            )
            record = MediaRecord(
                user_id=user_id,
                filename=normalized,
                source=object_source,
                content_hash=hashlib.md5(payload).hexdigest(),
                content_type=content_type,
            )
            try:
                return await self.repository.save(record)
            except BaseException as exc:
                await self.infrastructure.object_storage.delete_object(object_source)
                raise exc
        except R1ServiceError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise R1ServiceError("对象存储暂不可用", status_code=503) from exc

    async def delete_owned(self, media_id: int, user_id: int) -> None:
        record = await self.require_owned(media_id, user_id)
        try:
            await self.infrastructure.object_storage.delete_object(record.source)
            await self.repository.delete(media_id)
            await self.infrastructure.vector_index.delete_media(media_id)
            self.infrastructure.checkpoint_store.delete_media(media_id)
            self.infrastructure.checkpoint_cache.delete_media(media_id)
        except R1ServiceError:
            raise
        except Exception as exc:
            raise R1ServiceError("视频删除失败", status_code=503) from exc


class ProductionUploadFacade:
    """Keep the R1 API upload methods over the existing upload use case."""

    def __init__(self, infrastructure: R2Infrastructure) -> None:
        self.service = ChunkUploadService(
            infrastructure.chunk_store,
            infrastructure.object_storage,
            infrastructure.media_repository,
            infrastructure.upload_sessions,
            infrastructure.merge_lock,
            workspace_parent=infrastructure.settings.media_workspace,
        )

    async def init(self, user_id: int, filename: str, total_chunks: int) -> str:
        return await self.service.initialize(filename, total_chunks, user_id)

    async def status(self, upload_id: str, user_id: int) -> UploadStatus:
        return await self.service.status(upload_id, user_id)

    async def put_chunk(
        self,
        upload_id: str,
        user_id: int,
        chunk_index: int,
        total_chunks: int,
        payload: bytes,
    ) -> UploadStatus:
        await self.service.upload_chunk(
            upload_id,
            chunk_index,
            total_chunks,
            payload,
            user_id,
        )
        return await self.service.status(upload_id, user_id)

    async def complete(self, upload_id: str, user_id: int) -> tuple[MediaRecord, UploadStatus]:
        record = await self.service.complete(upload_id, user_id)
        return record, await self.service.status(upload_id, user_id)


class ProductionR2Services:
    """Concrete production-profile service surface for the unchanged API."""

    def __init__(self, infrastructure: R2Infrastructure) -> None:
        self.infrastructure = infrastructure
        self.auth = RedisAuthService(infrastructure.user_store, infrastructure.redis_client)
        self.ai_interaction_limiter = AiInteractionLimiter(
            infrastructure.redis_client,
            settings=infrastructure.settings.ai_interaction_rate_limit,
        )
        self.media = ProductionMediaStore(infrastructure)
        self.uploads = ProductionUploadFacade(infrastructure)
        self.checkpoint = AgentCheckpointService(infrastructure.checkpoint_repository)
        self.trace = RedisAgentTelemetry(infrastructure.redis_client)
        self.playback_grants: dict[str, tuple[int, int, float]] = {}

    async def startup(self) -> None:
        await self.infrastructure.initialize()

    async def shutdown(self) -> None:
        for path, (_media_id, _user_id, _expires) in tuple(self._playback_files.items()):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self.infrastructure.close()

    async def ingest(self, user_id: int, filename: str, payload: bytes, content_type: str | None) -> MediaRecord:
        return await self.media.ingest(user_id, filename, payload, content_type)

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
        path = await self._download_object(record)
        return path, record.content_type

    async def download_path(self, media_id: int, user_id: int) -> tuple[Path, str]:
        record = await self.media.require_owned(media_id, user_id)
        return await self._download_object(record), Path(record.filename).stem + ".mp3"

    async def _download_object(self, record: MediaRecord) -> Path:
        name = record.source.split("/", 3)[-1]
        target_dir = self.infrastructure.settings.media_workspace
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"playback-{record.media_id}-{secrets.token_hex(8)}{filename_suffix(record.filename)}"

        def download() -> None:
            response = self.infrastructure.minio_client.get_object(
                self.infrastructure.settings.minio_bucket,
                name,
            )
            try:
                with target.open("wb") as output:
                    while True:
                        piece = response.read(1024 * 1024)
                        if not piece:
                            break
                        output.write(piece)
            finally:
                response.close()
                response.release_conn()

        try:
            await asyncio.to_thread(download)
        except Exception as exc:
            raise R1ServiceError("视频对象不可读", status_code=503) from exc
        self._playback_files[target] = (record.media_id or 0, record.user_id, time.monotonic() + 15 * 60)
        return target

    @property
    def _playback_files(self) -> dict[Path, tuple[int, int, float]]:
        values = getattr(self, "__playback_files", None)
        if values is None:
            values = {}
            setattr(self, "__playback_files", values)
        return values

    async def delete_media(self, media_id: int, user_id: int) -> None:
        await self.media.delete_owned(media_id, user_id)

    def route(self, goal: str) -> tuple[AnalysisMode, str]:
        """Legacy keyword route for R2-compatible dev/test compositions.

        Production R4 overrides this method with the bounded model router.
        """

        normalized = goal.casefold()
        if any(word in normalized for word in ("学习", "教程", "笔记", "知识点", "learn")):
            return AnalysisMode.LEARNING, "目标包含学习/知识整理意图"
        if any(word in normalized for word in ("审查", "审核", "风险", "review", "漏洞")):
            return AnalysisMode.REVIEW, "目标包含审查或风险识别意图"
        if any(word in normalized for word in ("创作", "脚本", "标题", "creation", "script")):
            return AnalysisMode.CREATION, "目标包含创作产物意图"
        return AnalysisMode.GENERAL, "按通用模式处理分析目标"

    def key(self, media_id: int, goal: str, mode: AnalysisMode) -> TaskKey:
        return TaskKey(media_id, goal, mode)

    async def submit_analysis(self, media_id: int, user_id: int, goal: str, mode: AnalysisMode) -> DispatchDisposition:
        await self.media.require_owned(media_id, user_id)
        raise R1ServiceError(
            "R2 数据基础设施已就绪；Agent worker transport 仍由后续 R3 提供",
            status_code=501,
        )

    async def follow_up(self, media_id: int, question: str, goal: str | None, mode: AnalysisMode) -> str:
        del question, goal, mode
        await self.media.get(media_id)
        raise R1ServiceError("production Agent worker 尚未启用", status_code=501)

    async def evidence_search(self, media_id: int, query: str) -> tuple[object, ...]:
        del query
        if await self.media.get(media_id) is None:
            raise R1ServiceError("视频不存在", status_code=404)
        return ()

    async def save_feedback(self, feedback: object) -> None:
        await self.checkpoint.save_feedback(feedback)

    async def revise_analysis(self, feedback, user_id: int) -> DispatchDisposition:
        if not hasattr(self, "dispatcher"):
            raise R1ServiceError("production Agent worker 尚未启用", status_code=501)
        record = await self.media.require_owned(feedback.media_id, user_id)
        goal = feedback.corrected_goal or feedback.goal
        plan = AgentPlan(understoodGoal=goal, tasks=feedback.corrected_tasks) if feedback.corrected_tasks else None
        request = AnalysisRequest(record.to_ref(), goal, feedback.mode, request_id=f"revision:{uuid4().hex}")
        return await self.dispatcher.dispatch(request, revision_plan=plan, revision_checkpoint=self.checkpoint)

    def feedback_for(self, media_id: int) -> tuple[object, ...]:
        del media_id
        return ()

    async def plan(self, media_id: int, goal: str, mode: AnalysisMode) -> object | None:
        return await self.checkpoint.load_plan(TaskKey(media_id, goal, mode))

    async def status(self, media_id: int, goal: str, mode: AnalysisMode) -> TaskStatus:
        key = TaskKey(media_id, goal, mode)
        result = await self.checkpoint.load_result(key)
        if result is not None:
            return TaskStatus.completed(result)
        return TaskStatus(state=TaskStatusState.NOT_STARTED, result=None, message="任务尚未提交")

    async def evaluation(self, media_id: int, goal: str, mode: AnalysisMode) -> dict[str, object]:
        del media_id, goal, mode
        return {"available": False, "reason": "production Agent worker 尚未启用"}

    def trace_snapshot(self, media_id: int, goal: str, mode: AnalysisMode) -> dict[str, object]:
        return self.trace.latest(TaskKey(media_id, goal, mode))

    async def start_transcription(self, media_id: int, user_id: int) -> None:
        await self.media.require_owned(media_id, user_id)
        raise R1ServiceError("production transcription worker 尚未启用", status_code=501)

    async def transcription_status(self, media_id: int, user_id: int) -> TaskStatus:
        await self.media.require_owned(media_id, user_id)
        return TaskStatus(state=TaskStatusState.NOT_STARTED, result=None, message="任务尚未提交")

    async def subscribe(self, key: TaskKey) -> AsyncIterator[object | None]:
        del key
        yield None


def create_production_services() -> ProductionR2Services:
    return ProductionR2Services(create_r2_infrastructure())


__all__ = ["ProductionR2Services", "create_production_services"]
