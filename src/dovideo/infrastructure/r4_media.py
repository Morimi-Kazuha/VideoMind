"""Extracted production media boundary; legacy exports remain in r4_runtime."""
from __future__ import annotations
import asyncio
import hashlib
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4
from dovideo.application.value_objects import (AnalysisRequest, TranscriptSpan)
from dovideo.application.context import (VideoContextBuilder)
from dovideo.application.ports.checkpoint import ContextCheckpointPort
from dovideo.application.content_context import ContentContextKey, ContentContextPreparation
from dovideo.application.ports.tasks import TaskEventPublisherPort
from dovideo.domain import TaskEvent, TaskStage, TaskStatus, TaskStatusState, VideoContext
from dovideo.infrastructure.media import AudioSegmenter, AsyncSubprocessRunner, FfprobeDurationAdapter, FFmpegKeyframeExtractor, LocalWhisperTranscriptionAdapter, MediaBranchOrchestrator, OcrBatchService, PillowDifferenceHash, SegmentedTranscriptionService, TesseractOcrAdapter
from dovideo.presentation.composition import AnalysisSettings, _prepend_tool_directory
from .r2_config import R2Infrastructure
from .content_context import RedisContentBuildLock
from .persistence.content_context import SqlAlchemyContentArtifacts
from .r4_telemetry import R4AgentTelemetry
from .media.response_files import joined_thread

_CURRENT_R4_REQUEST: ContextVar[AnalysisRequest | None] = ContextVar(
    "dovideo_r4_current_request",
    default=None,
)


class _WhisperSegmentTranscriber:
    """Adapt the existing local Whisper adapter to segmented ASR."""

    def __init__(self, adapter: LocalWhisperTranscriptionAdapter) -> None:
        self.adapter = adapter

    async def transcribe_segment(
        self,
        audio_path: Path,
        *,
        trace_id: str | None = None,
    ) -> tuple[TranscriptSpan, ...]:
        return await self.adapter.transcribe_path(audio_path, trace_id=trace_id)


class R4MediaPipeline:
    """Reuse trusted MD5 preprocessing; download MinIO bytes only on a miss."""

    def __init__(
        self,
        infrastructure: R2Infrastructure,
        settings: AnalysisSettings,
        telemetry: R4AgentTelemetry,
        events: TaskEventPublisherPort,
    ) -> None:
        self.infrastructure = infrastructure
        self.settings = settings
        self.telemetry = telemetry
        self.events = events
        self._whisper: LocalWhisperTranscriptionAdapter | None = None
        self.context_preparation = ContentContextPreparation(
            SqlAlchemyContentArtifacts(infrastructure.engine, infrastructure.redis_client),
            RedisContentBuildLock(infrastructure.redis_client),
            wait_seconds=settings.media_timeout_seconds + 120,
        )

    async def build_context(self, request: AnalysisRequest) -> VideoContext:
        # Submission is owner-authorized by require_owned. Re-read the durable
        # record here: transport content_hash/filename must not select a cache.
        record = await self.infrastructure.media_repository.get(request.media.media_id)
        if record is None or record.source != request.media.source:
            raise ValueError("R4 media record is missing or its source has changed")
        request = replace(request, media=record.to_ref())
        self._object_coordinates(request)  # Validate even when no download occurs.
        local_path = None
        try:
            try:
                key = ContentContextKey(record.content_hash)
            except ValueError:
                # Historical missing/invalid MD5: hash during the necessary
                # download, then join the same MD5 cache and content lock.
                local_path, fingerprint = await self._download(request)
                key = ContentContextKey(fingerprint)
            complete = [False]

            async def build():
                nonlocal local_path
                if local_path is None:
                    local_path, fingerprint = await self._download(request)
                    if fingerprint != key.fingerprint:
                        raise ValueError("R4 downloaded media does not match its persisted MD5")
                return await self._build_local(request, local_path, key, complete)

            return await self.context_preparation.prepare(
                key, request.media.source, request.goal, build,
                cacheable=lambda: complete[0],
            )
        finally:
            if local_path is not None:
                try:
                    await asyncio.to_thread(local_path.unlink, True)
                except OSError:
                    pass

    async def _build_local(self, request, local_path, key, complete):
        _prepend_tool_directory(self.settings.ffmpeg_executable)
        runner = AsyncSubprocessRunner(
            default_timeout=self.settings.process_timeout_seconds
        )
        duration = await FfprobeDurationAdapter(
            runner,
            executable=self.settings.ffprobe_executable,
        ).probe(local_path)
        self.telemetry.observe("mediaDurationSeconds", duration.seconds)

        await self._publish(
            request,
            TaskStage.VIDEO_CONTEXT,
            "正在对 MinIO 视频执行 FFmpeg 媒体处理",
        )
        await self._publish(request, TaskStage.ASR, "正在运行本地 Whisper ASR")
        await self._publish(request, TaskStage.TRANSCRIPTION, "正在生成时间戳转录")
        observations = await MediaBranchOrchestrator(
            AudioSegmenter(
                runner,
                executable=self.settings.ffmpeg_executable,
                timeout=self.settings.process_timeout_seconds,
            ),
            SegmentedTranscriptionService(
                _WhisperSegmentTranscriber(self._whisper_adapter())
            ),
            FFmpegKeyframeExtractor(
                runner,
                executable=self.settings.ffmpeg_executable,
                timeout=self.settings.process_timeout_seconds,
            ),
            OcrBatchService(
                TesseractOcrAdapter(
                    runner,
                    executable=self.settings.tesseract_executable,
                ),
                PillowDifferenceHash(),
                telemetry=self.telemetry,
            ),
            telemetry=self.telemetry,
            total_timeout_seconds=self.settings.media_timeout_seconds,
        ).collect(
            str(local_path),
            media_identity=(
                key.media_identity
            ),
            parent=self.infrastructure.settings.media_workspace / "r4-media-workspaces",
        )
        self.telemetry.observe("asrSpanCount", len(observations.asr.observations))
        self.telemetry.observe("ocrObservationCount", len(observations.ocr.observations))
        if not observations.asr.observations and not observations.ocr.observations:
            raise ValueError("R4 media produced no usable ASR or OCR observations")

        context = VideoContextBuilder().build(
            key.artifact_source,
            "",
            observations,
            media_content_identity=(
                key.media_identity
            ),
        )
        if not context.segments:
            raise ValueError("R4 media did not produce temporal observations")
        self.telemetry.observe("videoContextWindowCount", len(context.segments))
        from dovideo.application.value_objects import BranchStatus
        complete[0] = (
            observations.asr.status is BranchStatus.SUCCESS
            and observations.ocr.status is BranchStatus.SUCCESS
        )
        await self._publish(
            request,
            TaskStage.CONTEXT_COMPLETED,
            "VideoContext 时间窗口已从真实媒体观察构建",
        )
        return context

    def _whisper_adapter(self) -> LocalWhisperTranscriptionAdapter:
        if self._whisper is None:
            self._whisper = LocalWhisperTranscriptionAdapter.from_openai_whisper(
                self.settings.whisper_model,
                model_root=self.settings.whisper_model_root,
                device=self.settings.whisper_device,
                language=self.settings.whisper_language,
            )
        return self._whisper

    def _object_coordinates(self, request: AnalysisRequest) -> tuple[str, str]:
        source = request.media.source
        parsed = urlsplit(source)
        bucket = parsed.netloc
        object_name = parsed.path.lstrip("/")
        if parsed.scheme != "minio" or bucket != self.infrastructure.settings.minio_bucket:
            raise ValueError("R4 worker requires a media object owned by MinIO")
        if not object_name or ".." in object_name or "\\" in object_name:
            raise ValueError("R4 media object name is invalid")
        return bucket, object_name

    async def _download(self, request: AnalysisRequest) -> tuple[Path, str]:
        bucket, object_name = self._object_coordinates(request)
        target_dir = self.infrastructure.settings.media_workspace / "r4-inputs"
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        suffix = Path(request.media.filename or "media.mp4").suffix.lower() or ".mp4"
        target = target_dir / f"media-{request.media.media_id}-{uuid4().hex}{suffix}"

        def read_object() -> str:
            digest = hashlib.md5()
            response = self.infrastructure.minio_client.get_object(bucket, object_name)
            try:
                with target.open("wb") as output:
                    while True:
                        piece = response.read(1024 * 1024)
                        if not piece:
                            break
                        output.write(piece)
                        digest.update(piece)
            finally:
                close = getattr(response, "close", None)
                release = getattr(response, "release_conn", None)
                if callable(close):
                    close()
                if callable(release):
                    release()
            return digest.hexdigest()

        try:
            fingerprint = await joined_thread(read_object)
            if (await asyncio.to_thread(target.stat)).st_size <= 0:
                raise ValueError("R4 MinIO media object is empty")
            return target, fingerprint
        except BaseException:
            try:
                await asyncio.to_thread(target.unlink, True)
            except OSError:
                pass
            raise

    async def _publish(self, request: AnalysisRequest, stage: TaskStage, message: str) -> None:
        try:
            await self.events.publish(
                request.task_key,
                TaskEvent.of(TaskStatus.of(TaskStatusState.PROCESSING, message), stage),
            )
        except Exception:
            pass


class R4RequestContextCheckpoint(ContextCheckpointPort):
    """Build fresh context on first delivery, then use durable R2 checkpoints."""

    def __init__(
        self,
        delegate: ContextCheckpointPort,
        pipeline: R4MediaPipeline,
        *,
        prepare_chunks: Any | None = None,
    ) -> None:
        self.delegate = delegate
        self.pipeline = pipeline
        self.prepare_chunks = prepare_chunks

    async def load_context(self, media_id: int) -> VideoContext | None:
        request = _CURRENT_R4_REQUEST.get()
        if request is not None and request.media.media_id == media_id:
            # This port runs only after TaskWorker acquired the task lock and
            # accepted the current request identity. Keep retry usage, but
            # never charge a revision for its predecessor's provider calls.
            self.pipeline.telemetry.store.start_for_request(
                request.task_key, request.request_id,
            )
        if request is not None and request.media.media_id == media_id:
            # Resolve the current durable media MD5. A legacy media checkpoint
            # cannot bypass the MD5 artifact/provenance validation.
            context = await self.pipeline.build_context(request)
            prior = await self.delegate.load_context(media_id)
            if prior is not None and prior.source_revision != context.source_revision:
                await self.delegate.save_chunks(media_id, ())
            await self.delegate.save_context(media_id, context)
            # Agent selection deliberately bypasses retrieval for short media.
            # Its M1 read surface still requires durable chunks. Prepare those
            # through the existing chunk/embed/index path under the worker lease;
            # keep the Agent's original short-context selection unchanged.
            if self.prepare_chunks is not None and context.segments and context.segments[-1].end_ms <= 300_000:
                await self.prepare_chunks(media_id, context)
        else:
            context = await self.delegate.load_context(media_id)
        if context is None or request is None or request.media.media_id != media_id:
            return context
        if context.user_goal == request.goal:
            return context
        return context.model_copy(update={"user_goal": request.goal})

    async def save_context(self, media_id: int, context: VideoContext) -> None:
        await self.delegate.save_context(media_id, context)

    async def load_chunks(self, media_id: int):
        return await self.delegate.load_chunks(media_id)

    async def save_chunks(self, media_id: int, chunks):
        await self.delegate.save_chunks(media_id, chunks)


def bind_r4_request(request: AnalysisRequest):
    if not isinstance(request, AnalysisRequest):
        raise TypeError("request must be an AnalysisRequest")
    return _CURRENT_R4_REQUEST.set(request)


def reset_r4_request(token: Any) -> None:
    _CURRENT_R4_REQUEST.reset(token)
