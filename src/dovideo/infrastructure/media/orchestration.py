"""Concurrent ASR/OCR branch observation orchestration."""

from __future__ import annotations

import asyncio
from os import PathLike
from typing import Any

from dovideo.application import (
    AsrBranchOutcome,
    BranchStatus,
    MediaObservationBundle,
    OcrBranchOutcome,
)
from dovideo.application.ports.observability import TelemetryPort

from .asr import SegmentedTranscriptionService
from .audio import AudioSegmenter
from .errors import BothMediaBranchesFailed, MediaBranchesTimeout
from .keyframes import KeyframeExtractor
from .ocr import OcrBatchService
from .telemetry import NullTelemetry
from .workspace import MediaWorkspace

TOTAL_BRANCH_TIMEOUT_SECONDS = 60 * 60


class MediaBranchOrchestrator:
    """Run audio+ASR and frames+OCR concurrently inside one workspace."""

    def __init__(
        self,
        audio_segmenter: AudioSegmenter,
        transcription: SegmentedTranscriptionService,
        keyframe_extractor: KeyframeExtractor,
        ocr: OcrBatchService,
        telemetry: TelemetryPort | None = None,
        *,
        total_timeout_seconds: float = TOTAL_BRANCH_TIMEOUT_SECONDS,
    ) -> None:
        if total_timeout_seconds <= 0:
            raise ValueError("total_timeout_seconds must be positive")
        self._audio_segmenter = audio_segmenter
        self._transcription = transcription
        self._keyframe_extractor = keyframe_extractor
        self._ocr = ocr
        self._telemetry = telemetry or NullTelemetry()
        self._total_timeout_seconds = total_timeout_seconds

    async def collect(
        self,
        source: str,
        *,
        parent: str | PathLike[str] | None = None,
        trace_id: str | None = None,
    ) -> MediaObservationBundle:
        """Return branch observations; cleanup completes before this returns."""

        async with MediaWorkspace(parent=parent) as workspace:
            return await self.collect_in_workspace(
                source,
                workspace,
                trace_id=trace_id,
            )

    async def collect_in_workspace(
        self,
        source: str,
        workspace: MediaWorkspace,
        *,
        trace_id: str | None = None,
    ) -> MediaObservationBundle:
        """Run against a caller-owned active scope (which remains their duty)."""

        # Touching ``path`` validates that a caller did not pass a closed scope
        # before any task can create files.
        workspace.path
        tasks = (
            asyncio.create_task(self._run_asr_branch(source, workspace, trace_id)),
            asyncio.create_task(self._run_ocr_branch(source, workspace, trace_id)),
        )
        try:
            results = await asyncio.wait_for(
                _wait_for_branches(tasks),
                timeout=self._total_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            await _cancel_and_wait(tasks)
            raise MediaBranchesTimeout(self._total_timeout_seconds) from exc
        except asyncio.CancelledError:
            await _cancel_and_wait(tasks)
            raise

        # The branch collector represents a self-cancelled child as a
        # CancelledError control-flow signal, not a business branch failure.
        # It has already cancelled and awaited siblings; this defensive wait
        # also makes the cleanup contract explicit at this boundary.
        if any(isinstance(result, asyncio.CancelledError) for result in results):
            await _cancel_and_wait(tasks)
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    raise result

        asr = _asr_result(results[0])
        ocr = _ocr_result(results[1])
        if asr.status is BranchStatus.FAILED:
            self._telemetry.increment("asrBranchFailures")
        if ocr.status is BranchStatus.FAILED:
            self._telemetry.increment("ocrBranchFailures")
        if asr.status is BranchStatus.FAILED and ocr.status is BranchStatus.FAILED:
            failure = BothMediaBranchesFailed(asr, ocr)
            if failure.causes:
                raise failure from failure.causes[0]
            raise failure
        return MediaObservationBundle(asr=asr, ocr=ocr)

    observe = collect

    async def _run_asr_branch(
        self,
        source: str,
        workspace: MediaWorkspace,
        trace_id: str | None,
    ) -> AsrBranchOutcome:
        segments = await self._audio_segmenter.segment(source, workspace)
        return await self._transcription.transcribe(segments, trace_id=trace_id)

    async def _run_ocr_branch(
        self,
        source: str,
        workspace: MediaWorkspace,
        trace_id: str | None,
    ) -> OcrBranchOutcome:
        try:
            frames = await self._keyframe_extractor.extract(source, workspace)
        except Exception as exc:
            recorder = getattr(self._telemetry, "record_media_failure", None)
            if callable(recorder):
                try:
                    recorder(
                        media_branch="OCR",
                        failure_stage="KEYFRAME_EXTRACTION",
                        error=exc,
                    )
                except Exception:
                    # A diagnostic sink must not replace the original failure.
                    pass
            raise
        return await self._ocr.process(source, frames, trace_id=trace_id)


async def _cancel_and_wait(tasks: tuple[asyncio.Task[Any], ...]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _wait_for_branches(tasks: tuple[asyncio.Task[Any], ...]) -> list[object]:
    """Collect both branches while treating cancellation as control flow.

    Ordinary ``Exception`` values are retained so the healthy branch can
    continue.  A child ``CancelledError`` or another ``BaseException`` is
    different: cancel and await all siblings before propagating it, so no
    task can outlive the workspace that owns its artifacts.
    """

    pending: set[asyncio.Task[Any]] = set(tasks)
    indexes = {task: index for index, task in enumerate(tasks)}
    results: list[object] = [None] * len(tasks)
    while pending:
        done, pending = await asyncio.wait(
            pending,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            index = indexes[task]
            try:
                results[index] = task.result()
            except asyncio.CancelledError:
                await _cancel_and_wait(tuple(pending))
                raise
            except Exception as exc:
                results[index] = exc
            except BaseException:
                await _cancel_and_wait(tuple(pending))
                raise
    return results


def _asr_result(result: object) -> AsrBranchOutcome:
    if isinstance(result, AsrBranchOutcome):
        return result
    if isinstance(result, asyncio.CancelledError):
        raise result
    if isinstance(result, Exception):
        causes = tuple(getattr(result, "causes", ()))
        branch_error = getattr(result, "branch_error", None)
        if not causes and branch_error is None:
            branch_error = result
        return AsrBranchOutcome(
            attempted=getattr(result, "attempted", 0),
            failed=len(causes),
            causes=causes,
            branch_error=branch_error,
        )
    if isinstance(result, BaseException):
        raise result
    raise TypeError("ASR branch returned an unexpected result")


def _ocr_result(result: object) -> OcrBranchOutcome:
    if isinstance(result, OcrBranchOutcome):
        return result
    if isinstance(result, asyncio.CancelledError):
        raise result
    if isinstance(result, Exception):
        causes = tuple(getattr(result, "causes", ()))
        branch_error = getattr(result, "branch_error", None)
        if not causes and branch_error is None:
            branch_error = result
        return OcrBranchOutcome(
            attempted=getattr(result, "attempted", 0),
            skipped_duplicates=getattr(result, "skipped_duplicates", 0),
            failed=len(causes),
            causes=causes,
            branch_error=branch_error,
        )
    if isinstance(result, BaseException):
        raise result
    raise TypeError("OCR branch returned an unexpected result")


__all__ = ["MediaBranchOrchestrator", "TOTAL_BRANCH_TIMEOUT_SECONDS"]
