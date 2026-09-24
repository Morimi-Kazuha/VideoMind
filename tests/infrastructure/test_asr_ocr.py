from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest

from dovideo.application import (
    AsrBranchOutcome,
    BranchStatus,
    OcrBranchOutcome,
    OcrObservation,
    TranscriptSpan,
)
from dovideo.infrastructure.media import (
    AllAsrSegmentsFailed,
    AllOcrFramesFailed,
    AsrHttpAdapter,
    AsrRequestRejected,
    AsrResponseError,
    AsrTransientFailure,
    AudioSegment,
    Keyframe,
    KeyframeSelection,
    MediaBranchOrchestrator,
    MediaBranchesTimeout,
    MediaWorkspace,
    OcrBatchService,
    OcrImageMissing,
    SegmentedTranscriptionService,
    SubprocessExecutionError,
    SubprocessResult,
    TesseractOcrAdapter,
    hamming_distance,
    difference_hash_from_grayscale,
)
from dovideo.infrastructure.media.hashing import PillowDifferenceHash
from dovideo.infrastructure.media.telemetry import InMemoryTelemetry


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class FakeHttpClient:
    def __init__(self, responses: Sequence[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def post(self, url: str, *, headers, files, data):
        self.calls.append(
            {"url": url, "headers": headers, "files": files, "data": data}
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeSegmentTranscriber:
    def __init__(self, results: dict[str, object]) -> None:
        self.results = results
        self.calls: list[str] = []

    async def transcribe_segment(self, audio_path: Path, *, trace_id=None) -> str | None:
        self.calls.append(audio_path.name)
        result = self.results[audio_path.name]
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


class FakeFrameOcr:
    def __init__(self, results: dict[str, object]) -> None:
        self.results = results
        self.calls: list[str] = []

    async def recognize_frame(self, image_path: Path, *, trace_id=None) -> str | None:
        self.calls.append(image_path.name)
        result = self.results[image_path.name]
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


class FakeEvidence:
    def __init__(self, results: dict[str, object]) -> None:
        self.results = results
        self.calls: list[str] = []

    async def persist_frame(self, image_path: Path, *, timestamp_ms: int) -> str:
        self.calls.append(f"{image_path.name}:{timestamp_ms}")
        result = self.results[image_path.name]
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


class FakeHasher:
    def __init__(self, values: dict[str, int | Exception]) -> None:
        self.values = values
        self.calls: list[str] = []

    def difference_hash(self, image_path: Path) -> int:
        self.calls.append(image_path.name)
        result = self.values[image_path.name]
        if isinstance(result, Exception):
            raise result
        return result


class FakeRunner:
    def __init__(self, result: SubprocessResult) -> None:
        self.result = result
        self.calls: list[tuple[str, ...]] = []

    async def run(self, args, *, cwd=None, timeout=None, env=None):
        self.calls.append(tuple(str(value) for value in args))
        return self.result


async def no_sleep(delay: float) -> None:
    return None


def _make_audio_segments(workspace: MediaWorkspace, directory: Path, indexes=(0, 1, 2)) -> tuple[AudioSegment, ...]:
    segments: list[AudioSegment] = []
    for index in indexes:
        path = directory / f"audio_{index:03d}.mp3"
        path.write_bytes(b"audio")
        segments.append(
            AudioSegment(
                index=index,
                start_ms=index * 60_000,
                artifact=workspace.artifact(path),
            )
        )
    return tuple(segments)


async def _make_keyframes(workspace: MediaWorkspace, directory: Path, indexes=(0, 1, 2)) -> tuple[Keyframe, ...]:
    frames: list[Keyframe] = []
    for index in indexes:
        path = directory / f"frame_{index:06d}.jpg"
        path.write_bytes(b"image")
        frames.append(
            Keyframe(
                index=index,
                timestamp_ms=index * 30_000,
                artifact=workspace.artifact(path),
                selection=KeyframeSelection.SCENE_CHANGE,
            )
        )
    return tuple(frames)


@pytest.mark.asyncio
async def test_http_asr_builds_multipart_bearer_request_and_trims_text(tmp_path: Path) -> None:
    audio = tmp_path / "audio_000.mp3"
    audio.write_bytes(b"mp3")
    client = FakeHttpClient([FakeResponse(200, {"text": "  hello ASR  "})])
    adapter = AsrHttpAdapter(
        "https://asr.invalid/transcribe",
        "  secret-key  ",
        "demo",
        client=client,
    )

    assert await adapter.transcribe_segment(audio) == "hello ASR"
    call = client.calls[0]
    assert call["url"] == "https://asr.invalid/transcribe"
    assert call["headers"] == {"Authorization": "Bearer secret-key"}
    assert call["data"] == {"model": "demo"}
    assert call["files"] == {"file": ("audio_000.mp3", b"mp3", "application/octet-stream")}


@pytest.mark.parametrize(
    "kwargs",
    (
        {"max_attempts": True},
        {"max_attempts": 1.5},
        {"max_attempts": 0},
        {"retry_base_seconds": float("nan")},
        {"retry_base_seconds": float("inf")},
        {"retry_base_seconds": -1.0},
    ),
)
def test_http_asr_rejects_invalid_retry_settings(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AsrHttpAdapter("https://asr.invalid", "secret", "model", **kwargs)


@pytest.mark.asyncio
async def test_http_asr_retries_429_and_5xx_with_injected_backoff() -> None:
    client = FakeHttpClient(
        [FakeResponse(429, {}), FakeResponse(503, {}), FakeResponse(200, {"text": "ok"})]
    )
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    # A small temporary file avoids involving any network or provider.
    path = Path(__file__).with_name("asr-fixture.mp3")
    path.write_bytes(b"fixture")
    try:
        adapter = AsrHttpAdapter("https://asr.invalid", "secret", "model", client=client, sleeper=sleeper)
        assert await adapter.transcribe_segment(path) == "ok"
    finally:
        path.unlink(missing_ok=True)
    assert len(client.calls) == 3
    assert delays == [1.0, 2.0]


@pytest.mark.asyncio
async def test_http_asr_retries_transport_errors_at_most_three_times() -> None:
    client = FakeHttpClient([OSError("network"), TimeoutError("timeout"), OSError("gone")])
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    path = Path(__file__).with_name("asr-network-fixture.mp3")
    path.write_bytes(b"fixture")
    try:
        adapter = AsrHttpAdapter("https://asr.invalid", "secret", "model", client=client, sleeper=sleeper)
        with pytest.raises(AsrTransientFailure) as caught:
            await adapter.transcribe_segment(path)
    finally:
        path.unlink(missing_ok=True)
    assert len(client.calls) == 3
    assert delays == [1.0, 2.0]
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_http_asr_4xx_and_empty_success_are_not_retried(tmp_path: Path) -> None:
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"fixture")
    client = FakeHttpClient([FakeResponse(400, {"error": "bad"})])
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    adapter = AsrHttpAdapter("https://asr.invalid", "secret", "model", client=client, sleeper=sleeper)
    with pytest.raises(AsrRequestRejected) as rejected:
        await adapter.transcribe_segment(path)
    assert rejected.value.status_code == 400
    assert len(client.calls) == 1
    assert delays == []

    empty_client = FakeHttpClient([FakeResponse(200, {"text": "  "})])
    empty_adapter = AsrHttpAdapter("https://asr.invalid", "secret", "model", client=empty_client, sleeper=sleeper)
    with pytest.raises(AsrResponseError):
        await empty_adapter.transcribe_segment(path)
    assert len(empty_client.calls) == 1


@pytest.mark.asyncio
async def test_segmented_asr_keeps_order_offsets_and_partial_failures(tmp_path: Path) -> None:
    async with MediaWorkspace(parent=tmp_path) as workspace:
        audio_dir = await workspace.directory("audio")
        segments = _make_audio_segments(workspace, audio_dir)
        error = RuntimeError("segment two")
        transcriber = FakeSegmentTranscriber(
            {"audio_000.mp3": " first ", "audio_001.mp3": None, "audio_002.mp3": error}
        )
        telemetry = InMemoryTelemetry()
        outcome = await SegmentedTranscriptionService(transcriber, telemetry).transcribe(segments)

    assert outcome.status is BranchStatus.PARTIAL_FAILURE
    assert outcome.observations == (TranscriptSpan(0, 60_000, "first"),)
    assert transcriber.calls == ["audio_000.mp3", "audio_001.mp3", "audio_002.mp3"]
    assert telemetry.counts == {"asrCalls": 3, "asrSegmentFailures": 1}


@pytest.mark.asyncio
async def test_segmented_asr_all_failure_preserves_last_cause(tmp_path: Path) -> None:
    async with MediaWorkspace(parent=tmp_path) as workspace:
        audio_dir = await workspace.directory("audio")
        segments = _make_audio_segments(workspace, audio_dir, indexes=(0, 1))
        first, last = RuntimeError("first"), ValueError("last")
        transcriber = FakeSegmentTranscriber({"audio_000.mp3": first, "audio_001.mp3": last})
        with pytest.raises(AllAsrSegmentsFailed) as caught:
            await SegmentedTranscriptionService(transcriber).transcribe(segments)

    assert caught.value.causes == (first, last)
    assert caught.value.last_cause is last
    assert caught.value.__cause__ is last


@pytest.mark.asyncio
async def test_segmented_asr_all_empty_without_errors_is_success(tmp_path: Path) -> None:
    async with MediaWorkspace(parent=tmp_path) as workspace:
        audio_dir = await workspace.directory("audio")
        segments = _make_audio_segments(workspace, audio_dir, indexes=(0,))
        outcome = await SegmentedTranscriptionService(
            FakeSegmentTranscriber({"audio_000.mp3": "   "})
        ).transcribe(segments)
    assert outcome.status is BranchStatus.SUCCESS
    assert outcome.observations == ()


def test_branch_outcomes_enforce_failure_and_attempt_invariants() -> None:
    span = TranscriptSpan(0, 60_000, "text")
    observation = OcrObservation(0, "")
    cause = RuntimeError("item failure")

    valid_asr = AsrBranchOutcome(
        observations=(span,),
        attempted=2,
        failed=1,
        causes=(cause,),
    )
    assert valid_asr.status is BranchStatus.PARTIAL_FAILURE
    with pytest.raises(TypeError):
        AsrBranchOutcome(attempted=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AsrBranchOutcome(failed=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AsrBranchOutcome(observations=(span,), attempted=1, failed=1, causes=(cause,))
    with pytest.raises(ValueError):
        AsrBranchOutcome(attempted=0, failed=1, causes=(cause,))
    with pytest.raises(TypeError):
        AsrBranchOutcome(causes=(asyncio.CancelledError(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AsrBranchOutcome(branch_error=asyncio.CancelledError())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AllAsrSegmentsFailed((asyncio.CancelledError(),))  # type: ignore[arg-type]

    valid_ocr = OcrBranchOutcome(
        observations=(observation,),
        attempted=2,
        failed=1,
        causes=(cause,),
        skipped_duplicates=3,
    )
    assert valid_ocr.status is BranchStatus.PARTIAL_FAILURE
    with pytest.raises(TypeError):
        OcrBranchOutcome(attempted=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        OcrBranchOutcome(failed=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        OcrBranchOutcome(skipped_duplicates=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        OcrBranchOutcome(observations=(observation,), attempted=2)
    with pytest.raises(ValueError):
        OcrBranchOutcome(attempted=0, failed=1, causes=(cause,))
    with pytest.raises(TypeError):
        OcrBranchOutcome(causes=(asyncio.CancelledError(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        OcrBranchOutcome(branch_error=asyncio.CancelledError())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AllOcrFramesFailed((asyncio.CancelledError(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AllOcrFramesFailed((cause,), attempted=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AllOcrFramesFailed((cause,), skipped_duplicates=True)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_tesseract_adapter_uses_java_arguments_and_checks_file(tmp_path: Path) -> None:
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"image")
    runner = FakeRunner(SubprocessResult(("tesseract",), 0, "  OCR text \n", ""))
    adapter = TesseractOcrAdapter(runner, timeout=120)

    assert await adapter.recognize_frame(image) == "OCR text"
    assert runner.calls[0] == ("tesseract", str(image), "stdout", "-l", "chi_sim+eng")
    with pytest.raises(OcrImageMissing):
        await adapter.recognize_frame(tmp_path / "missing.jpg")


def test_difference_hash_and_hamming_threshold() -> None:
    increasing = tuple(tuple(x for x in range(9)) for _ in range(8))
    decreasing = tuple(tuple(8 - x for x in range(9)) for _ in range(8))
    left, right = difference_hash_from_grayscale(increasing), difference_hash_from_grayscale(decreasing)
    assert left == 0
    assert right == (1 << 64) - 1
    assert hamming_distance(left, right) == 64


def test_pillow_difference_hash_decodes_real_small_image(tmp_path: Path) -> None:
    image_module = pytest.importorskip("PIL.Image")
    image = image_module.new("L", (9, 8))
    for y in range(8):
        for x in range(9):
            image.putpixel((x, y), 255 if x < 4 else 0)
    path = tmp_path / "small.png"
    image.save(path)

    value = PillowDifferenceHash().difference_hash(path)

    assert value == difference_hash_from_grayscale(
        tuple(tuple(255 if x < 4 else 0 for x in range(9)) for _ in range(8))
    )


@pytest.mark.asyncio
async def test_ocr_batch_skips_duplicates_records_empty_text_and_falls_back_upload(tmp_path: Path) -> None:
    async with MediaWorkspace(parent=tmp_path) as workspace:
        frame_dir = await workspace.directory("frames")
        frames = await _make_keyframes(workspace, frame_dir)
        hasher = FakeHasher({"frame_000000.jpg": 0, "frame_000001.jpg": 1, "frame_000002.jpg": 63})
        ocr = FakeFrameOcr({"frame_000000.jpg": "  ", "frame_000002.jpg": "visible"})
        evidence = FakeEvidence({"frame_000000.jpg": "https://objects/frame0", "frame_000002.jpg": OSError("store")})
        telemetry = InMemoryTelemetry()
        outcome = await OcrBatchService(ocr, hasher, evidence, telemetry).process("video.mp4", frames)

    assert outcome.status is BranchStatus.SUCCESS
    assert outcome.skipped_duplicates == 1
    assert outcome.attempted == 2
    assert outcome.observations == (
        OcrObservation(0, "", "https://objects/frame0"),
        OcrObservation(60_000, "visible", "video.mp4#timestampMs=60000"),
    )
    assert ocr.calls == ["frame_000000.jpg", "frame_000002.jpg"]
    assert telemetry.counts == {"ocrCalls": 2, "frameUploadFailures": 1}


@pytest.mark.asyncio
async def test_ocr_batch_all_failure_preserves_causes(tmp_path: Path) -> None:
    async with MediaWorkspace(parent=tmp_path) as workspace:
        frame_dir = await workspace.directory("frames")
        frames = await _make_keyframes(workspace, frame_dir, indexes=(0, 1))
        first, last = RuntimeError("first OCR"), ValueError("last OCR")
        service = OcrBatchService(
            FakeFrameOcr({"frame_000000.jpg": first, "frame_000001.jpg": last}),
            FakeHasher({"frame_000000.jpg": 0, "frame_000001.jpg": 63}),
        )
        with pytest.raises(AllOcrFramesFailed) as caught:
            await service.process("video.mp4", frames)
    assert caught.value.causes == (first, last)
    assert caught.value.__cause__ is last


@pytest.mark.asyncio
async def test_ocr_hash_failure_is_a_structured_branch_failure(tmp_path: Path) -> None:
    async with MediaWorkspace(parent=tmp_path) as workspace:
        frame_dir = await workspace.directory("frames")
        frames = await _make_keyframes(workspace, frame_dir, indexes=(0,))
        hash_error = RuntimeError("decode failed")
        service = OcrBatchService(
            FakeFrameOcr({"frame_000000.jpg": "unused"}),
            FakeHasher({"frame_000000.jpg": hash_error}),
        )
        with pytest.raises(AllOcrFramesFailed) as caught:
            await service.process("video.mp4", frames)

    assert caught.value.causes == (hash_error,)
    assert caught.value.attempted == 1


class FakeAudioExtractor:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def segment(self, source: str, workspace: MediaWorkspace):
        if self.error is not None:
            raise self.error
        directory = await workspace.directory("audio")
        return _make_audio_segments(workspace, directory, indexes=(0,))


class FakeFrameExtractor:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def extract(self, source: str, workspace: MediaWorkspace):
        if self.error is not None:
            raise self.error
        directory = await workspace.directory("frames")
        return await _make_keyframes(workspace, directory, indexes=(0,))


class MediaFailureTelemetry(InMemoryTelemetry):
    def __init__(self) -> None:
        super().__init__()
        self.media_failures: list[dict[str, object]] = []

    def record_media_failure(self, **failure: object) -> None:
        self.media_failures.append(failure)


class SelfCancellingAudioExtractor:
    async def segment(self, source: str, workspace: MediaWorkspace):
        del source, workspace
        raise asyncio.CancelledError()


class SlowAudioExtractor(FakeAudioExtractor):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    async def segment(self, source: str, workspace: MediaWorkspace):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super().segment(source, workspace)


class SlowFrameExtractor(FakeFrameExtractor):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    async def extract(self, source: str, workspace: MediaWorkspace):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super().extract(source, workspace)


def _orchestrator(audio, frames, *, timeout=5, telemetry=None):
    # Branch fakes are intentionally typed by behavior; only the application
    # ports, not infrastructure classes, cross this seam.
    return MediaBranchOrchestrator(
        audio,
        SegmentedTranscriptionService(FakeSegmentTranscriber({"audio_000.mp3": "ok"}), telemetry),
        frames,
        OcrBatchService(
            FakeFrameOcr({"frame_000000.jpg": "ok"}),
            FakeHasher({"frame_000000.jpg": 0}),
            telemetry=telemetry,
        ),
        telemetry,
        total_timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_orchestrator_returns_one_branch_failure_and_counts_metric(tmp_path: Path) -> None:
    telemetry = InMemoryTelemetry()
    branch_error = RuntimeError("audio branch")
    orchestrator = _orchestrator(
        FakeAudioExtractor(branch_error),
        FakeFrameExtractor(),
        telemetry=telemetry,
    )
    bundle = await orchestrator.collect("video.mp4", parent=tmp_path)
    assert bundle.asr.status is BranchStatus.FAILED
    assert bundle.asr.attempted == 0
    assert bundle.asr.failed == 0
    assert bundle.asr.causes == ()
    assert bundle.asr.branch_error is branch_error
    assert bundle.asr.errors == (branch_error,)
    assert bundle.ocr.status is BranchStatus.SUCCESS
    assert telemetry.count("asrBranchFailures") == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_orchestrator_records_keyframe_failure_stage(tmp_path: Path) -> None:
    telemetry = MediaFailureTelemetry()
    error = SubprocessExecutionError(
        "FFmpeg rejected option vsync",
        command=("ffmpeg", "private-source.mp4"),
        stderr="private diagnostic output",
        returncode=64,
    )
    orchestrator = _orchestrator(
        FakeAudioExtractor(),
        FakeFrameExtractor(error),
        telemetry=telemetry,
    )

    bundle = await orchestrator.collect("video.mp4", parent=tmp_path)

    assert bundle.ocr.status is BranchStatus.FAILED
    assert bundle.ocr.branch_error is error
    assert telemetry.count("ocrBranchFailures") == 1
    assert telemetry.media_failures == [
        {
            "media_branch": "OCR",
            "failure_stage": "KEYFRAME_EXTRACTION",
            "error": error,
        }
    ]
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_orchestrator_both_failure_preserves_both_causes_and_cleans(tmp_path: Path) -> None:
    telemetry = InMemoryTelemetry()
    asr_error, ocr_error = RuntimeError("audio branch"), ValueError("OCR branch")
    orchestrator = _orchestrator(
        FakeAudioExtractor(asr_error),
        FakeFrameExtractor(ocr_error),
        telemetry=telemetry,
    )
    with pytest.raises(Exception) as caught:
        await orchestrator.collect("video.mp4", parent=tmp_path)
    assert caught.value.__class__.__name__ == "BothMediaBranchesFailed"
    assert caught.value.causes == (asr_error, ocr_error)
    assert telemetry.counts["asrBranchFailures"] == 1
    assert telemetry.counts["ocrBranchFailures"] == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_orchestrator_timeout_cancels_both_before_workspace_cleanup(tmp_path: Path) -> None:
    audio, frames = SlowAudioExtractor(), SlowFrameExtractor()
    orchestrator = _orchestrator(audio, frames, timeout=0.05)
    with pytest.raises(MediaBranchesTimeout):
        await orchestrator.collect("video.mp4", parent=tmp_path)
    assert audio.cancelled and frames.cancelled
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_orchestrator_propagates_child_cancel_and_reaps_sibling(tmp_path: Path) -> None:
    audio, frames = SelfCancellingAudioExtractor(), SlowFrameExtractor()
    orchestrator = _orchestrator(audio, frames, timeout=5)
    with pytest.raises(asyncio.CancelledError):
        await orchestrator.collect("video.mp4", parent=tmp_path)
    assert frames.cancelled
    assert not list(tmp_path.iterdir())
