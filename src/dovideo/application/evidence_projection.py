"""Exact shared answer/reflection source projection; no second clipping policy."""
from collections.abc import Sequence
from typing import Any
import re
import unicodedata
from dovideo.domain import VideoEvidenceHit, TemporalObservation
from .evidence import normalize_evidence_text
MAX_FOLLOW_UP_SOURCE_CHARS = 400
MAX_FOLLOW_UP_OCR_EXCERPTS = 2
MAX_FOLLOW_UP_OBSERVATIONS = 16

_LITERAL = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9_]+(?:\([A-Za-z0-9_]+\))?(?:[=+*/^−-][A-Za-z0-9_()]+)+")


def normalize_quote_text(text):
    """Keep mathematical operators when comparing a quote's declared channel."""
    operators = frozenset("=+−-*/^<>%×÷≤≥≠±")
    return "".join(c for c in text.lower() if not c.isspace()
        and (c in operators or not unicodedata.category(c).startswith(("P", "S"))))


def bounded_source_hit(hit: VideoEvidenceHit, question: str = "") -> VideoEvidenceHit:
    """Select a contiguous source window around explicit technical literals.

    No generated text, extra channels or larger cap. Unnamed requirements keep
    the prefix behavior; selection cannot prove semantic completeness.
    """
    terms = tuple(dict.fromkeys(_LITERAL.findall(question[:500])))
    def excerpt(text):
        text = text.strip()
        cap = MAX_FOLLOW_UP_SOURCE_CHARS
        start = 0
        windows = {0}
        if len(text) > cap:
            for term in terms:
                for match in re.finditer(re.escape(term), text, re.IGNORECASE):
                    windows.add(max(0, min(len(text) - cap, match.start() - max(0, (cap - len(term)) // 2))))
            windows = sorted(windows)[:128]
            start = max(windows, key=lambda offset: (
                sum(len(term) for term in terms if re.search(re.escape(term), text[offset:offset + cap], re.IGNORECASE)),
                -offset,
            ))
        return "".join(c for c in text[start:start + cap] if c.isprintable() or c in "\n\t").strip()
    return hit.model_copy(update={"snippet": excerpt(hit.snippet), "transcript": excerpt(hit.transcript),
        "ocr_texts": tuple(excerpt(text) for text in hit.ocr_texts[:MAX_FOLLOW_UP_OCR_EXCERPTS]
                           if isinstance(text, str) and text.strip())})

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
    # A full ASR observation budget must not hide exact times for visible OCR.
    # This maps the existing excerpts, without duplicating text or guessing.
    from dovideo.domain.provenance import content_digest
    ocr_times = []
    for excerpt_index, excerpt in enumerate(payload["ocrExcerpts"]):
        timestamp = None
        for observation in observations:
            item = observation.source_item
            if (item.source_type == "OCR" and item.source_item_id in candidate_ids
                    and item.source_revision == hit.source_revision and item.segment_id == hit.segment_id
                    and hit.start_ms <= item.timestamp_ms < hit.end_ms
                    and item.content_digest == content_digest(observation.text)
                    and normalize_quote_text(excerpt) in normalize_quote_text(observation.text)):
                timestamp = item.timestamp_ms
                break
        ocr_times.append(timestamp)
    if any(timestamp is not None for timestamp in ocr_times):
        payload["ocrObservationTimestamps"] = ocr_times
    return payload
