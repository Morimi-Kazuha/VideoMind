"""Shared normalized documents, technical tokens and stable segment identity."""

from __future__ import annotations

import re
import unicodedata

from dovideo.domain import VideoChunk, VideoSegment
from dovideo.domain.provenance import sha256_canonical

_TOKENS = re.compile(r"[\u3400-\u9fff]+|[a-z0-9_]+(?:(?:::|[.+/#:-])[a-z0-9_]+)*(?:\+\+|#)?|[+#]")
_SENTENCES = re.compile(r"[\r\n]+|(?<=[。！？])|(?<=[.!?;；])\s+")


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def tokenize(text: str) -> tuple[str, ...]:
    """Chinese unigrams + bigrams; Latin/numeric/identifier compounds.

    Keep exact technical compounds (flashattention-2, b+tree, qkv, foo_bar)
    and their components. Query repeats are collapsed by the BM25 scorer.
    """
    result = []
    for match in _TOKENS.finditer(normalize_text(text)):
        token = match.group()
        if "\u3400" <= token[0] <= "\u9fff":
            result.extend(token)
            result.extend(token[i:i + 2] for i in range(len(token) - 1))
        else:
            result.append(token)
            parts = re.split(r"[.+/#:-]", token)
            if len(parts) > 1:
                result.extend(part for part in parts if part)
    return tuple(result)


def retrieval_document(chunk: VideoChunk) -> str:
    """Distinct normalized sentences from summary, ASR, OCR and keywords.

    Repeated source observations across channels do not inflate TF. Keywords
    already present as tokens are omitted. Repetitions inside a sentence remain
    meaningful TF; this does not globally turn the document into a token set.
    """
    sentences = []
    seen = set()
    fields = [chunk.segment_summary]
    for segment in chunk.raw_segments:
        fields.extend((segment.transcript, *segment.ocr_texts))
    for field in fields:
        for sentence in _SENTENCES.split(field):
            value = normalize_text(sentence).strip(" .!?。！？;；")
            if value and value not in seen:
                seen.add(value)
                sentences.append(value)
    present = set(tokenize("\n".join(sentences)))
    for keyword in chunk.keywords:
        value = normalize_text(keyword)
        tokens = set(tokenize(value))
        if tokens and not tokens <= present and value not in seen:
            seen.add(value)
            sentences.append(value)
            present.update(tokens)
    return "\n".join(sentences)


def segment_identity(segment: VideoSegment) -> str:
    if segment.segment_id:
        return "segment:" + segment.segment_id
    return "legacy:" + sha256_canonical({
        "revision": segment.source_revision, "start": segment.start_ms,
        "end": segment.end_ms, "transcript": segment.transcript,
        "ocr": list(segment.ocr_texts), "frames": list(segment.evidence_frames),
    })
