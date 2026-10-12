"""Exact shared answer/reflection source projection; no second clipping policy."""
from collections.abc import Sequence
from typing import Any
from dovideo.domain import VideoEvidenceHit, TemporalObservation
from .evidence import normalize_evidence_text
MAX_FOLLOW_UP_SOURCE_CHARS = 400
MAX_FOLLOW_UP_OCR_EXCERPTS = 2
MAX_FOLLOW_UP_OBSERVATIONS = 16

def source_payload(
    index: int,
    hit: VideoEvidenceHit,
    observations: Sequence[TemporalObservation] = (),
) -> dict[str, Any]:
    if not isinstance(hit, VideoEvidenceHit):
        raise TypeError("invalid evidence hit")
    payload = {
        "candidateIndex": index,
        "startMs": hit.start_ms,
        "endMs": hit.end_ms,
        "source": hit.source[:16],
        "asrExcerpt": hit.transcript[:MAX_FOLLOW_UP_SOURCE_CHARS],
        "ocrExcerpts": tuple(
            value[:MAX_FOLLOW_UP_SOURCE_CHARS]
            for value in hit.ocr_texts[:MAX_FOLLOW_UP_OCR_EXCERPTS]
        ),
    }
    source_observations = []
    remaining_chars = MAX_FOLLOW_UP_SOURCE_CHARS
    candidate_ids = frozenset(hit.source_item_ids)
    for observation in observations:
        item = observation.source_item
        if (
            item.source_item_id not in candidate_ids
            or item.source_revision != hit.source_revision
            or item.segment_id != hit.segment_id
        ):
            continue
        source_text = payload["asrExcerpt"] if item.source_type == "ASR" else " ".join(payload["ocrExcerpts"])
        text = observation.text[:remaining_chars]
        if not text or normalize_evidence_text(text) not in normalize_evidence_text(source_text):
            continue
        if item.source_type == "OCR":
            if not hit.start_ms <= item.timestamp_ms < hit.end_ms:
                continue
        elif max(hit.start_ms, item.timestamp_ms) >= min(hit.end_ms, item.end_ms or item.timestamp_ms):
            continue
        source_observations.append({
            "source": item.source_type,
            "timestampMs": item.timestamp_ms,
            "endMs": item.end_ms,
            "excerpt": text,
        })
        remaining_chars -= len(text)
        if not remaining_chars or len(source_observations) >= MAX_FOLLOW_UP_OBSERVATIONS:
            break
    if source_observations:
        payload["sourceObservations"] = tuple(source_observations)
    return payload
