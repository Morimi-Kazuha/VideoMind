"""Read-only projections of persisted video windows and verified answer evidence."""

from __future__ import annotations

from dovideo.domain.agent import AgentState
from dovideo.domain.video import VideoContext

from .evidence import EvidenceVerificationService, bind_evidence_provenance


def temporal_observation_page(
    context: VideoContext | None, *, limit: int, offset: int
) -> dict[str, object]:
    """Page original observations from the current media context revision."""
    observations = () if context is None else context.observations
    ordered = sorted(
        observations,
        key=lambda observation: (
            observation.source_item.timestamp_ms,
            observation.source_item.source_type,
            observation.source_item.ordinal,
            observation.source_item.source_item_id,
        ),
    )
    return {
        "available": bool(ordered),
        "granularity": "source-observation",
        "sourceRevision": context.source_revision if context is not None else "",
        "total": len(ordered),
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": observation.source_item.source_item_id,
                "segmentId": observation.source_item.segment_id,
                "sourceRevision": observation.source_item.source_revision,
                "kind": observation.source_item.source_type,
                "ordinal": observation.source_item.ordinal,
                "startMs": observation.source_item.timestamp_ms,
                "endMs": observation.source_item.end_ms,
                "text": observation.text,
                "frameId": observation.source_item.frame_ref_digest or None,
            }
            for observation in ordered[offset : offset + limit]
        ],
    }


def temporal_window_page(
    context: VideoContext | None, *, limit: int, offset: int
) -> dict[str, object]:
    """Expose the complete persisted 60-second windows without inventing spans.

    Source-item identities retain exact observation times, but the persisted
    context stores ASR/OCR *text* grouped by window.  This projection labels
    that granularity honestly and bounds the response for long videos.
    """

    segments = () if context is None else tuple(context.segments)
    ordered = sorted(segments, key=lambda segment: (segment.start_ms, segment.end_ms))
    return {
        "available": context is not None,
        "granularity": "context-window-60s",
        "total": len(ordered),
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "segmentId": segment.segment_id,
                "sourceRevision": segment.source_revision,
                "startMs": segment.start_ms,
                "endMs": segment.end_ms,
                "transcript": segment.transcript,
                "ocrTexts": list(segment.ocr_texts),
            }
            for segment in ordered[offset : offset + limit]
        ],
    }


def verified_answer_citations(
    context: VideoContext | None, state: AgentState | None
) -> list[dict[str, object]]:
    """Return only answer-level evidence bound to authoritative source items."""

    if context is None or state is None or state.result is None:
        return []
    verifier = EvidenceVerificationService()
    result = bind_evidence_provenance(context, state.result)
    conclusions = set(result.conclusions)
    citations: list[dict[str, object]] = []
    for index, evidence in enumerate(result.evidence):
        if not (
            evidence.source_revision
            and evidence.segment_id
            and evidence.source_item_ids
            and evidence.claim in conclusions
            and verifier.supported(context, evidence)
        ):
            continue
        citations.append(
            {
                "id": f"{evidence.segment_id}:{index}",
                "claim": evidence.claim,
                "source": evidence.source,
                "content": evidence.content,
                "timestampMs": evidence.timestamp_ms,
                "segmentId": evidence.segment_id,
                "sourceRevision": evidence.source_revision,
                "sourceItemIds": list(evidence.source_item_ids),
            }
        )
    return citations
