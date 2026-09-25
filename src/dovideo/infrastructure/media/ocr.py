"""Tesseract invocation and OCR batch de-duplication."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from os import PathLike
from pathlib import Path

from dovideo.application import OcrBranchOutcome, OcrObservation
from dovideo.application.ports.ai import FrameOcrPort
from dovideo.application.ports.media import EvidenceFramePort, ImageHashPort
from dovideo.application.ports.observability import TelemetryPort
from dovideo.domain.provenance import stable_frame_ref

from .errors import AllOcrFramesFailed, OcrImageMissing
from .hashing import DEFAULT_HAMMING_THRESHOLD, hamming_distance
from .keyframes import Keyframe
from .runner import AsyncSubprocessRunner

OCR_TIMEOUT_SECONDS = 2 * 60
OCR_LANGUAGE = "chi_sim+eng"


class TesseractOcrAdapter:
    """Run Tesseract with the exact Java argument order and timeout."""

    def __init__(
        self,
        runner: AsyncSubprocessRunner,
        *,
        executable: str | PathLike[str] = "tesseract",
        timeout: float = OCR_TIMEOUT_SECONDS,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._runner = runner
        self._executable = _tool_name(executable)
        self._timeout = timeout

    async def recognize_frame(
        self,
        image_path: Path,
        *,
        trace_id: str | None = None,
    ) -> str:
        del trace_id
        path = Path(image_path)
        if not path.is_file():
            raise OcrImageMissing("OCR image does not exist")
        result = await self._runner.run(
            (
                self._executable,
                os.fspath(path),
                "stdout",
                "-l",
                OCR_LANGUAGE,
            ),
            timeout=self._timeout,
        )
        return result.stdout.strip()

    recognize = recognize_frame


class OcrBatchService:
    """OCR ordered keyframes, skipping near-duplicate images safely."""

    def __init__(
        self,
        ocr: FrameOcrPort,
        image_hash: ImageHashPort,
        evidence_frames: EvidenceFramePort | None = None,
        telemetry: TelemetryPort | None = None,
        *,
        hamming_threshold: int = DEFAULT_HAMMING_THRESHOLD,
    ) -> None:
        if hamming_threshold < 0:
            raise ValueError("hamming threshold cannot be negative")
        self._ocr = ocr
        self._image_hash = image_hash
        self._evidence_frames = evidence_frames
        self._telemetry = telemetry
        self._hamming_threshold = hamming_threshold

    async def process(
        self,
        source: str,
        frames: Sequence[Keyframe],
        *,
        media_identity: str | None = None,
        trace_id: str | None = None,
    ) -> OcrBranchOutcome:
        observations: list[OcrObservation] = []
        causes: list[Exception] = []
        attempted = 0
        skipped_duplicates = 0
        previous_hash: int | None = None
        for frame in tuple(frames):
            try:
                # Pillow/image decoding is synchronous; keep it off the event
                # loop while the workspace remains open for this frame.
                current_hash = await asyncio.to_thread(
                    self._image_hash.difference_hash,
                    frame.path,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempted += 1
                causes.append(exc)
                self._increment("ocrFrameFailures")
                continue
            if previous_hash is not None and hamming_distance(previous_hash, current_hash) <= self._hamming_threshold:
                skipped_duplicates += 1
                continue
            previous_hash = current_hash
            attempted += 1
            self._increment("ocrCalls")
            try:
                text = await self._ocr.recognize_frame(frame.path, trace_id=trace_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                causes.append(exc)
                self._increment("ocrFrameFailures")
                continue

            frame_ref = stable_frame_ref(
                media_identity if media_identity is not None else source,
                frame.timestamp_ms,
                frame.index,
            )
            frame_location = None
            if self._evidence_frames is not None:
                try:
                    persisted = await self._evidence_frames.persist_frame(
                        frame.path,
                        timestamp_ms=frame.timestamp_ms,
                    )
                    if not isinstance(persisted, str) or not persisted.strip():
                        raise ValueError("evidence frame publisher returned an empty reference")
                    frame_location = persisted
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Java retains the source/timestamp fallback when MinIO
                    # cannot publish a frame; the OCR observation still counts.
                    self._increment("frameUploadFailures")
            observations.append(
                OcrObservation(
                    timestamp_ms=frame.timestamp_ms,
                    text=text.strip() if isinstance(text, str) else "",
                    frame_ref=frame_ref,
                    frame_location=frame_location,
                )
            )

        if not observations and causes:
            failure = AllOcrFramesFailed(
                causes,
                attempted=attempted,
                skipped_duplicates=skipped_duplicates,
            )
            raise failure from failure.last_cause
        return OcrBranchOutcome(
            observations=tuple(observations),
            attempted=attempted,
            failed=len(causes),
            skipped_duplicates=skipped_duplicates,
            causes=tuple(causes),
        )

    process_frames = process

    def _increment(self, metric: str) -> None:
        if self._telemetry is not None:
            self._telemetry.increment(metric)


def _tool_name(value: str | PathLike[str]) -> str:
    try:
        result = os.fspath(value)
    except TypeError as exc:
        raise TypeError("tesseract executable must be text or path-like") from exc
    if isinstance(result, bytes):
        result = os.fsdecode(result)
    if not isinstance(result, str) or not result or "\x00" in result:
        raise ValueError("tesseract executable must be non-empty text")
    return result


__all__ = [
    "OCR_LANGUAGE",
    "OCR_TIMEOUT_SECONDS",
    "OcrBatchService",
    "TesseractOcrAdapter",
]
