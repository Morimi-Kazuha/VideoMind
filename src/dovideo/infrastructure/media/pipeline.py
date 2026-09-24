"""Composed, scoped media preprocessing for Phase 3A."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from os import PathLike

from .audio import AudioSegment, AudioSegmenter
from .ffprobe import FfprobeDurationAdapter, MediaDuration
from .keyframes import Keyframe, KeyframeExtractor
from .workspace import MediaWorkspace


@dataclass(frozen=True, slots=True)
class PreprocessedMedia:
    """All local artifacts produced for one scoped preprocessing operation."""

    duration: MediaDuration
    audio_segments: tuple[AudioSegment, ...]
    keyframes: tuple[Keyframe, ...]

    @property
    def segments(self) -> tuple[AudioSegment, ...]:
        """Short alias for audio artifacts."""

        return self.audio_segments


class MediaPreprocessor:
    """Compose ffprobe, audio segmentation, and keyframe extraction.

    The returned value is yielded from an async context manager.  Artifact path
    properties are intentionally unusable after the context exits because the
    workspace has then been deleted.
    """

    def __init__(
        self,
        duration_probe: FfprobeDurationAdapter,
        audio_segmenter: AudioSegmenter,
        keyframe_extractor: KeyframeExtractor,
    ) -> None:
        self._duration_probe = duration_probe
        self._audio_segmenter = audio_segmenter
        self._keyframe_extractor = keyframe_extractor

    @asynccontextmanager
    async def preprocess(
        self,
        source: str | PathLike[str],
        *,
        parent: str | PathLike[str] | None = None,
    ) -> AsyncIterator[PreprocessedMedia]:
        """Yield a frozen artifact description and clean it on every exit."""

        async with MediaWorkspace(parent=parent) as workspace:
            duration = await self._duration_probe.probe(source)
            audio_segments = await self._audio_segmenter.segment(source, workspace)
            keyframes = await self._keyframe_extractor.extract(source, workspace)
            yield PreprocessedMedia(duration, audio_segments, keyframes)

    prepare = preprocess


__all__ = ["MediaPreprocessor", "PreprocessedMedia"]
