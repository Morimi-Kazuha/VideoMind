"""Pure Phase 4 construction of a unified :class:`VideoContext`.

The Java ``VideoContextService.finishContext`` method receives the completed
ASR and OCR branches and folds their observations into fixed 60-second
windows.  This module keeps that merge policy in the application layer: it
depends only on domain models and the provider-neutral observation values,
not on FFmpeg, storage, HTTP, or a persistence implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dovideo.domain import (
    PROVENANCE_VERSION,
    SourceItemIdentity,
    SourceType,
    VideoContext,
    VideoSegment,
    compute_source_revision,
    content_digest,
    canonical_frame_ref,
    segment_id_for,
    source_item_id_for,
)

from .errors import (
    BothBranchesFailed,
    BothMediaBranchesFailed,
    BothObservationBranchesFailed,
    EmptyVideoContextError,
    NoUsableVideoEvidence,
    VideoContextBuildError,
)
from .value_objects import BranchStatus, MediaObservationBundle

SEGMENT_WINDOW_MS = 60_000


@dataclass(slots=True)
class _WindowBuilder:
    """Mutable implementation detail used only during one pure build call."""

    start_ms: int
    transcripts: list[str] = field(default_factory=list)
    ocr_texts: list[str] = field(default_factory=list)
    evidence_frames: list[str] = field(default_factory=list)
    asr_observations: list[tuple[int, Any]] = field(default_factory=list)
    ocr_observations: list[tuple[int, Any]] = field(default_factory=list)

    def build(self, source_revision: str, ordinal: int) -> VideoSegment:
        segment_id = segment_id_for(
            source_revision,
            self.start_ms,
            self.start_ms + SEGMENT_WINDOW_MS,
            ordinal,
        )
        source_items: list[SourceItemIdentity] = []
        for source_ordinal, observation in self.asr_observations:
            source_items.append(
                SourceItemIdentity(
                    source_item_id=source_item_id_for(
                        source_revision,
                        segment_id,
                        SourceType.ASR.value,
                        source_ordinal,
                        observation.start_ms,
                        observation.end_ms,
                        observation.text,
                    ),
                    source_revision=source_revision,
                    segment_id=segment_id,
                    source_type=SourceType.ASR.value,
                    ordinal=source_ordinal,
                    timestamp_ms=observation.start_ms,
                    end_ms=observation.end_ms,
                    content_digest=content_digest(observation.text),
                    provenance_version=PROVENANCE_VERSION,
                )
            )
        for source_ordinal, observation in self.ocr_observations:
            frame_ref = observation.frame_ref
            source_items.append(
                SourceItemIdentity(
                    source_item_id=source_item_id_for(
                        source_revision,
                        segment_id,
                        SourceType.OCR.value,
                        source_ordinal,
                        observation.timestamp_ms,
                        None,
                        observation.text,
                        frame_ref,
                    ),
                    source_revision=source_revision,
                    segment_id=segment_id,
                    source_type=SourceType.OCR.value,
                    ordinal=source_ordinal,
                    timestamp_ms=observation.timestamp_ms,
                    end_ms=None,
                    content_digest=content_digest(observation.text),
                    frame_ref_digest=(
                        content_digest(canonical_frame_ref(frame_ref))
                        if frame_ref and frame_ref.strip()
                        else ""
                    ),
                    provenance_version=PROVENANCE_VERSION,
                )
            )
        return VideoSegment(
            start_ms=self.start_ms,
            end_ms=self.start_ms + SEGMENT_WINDOW_MS,
            transcript="\n".join(self.transcripts),
            ocr_texts=tuple(self.ocr_texts),
            evidence_frames=tuple(self.evidence_frames),
            source_revision=source_revision,
            segment_id=segment_id,
            source_items=tuple(source_items),
            provenance_version=PROVENANCE_VERSION,
        )


@dataclass(frozen=True, slots=True)
class VideoContextBuilder:
    """Build one immutable domain context from a completed observation bundle.

    ``window_ms`` is fixed to Java's 60,000 millisecond segment size.  The
    field is exposed only as a read-only constant-like attribute to make the
    policy explicit in type-aware callers; changing the bucketing policy is a
    separate future contract, not a configurable production behavior here.
    """

    window_ms: int = SEGMENT_WINDOW_MS

    def __post_init__(self) -> None:
        if self.window_ms != SEGMENT_WINDOW_MS:
            raise ValueError("Phase 4 uses a fixed 60,000 millisecond window")

    def build(
        self,
        source: str,
        user_goal: str | None,
        observations: MediaObservationBundle,
        *,
        media_content_identity: str | None = None,
    ) -> VideoContext:
        """Merge ASR/OCR observations using Java's timestamp/window rules."""

        if not isinstance(observations, MediaObservationBundle):
            raise TypeError("observations must be a MediaObservationBundle")
        if (
            observations.asr.status is BranchStatus.FAILED
            and observations.ocr.status is BranchStatus.FAILED
        ):
            raise BothObservationBranchesFailed(
                observations.asr.all_causes,
                observations.ocr.all_causes,
            )

        windows: dict[int, _WindowBuilder] = {}

        authoritative_ocr = tuple(
            observation
            for observation in observations.ocr.observations
            if observation.text.strip()
            or (
                observation.frame_ref is not None
                and bool(observation.frame_ref.strip())
            )
        )
        media_identity = media_content_identity or source
        source_revision = compute_source_revision(
            media_identity,
            observations.asr.observations,
            authoritative_ocr,
        )

        # The branch services already return observations in source order.
        # Appending rather than sorting inside a window preserves Java's
        # input-order joining and duplicate behavior exactly.
        for source_ordinal, transcript in enumerate(observations.asr.observations):
            start_ms = _window_start(transcript.start_ms)
            window = windows.setdefault(start_ms, _WindowBuilder(start_ms))
            # Keep blank entries: Java's merge creates a window for every
            # successful TranscriptSegment and joins the complete input list
            # before VideoSegment trims the resulting string.
            window.transcripts.append(transcript.text)
            window.asr_observations.append((source_ordinal, transcript))

        for source_ordinal, observation in enumerate(authoritative_ocr):
            frame = observation.frame_ref
            text = observation.text
            has_text = bool(text.strip())
            # ``None``/blank means no evidence reference in this optional
            # application value.  Real successful Java FrameParts always have
            # a frame name; a blank OCR value with a real reference remains a
            # valid frame-only window.
            has_frame = frame is not None and bool(frame.strip())
            if not has_text and not has_frame:
                continue
            start_ms = _window_start(observation.timestamp_ms)
            window = windows.setdefault(start_ms, _WindowBuilder(start_ms))
            if has_text:
                # The Java merge filters blank OCR strings but otherwise keeps
                # their source spelling and order.
                window.ocr_texts.append(text)
            if has_frame:
                # Every successful frame reference is retained, including a
                # reference whose OCR text is empty.
                window.evidence_frames.append(frame)
            window.ocr_observations.append((source_ordinal, observation))

        segments = tuple(
            window.build(source_revision, ordinal)
            for ordinal, (_start_ms, window) in enumerate(sorted(windows.items()))
        )
        if not segments:
            raise EmptyVideoContextError(
                "video contains no usable speech or visual evidence"
            )
        return VideoContext(
            source=source,
            user_goal=user_goal,
            segments=segments,
            source_revision=source_revision,
            provenance_version=PROVENANCE_VERSION,
        )


def build_video_context(
    source: str,
    user_goal: str | None,
    observations: MediaObservationBundle,
    *,
    media_content_identity: str | None = None,
) -> VideoContext:
    """Functional convenience wrapper around :class:`VideoContextBuilder`."""

    return VideoContextBuilder().build(
        source,
        user_goal,
        observations,
        media_content_identity=media_content_identity,
    )


def _window_start(timestamp_ms: int) -> int:
    return timestamp_ms // SEGMENT_WINDOW_MS * SEGMENT_WINDOW_MS


__all__ = [
    "BothBranchesFailed",
    "BothMediaBranchesFailed",
    "BothObservationBranchesFailed",
    "EmptyVideoContextError",
    "NoUsableVideoEvidence",
    "SEGMENT_WINDOW_MS",
    "VideoContextBuildError",
    "VideoContextBuilder",
    "build_video_context",
]
