"""The single product composition root for the Python DOVideo application.

The domain and application packages remain provider-neutral.  This module is
the small, explicit wiring boundary used by the CLI and by the Phase 11 live
embedding verification.  It deliberately composes the existing media,
context, chunking, retrieval, and AgentLoop services instead of implementing a
second analysis pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from dovideo.application import (
    AgentLoopService,
    AsrBranchOutcome,
    LongVideoContextService,
    MediaObservationBundle,
    OcrBranchOutcome,
    TranscriptSpan,
    VideoChunkingService,
    VideoContextBuilder,
    VideoEvidenceRetrievalService,
    cosine_similarity,
    fallback_terms,
)
from dovideo.application.evidence import EvidenceVerificationService
from dovideo.domain import (
    AgentBudgetConfig,
    AgentState,
    VideoChunk,
    VideoContext,
    VideoEvidenceHit,
    VideoRetrievalIntent,
    VideoSegment,
)
from dovideo.infrastructure.media import (
    AudioSegmenter,
    AsyncSubprocessRunner,
    FfprobeDurationAdapter,
    FFmpegKeyframeExtractor,
    LocalWhisperTranscriptionAdapter,
    MediaBranchOrchestrator,
    OcrBatchService,
    PillowDifferenceHash,
    SegmentedTranscriptionService,
    TesseractOcrAdapter,
)
from dovideo.infrastructure.providers import (
    LocalChunkSummaryAdapter,
    LocalTfidfEmbeddingAdapter,
    OpenAICompatibleChatClient,
    OpenAICompatibleEmbeddingAdapter,
    OpenAICompatibleModelAdapter,
    ProviderConfig,
    ProviderConfigurationError,
)


EmbeddingMode = Literal["local", "remote"]
ProgressSink = Callable[[str, str], None]


class AnalysisConfigurationError(RuntimeError):
    """Raised when a user-facing analysis cannot be composed safely."""


class EvidenceGuardError(RuntimeError):
    """Raised when the authoritative evidence gate rejects model output."""


class WhisperSegmentPort(Protocol):
    async def transcribe_segment(
        self,
        audio_path: Path,
        *,
        trace_id: str | None = None,
    ) -> str | None:
        ...


@dataclass(frozen=True, slots=True)
class AnalysisSettings:
    """Local tool and runtime settings for one CLI analysis.

    Heavy local ASR dependencies are intentionally not package dependencies.
    The default paths point at the provisioned workspace tools when they are
    present; every path can be overridden through the documented environment
    variables.
    """

    embedding_mode: EmbeddingMode = "local"
    ffmpeg_executable: str = "ffmpeg"
    ffprobe_executable: str = "ffprobe"
    tesseract_executable: str = "tesseract"
    whisper_model: str = "tiny.en"
    whisper_model_root: Path | None = None
    whisper_device: str = "cpu"
    whisper_language: str | None = None
    process_timeout_seconds: float = 15 * 60
    media_timeout_seconds: float = 60 * 60
    workspace_parent: Path | None = None

    def __post_init__(self) -> None:
        if self.embedding_mode not in {"local", "remote"}:
            raise ValueError("embedding_mode must be local or remote")
        if not self.whisper_model.strip():
            raise ValueError("whisper_model must be nonblank")
        if not self.whisper_device.strip():
            raise ValueError("whisper_device must be nonblank")
        if self.process_timeout_seconds <= 0 or self.media_timeout_seconds <= 0:
            raise ValueError("media timeouts must be positive")

    @classmethod
    def from_environment(
        cls,
        *,
        embedding_mode: EmbeddingMode = "local",
        environ: Mapping[str, str] | None = None,
        project_root: Path | None = None,
    ) -> "AnalysisSettings":
        """Resolve CLI settings without importing Whisper or doing I/O."""

        values = os.environ if environ is None else environ
        root = (
            project_root.resolve()
            if project_root is not None
            else Path(__file__).resolve().parents[3]
        )
        ffmpeg_dir = _first_value(values, "DOVIDEO_FFMPEG_DIR", "FFMPEG_DIR")
        ffmpeg = _tool_setting(
            values,
            explicit_names=("DOVIDEO_FFMPEG", "DOVIDEO_FFMPEG_PATH"),
            directory=ffmpeg_dir,
            filename="ffmpeg.exe",
            packaged=(root / "tools" / "ffmpeg" / "ffmpeg.exe",),
            fallback="ffmpeg",
        )
        ffprobe = _tool_setting(
            values,
            explicit_names=("DOVIDEO_FFPROBE", "DOVIDEO_FFPROBE_PATH"),
            directory=ffmpeg_dir,
            filename="ffprobe.exe",
            packaged=(root / "tools" / "ffmpeg" / "ffprobe.exe",),
            fallback="ffprobe",
        )
        tesseract = _tool_setting(
            values,
            explicit_names=(
                "DOVIDEO_TESSERACT_PATH",
                "DOVIDEO_OCR_COMMAND",
                "OCR_COMMAND",
            ),
            directory=None,
            filename="tesseract.exe",
            packaged=(
                root / "tools" / "tesseract" / "install" / "tesseract.exe",
                root / "tesseract.exe",
            ),
            fallback="tesseract",
        )
        model_root_text = _first_value(
            values,
            "DOVIDEO_WHISPER_MODEL_ROOT",
            "WHISPER_MODEL_ROOT",
        )
        model_root = (
            Path(model_root_text).expanduser()
            if model_root_text is not None
            else root / "tools" / "asr" / "models"
        )
        workspace_text = _first_value(
            values,
            "DOVIDEO_MEDIA_WORKSPACE",
            "DOVIDEO_WORKSPACE_PARENT",
        )
        return cls(
            embedding_mode=embedding_mode,
            ffmpeg_executable=ffmpeg,
            ffprobe_executable=ffprobe,
            tesseract_executable=tesseract,
            whisper_model=_first_value(
                values, "DOVIDEO_WHISPER_MODEL", "WHISPER_MODEL"
            )
            or "tiny.en",
            whisper_model_root=model_root,
            whisper_device=_first_value(
                values, "DOVIDEO_WHISPER_DEVICE", "WHISPER_DEVICE"
            )
            or "cpu",
            whisper_language=_first_value(
                values, "DOVIDEO_WHISPER_LANGUAGE", "WHISPER_LANGUAGE"
            ),
            process_timeout_seconds=_float_setting(
                values,
                ("DOVIDEO_PROCESS_TIMEOUT_SECONDS",),
                15 * 60,
            ),
            media_timeout_seconds=_float_setting(
                values,
                ("DOVIDEO_MEDIA_TIMEOUT_SECONDS",),
                60 * 60,
            ),
            workspace_parent=(
                Path(workspace_text).expanduser() if workspace_text is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class AnalysisRun:
    """Safe user-facing result metadata; provider payloads are not retained."""

    source: Path
    goal: str
    duration_seconds: float
    context: VideoContext
    state: AgentState
    media_id: int
    asr_span_count: int
    ocr_observation_count: int
    chunk_count: int
    embedding_dimension: int
    embedding_mode: EmbeddingMode = "local"

    @property
    def result(self) -> Any:
        return self.state.result


class ProgressReporter:
    """Emit each named CLI stage once while allowing nested services to report."""

    def __init__(self, sink: ProgressSink | None = None) -> None:
        self._sink = sink or (lambda _stage, _message: None)
        self._seen: set[str] = set()

    def emit(self, stage: str, message: str) -> None:
        normalized = stage.strip().upper()
        if not normalized or normalized in self._seen:
            return
        self._seen.add(normalized)
        self._sink(normalized, message)

    __call__ = emit


class InMemoryVectorIndex:
    """Small process-local vector index used by the one-video CLI composition."""

    def __init__(self) -> None:
        self._chunks: dict[int, tuple[VideoChunk, ...]] = {}

    async def upsert(self, media_id: int, chunks: tuple[VideoChunk, ...]) -> None:
        self._chunks[int(media_id)] = tuple(chunks)

    async def search(
        self,
        media_id: int,
        query_embedding: tuple[float, ...],
        *,
        limit: int,
    ) -> tuple[Any, ...]:
        from dovideo.application import VectorHit

        chunks = self._chunks.get(int(media_id), ())
        ranked = sorted(
            (
                VectorHit(
                    start_ms=chunk.start_ms,
                    end_ms=chunk.end_ms,
                    score=cosine_similarity(query_embedding, chunk.embedding),
                )
                for chunk in chunks
            ),
            key=lambda hit: (-hit.score, hit.start_ms),
        )
        return tuple(ranked[: max(0, int(limit))])

    async def delete_media(self, media_id: int) -> None:
        self._chunks.pop(int(media_id), None)

    def chunks_for(self, media_id: int) -> tuple[VideoChunk, ...]:
        """Expose only local run metadata, never a provider response."""

        return self._chunks.get(int(media_id), ())


class LocalRetrievalPlanner:
    async def plan_retrieval(self, goal: str) -> VideoRetrievalIntent:
        terms = fallback_terms(goal)
        return VideoRetrievalIntent(
            semantic_query=goal,
            keywords=terms,
            visual_keywords=terms,
        )


class _LocalWhisperSegmentTranscriber:
    """Adapt the existing timestamped local Whisper adapter to ASR segments."""

    def __init__(self, adapter: LocalWhisperTranscriptionAdapter) -> None:
        self._adapter = adapter

    async def transcribe_segment(
        self,
        audio_path: Path,
        *,
        trace_id: str | None = None,
    ) -> str:
        spans = await self._adapter.transcribe_path(audio_path, trace_id=trace_id)
        return " ".join(span.text.strip() for span in spans if span.text.strip())


class _ProgressSummary:
    def __init__(self, inner: Any, progress: ProgressReporter) -> None:
        self._inner = inner
        self._progress = progress

    async def summarize_chunk(self, segments: Sequence[VideoSegment]) -> Any:
        self._progress.emit("CHUNK", "Building five-minute video chunks")
        return await self._inner.summarize_chunk(segments)


class _ProgressEmbedding:
    def __init__(self, inner: Any, progress: ProgressReporter) -> None:
        self._inner = inner
        self._progress = progress

    async def embed(self, text: str) -> tuple[float, ...]:
        self._progress.emit("EMBEDDING", "Generating configured chunk/query vectors")
        return await self._inner.embed(text)


class _ProgressRetrievalPlanner:
    def __init__(self, inner: Any, progress: ProgressReporter) -> None:
        self._inner = inner
        self._progress = progress

    async def plan_retrieval(self, goal: str) -> VideoRetrievalIntent:
        self._progress.emit("RETRIEVAL", "Ranking temporal evidence candidates")
        return await self._inner.plan_retrieval(goal)


class _ProgressChatClient:
    def __init__(self, inner: OpenAICompatibleChatClient, progress: ProgressReporter) -> None:
        self._inner = inner
        self._progress = progress

    async def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        stage: str,
    ) -> str:
        normalized = stage.upper()
        if normalized.startswith("PLANNER") or normalized == "REPLANNER":
            public_stage = "PLANNER"
        elif normalized == "EXECUTOR":
            public_stage = "EXECUTOR"
        elif normalized == "CRITIC":
            public_stage = "CRITIC"
        else:
            public_stage = normalized
        self._progress.emit(public_stage, f"Running {public_stage.title()} role")
        return await self._inner.complete(messages, stage=stage)


class VideoAnalysisApplication:
    """Compose one real local-media analysis through the existing services."""

    def __init__(
        self,
        settings: AnalysisSettings | None = None,
        *,
        progress: ProgressSink | None = None,
        model_config: ProviderConfig | None = None,
        embedding_config: ProviderConfig | None = None,
        whisper_factory: Callable[[AnalysisSettings], LocalWhisperTranscriptionAdapter]
        | None = None,
    ) -> None:
        self.settings = settings or AnalysisSettings.from_environment()
        self._progress = ProgressReporter(progress)
        self._model_config = model_config
        self._embedding_config = embedding_config
        self._whisper_factory = whisper_factory

    async def analyze(self, video_path: str | os.PathLike[str], goal: str) -> AnalysisRun:
        source = Path(video_path).expanduser().resolve()
        if not source.is_file():
            raise AnalysisConfigurationError(f"video file does not exist: {source}")
        if not isinstance(goal, str) or not goal.strip():
            raise AnalysisConfigurationError("analysis goal must be nonblank")
        goal = goal.strip()

        model_config = self._model_config or _model_config_from_environment()
        embedding_config = self._embedding_config
        if self.settings.embedding_mode == "remote" and embedding_config is None:
            embedding_config = embedding_provider_config_from_environment(required=True)

        self._progress.emit("MEDIA", "Probing local media with ffprobe")
        runner = AsyncSubprocessRunner(
            default_timeout=self.settings.process_timeout_seconds
        )
        duration = await FfprobeDurationAdapter(
            runner,
            executable=self.settings.ffprobe_executable,
        ).probe(source)
        _prepend_tool_directory(self.settings.ffmpeg_executable)

        self._progress.emit("ASR", "Extracting audio and running local Whisper")
        self._progress.emit("OCR", "Extracting keyframes and running Tesseract OCR")
        whisper = (
            self._whisper_factory(self.settings)
            if self._whisper_factory is not None
            else LocalWhisperTranscriptionAdapter.from_openai_whisper(
                self.settings.whisper_model,
                model_root=self.settings.whisper_model_root,
                device=self.settings.whisper_device,
                language=self.settings.whisper_language,
            )
        )
        media = MediaBranchOrchestrator(
            AudioSegmenter(
                runner,
                executable=self.settings.ffmpeg_executable,
                timeout=self.settings.process_timeout_seconds,
            ),
            SegmentedTranscriptionService(_LocalWhisperSegmentTranscriber(whisper)),
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
            ),
            total_timeout_seconds=self.settings.media_timeout_seconds,
        )
        observations = await media.collect(
            str(source),
            parent=self.settings.workspace_parent,
        )
        if not observations.asr.observations and not observations.ocr.observations:
            raise AnalysisConfigurationError(
                "media produced no usable ASR or OCR observations"
            )

        self._progress.emit("CONTEXT", "Building timestamped VideoContext windows")
        context = VideoContextBuilder().build(
            str(source),
            goal,
            observations,
            media_content_identity=_file_content_identity(source),
        )
        self._progress.emit("CHUNK", "Preparing five-minute chunking service")
        self._progress.emit("EMBEDDING", f"Embedding mode: {self.settings.embedding_mode}")
        embedder = self._build_embedder(context, goal, embedding_config)
        embedding = _ProgressEmbedding(embedder, self._progress)
        vector_index = InMemoryVectorIndex()
        retrieval = VideoEvidenceRetrievalService(
            _ProgressRetrievalPlanner(LocalRetrievalPlanner(), self._progress),
            embedding,
            vector_index,
        )
        long_context = LongVideoContextService(
            _ProgressChunkingService(
                VideoChunkingService(
                    _ProgressSummary(LocalChunkSummaryAdapter(), self._progress),
                    embedding,
                ),
                self._progress,
            ),
            retrieval,
        )
        self._progress.emit("RETRIEVAL", "Preparing hybrid temporal retrieval")

        chat_client = OpenAICompatibleChatClient(model_config)
        roles = OpenAICompatibleModelAdapter(
            _ProgressChatClient(chat_client, self._progress)
        )
        try:
            agent = AgentLoopService(
                context_service=long_context,
                planner=roles.planner,
                executor=roles.executor,
                critic=roles.critic,
                budget_config=_agent_budget_from_environment(),
            )
            state = await agent.run(
                context,
                media_id=media_id_for_source(source),
            )
        finally:
            await chat_client.aclose()

        if state.result is None or state.critique is None:
            raise EvidenceGuardError("AgentLoop returned no complete guarded result")
        if not state.critique.passed:
            raise EvidenceGuardError("Evidence Guard rejected the final Critic result")
        self._progress.emit("EVIDENCE", "Checking claims against source text and time windows")
        verifier = EvidenceVerificationService()
        if not all(
            verifier.supported(context, evidence)
            and verifier.timestamp_covered(context, evidence)
            for evidence in state.result.evidence
        ):
            raise EvidenceGuardError("final evidence is not grounded in VideoContext")

        chunks = vector_index.chunks_for(media_id_for_source(source))
        embedding_dimension = len(chunks[0].embedding) if chunks else 0
        self._progress.emit("DONE", "Analysis complete; guarded result is ready")
        return AnalysisRun(
            source=source,
            goal=goal,
            duration_seconds=duration.seconds,
            context=context,
            state=state,
            media_id=media_id_for_source(source),
            asr_span_count=len(observations.asr.observations),
            ocr_observation_count=len(observations.ocr.observations),
            chunk_count=len(chunks),
            embedding_dimension=embedding_dimension,
            embedding_mode=self.settings.embedding_mode,
        )

    def _build_embedder(
        self,
        context: VideoContext,
        goal: str,
        embedding_config: ProviderConfig | None,
    ) -> Any:
        if self.settings.embedding_mode == "remote":
            if embedding_config is None:
                raise AnalysisConfigurationError("remote embedding configuration is missing")
            return OpenAICompatibleEmbeddingAdapter(embedding_config)
        local = LocalTfidfEmbeddingAdapter(max_features=256)
        corpus = [goal, context.transcript_text()]
        corpus.extend(
            segment.transcript + "\n" + " ".join(segment.ocr_texts)
            for segment in context.segments
        )
        try:
            local.fit(corpus)
        except (TypeError, ValueError) as exc:
            raise AnalysisConfigurationError(
                "local embedding could not build a vocabulary from media evidence"
            ) from exc
        return local


class _ProgressChunkingService:
    """Forward the existing chunking service while preserving one progress seam."""

    def __init__(self, inner: VideoChunkingService, progress: ProgressReporter) -> None:
        self._inner = inner
        self._progress = progress

    async def build(self, segments: Iterable[VideoSegment | None]) -> tuple[VideoChunk, ...]:
        self._progress.emit("CHUNK", "Building five-minute video chunks")
        return await self._inner.build(segments)


def load_timestamped_asr_artifact(
    path: str | os.PathLike[str],
) -> tuple[dict[str, Any], tuple[TranscriptSpan, ...]]:
    """Load an existing local Whisper JSON artifact without synthesizing text."""

    artifact = Path(path)
    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AnalysisConfigurationError(f"could not read ASR artifact: {artifact}") from exc
    if not isinstance(payload, dict) or not str(payload.get("text", "")).strip():
        raise AnalysisConfigurationError("ASR artifact has no transcript text")
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise AnalysisConfigurationError("ASR artifact has no timestamped segments")
    spans: list[TranscriptSpan] = []
    previous_end = -1
    for item in raw_segments:
        if not isinstance(item, Mapping):
            raise AnalysisConfigurationError("ASR artifact contains a malformed segment")
        try:
            start = float(item["start"])
            end = float(item["end"])
            text = item["text"]
        except (KeyError, TypeError, ValueError) as exc:
            raise AnalysisConfigurationError("ASR artifact segment is incomplete") from exc
        start_ms = round(start * 1000)
        end_ms = round(end * 1000)
        if (
            not isinstance(text, str)
            or not text.strip()
            or start_ms < 0
            or end_ms <= start_ms
            or start_ms < previous_end
        ):
            raise AnalysisConfigurationError("ASR artifact contains invalid ordered spans")
        spans.append(TranscriptSpan(start_ms=start_ms, end_ms=end_ms, text=text))
        previous_end = end_ms
    return payload, tuple(spans)


def media_id_for_source(source: str | os.PathLike[str]) -> int:
    """Return a deterministic positive process-local media identifier."""

    digest = hashlib.sha256(str(Path(source).expanduser().resolve()).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False) or 1


def _file_content_identity(source: Path) -> str:
    """Return a streaming content identity for local composition contexts."""

    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def embedding_provider_config_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    required: bool = True,
) -> ProviderConfig:
    """Build the remote embedding config using the Java-compatible names.

    A dedicated embedding key is preferred so a chat-only credential cannot
    accidentally be used as a semantic-vector credential.  The legacy
    ``SILICONFLOW_API_KEY`` fallback is retained for compatibility with the
    original Java ``application.properties``.
    """

    values = os.environ if environ is None else environ
    api_key = _first_value(
        values,
        "DOVIDEO_EMBEDDING_API_KEY",
        "DOVIDEO_EMBEDDING_KEY",
        "EMBEDDING_API_KEY",
        "SILICONFLOW_API_KEY",
    )
    if required and not api_key:
        raise ProviderConfigurationError(
            "remote embedding credential is required; set DOVIDEO_EMBEDDING_API_KEY"
        )
    endpoint = _first_value(
        values,
        "DOVIDEO_EMBEDDING_BASE_URL",
        "EMBEDDING_BASE_URL",
        "DOVIDEO_MODEL_BASE_URL",
        "DOVIDEO_BASE_URL",
        "SILICONFLOW_BASE_URL",
    ) or "https://api.siliconflow.cn/v1"
    explicit_url = _first_value(
        values,
        "DOVIDEO_EMBEDDING_URL",
        "EMBEDDING_URL",
    )
    embedding_url: str | None = None
    if explicit_url is not None:
        embedding_url = explicit_url.rstrip("/")
        if not embedding_url.endswith("/embeddings"):
            embedding_url += "/embeddings"
        base_url = embedding_url[: -len("/embeddings")]
    else:
        base_url = endpoint
    model = _first_value(
        values,
        "DOVIDEO_EMBEDDING_MODEL",
        "EMBEDDING_MODEL",
    ) or "BAAI/bge-m3"
    return ProviderConfig(
        base_url=base_url,
        model=model,
        api_key=api_key,
        embedding_model=model,
        embedding_url=embedding_url,
        timeout_seconds=_float_setting(
            values,
            ("DOVIDEO_EMBEDDING_TIMEOUT_SECONDS", "DOVIDEO_TIMEOUT_SECONDS"),
            120.0,
        ),
        max_attempts=_int_setting(values, ("DOVIDEO_EMBEDDING_MAX_ATTEMPTS",), 1),
    )


def render_analysis(run: AnalysisRun) -> str:
    """Render only user-facing analysis fields, with timestamp ranges."""

    result = run.result
    if result is None:
        raise EvidenceGuardError("cannot render an incomplete AgentLoop result")
    lines = [
        f"# {result.title}",
        "",
        f"Source: `{run.source}`",
        f"Duration: {_format_duration(run.duration_seconds)}",
        f"Embedding mode: "
        f"{'local TF-IDF' if run.embedding_mode == 'local' else 'remote OpenAI-compatible'}",
        "",
        "## Conclusions",
        "",
    ]
    lines.extend(f"- {value}" for value in result.conclusions)
    lines.extend(("", "## Evidence", ""))
    for evidence in result.evidence:
        segment = _segment_for_timestamp(run.context, evidence.timestamp_ms)
        if segment is None:
            range_text = _format_ms(evidence.timestamp_ms)
        else:
            range_text = f"{_format_ms(segment.start_ms)}–{_format_ms(segment.end_ms)}"
        lines.append(
            f"- [{_format_ms(evidence.timestamp_ms)} in {range_text}] "
            f"{evidence.source}: {evidence.content}"
        )
    lines.extend(("", "## Suggestions", ""))
    lines.extend(f"- {value}" for value in result.suggestions)
    for section in result.sections:
        lines.extend(("", f"## {section.title}", ""))
        lines.extend(f"- {value}" for value in section.items)
    return "\n".join(lines).rstrip() + "\n"


def _model_config_from_environment() -> ProviderConfig:
    config = ProviderConfig.from_environment(required=True)
    if config is None:
        raise ProviderConfigurationError(
            "model provider configuration is required; set DOVIDEO_MODEL_BASE_URL"
        )
    return config


def _agent_budget_from_environment(environ: Mapping[str, str] | None = None) -> AgentBudgetConfig:
    values = os.environ if environ is None else environ
    return AgentBudgetConfig(
        max_rounds=_int_setting(values, ("DOVIDEO_AGENT_MAX_ROUNDS",), 2),
        max_duration_ms=_int_setting(
            values, ("DOVIDEO_AGENT_MAX_DURATION_MS",), 240_000
        ),
        max_estimated_tokens=_int_setting(
            values, ("DOVIDEO_AGENT_MAX_ESTIMATED_TOKENS",), 50_000
        ),
        max_estimated_cost=_float_setting(
            values, ("DOVIDEO_AGENT_MAX_ESTIMATED_COST",), 0.0
        ),
    )


def _tool_setting(
    values: Mapping[str, str],
    *,
    explicit_names: Sequence[str],
    directory: str | None,
    filename: str,
    packaged: Sequence[Path],
    fallback: str,
) -> str:
    explicit = _first_value(values, *explicit_names)
    if explicit is not None:
        return explicit
    if directory is not None:
        candidate = Path(directory).expanduser()
        if candidate.is_dir():
            return str(candidate / filename)
        if candidate.is_file():
            return str(candidate)
        # Configuration should remain inspectable before a media run.  Treat
        # a directory-shaped override as the requested tool location even
        # when the operator has not installed the binary yet; the subprocess
        # boundary will then return the actionable launch error.
        if candidate.suffix.lower() != ".exe":
            return str(candidate / filename)
        return str(candidate)
    for candidate in packaged:
        if candidate.is_file():
            return str(candidate)
    return fallback


def _first_value(values: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = values.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _float_setting(values: Mapping[str, str], names: Sequence[str], default: float) -> float:
    text = _first_value(values, *names)
    if text is None:
        return default
    try:
        return float(text)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProviderConfigurationError("numeric provider setting is invalid") from exc


def _int_setting(values: Mapping[str, str], names: Sequence[str], default: int) -> int:
    text = _first_value(values, *names)
    if text is None:
        return default
    try:
        return int(text)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProviderConfigurationError("integer provider setting is invalid") from exc


def _prepend_tool_directory(executable: str) -> None:
    """Make a configured FFmpeg sibling visible to Whisper's subprocess call."""

    path = Path(executable)
    if not path.is_file():
        return
    directory = str(path.resolve().parent)
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    if directory not in parts:
        os.environ["PATH"] = directory + os.pathsep + current


def _segment_for_timestamp(
    context: VideoContext,
    timestamp_ms: int,
) -> VideoSegment | None:
    return next(
        (
            segment
            for segment in context.segments
            if segment.start_ms <= timestamp_ms < segment.end_ms
        ),
        None,
    )


def _format_ms(milliseconds: int) -> str:
    total_seconds = max(0, int(milliseconds)) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _format_duration(seconds: float) -> str:
    return _format_ms(round(max(0.0, seconds) * 1000))


__all__ = [
    "AnalysisConfigurationError",
    "AnalysisRun",
    "AnalysisSettings",
    "EmbeddingMode",
    "EvidenceGuardError",
    "InMemoryVectorIndex",
    "LocalRetrievalPlanner",
    "ProgressReporter",
    "VideoAnalysisApplication",
    "embedding_provider_config_from_environment",
    "load_timestamped_asr_artifact",
    "media_id_for_source",
    "render_analysis",
]
