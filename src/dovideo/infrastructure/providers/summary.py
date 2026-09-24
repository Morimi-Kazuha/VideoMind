"""Deterministic local chunk-summary helper for offline and fallback wiring."""

from __future__ import annotations

import re
from collections.abc import Sequence

from dovideo.application.ports.ai import ChunkSummaryPort
from dovideo.domain import ChunkSummary, VideoSegment


DEFAULT_LOCAL_SUMMARY_CHARS = 200
DEFAULT_LOCAL_KEYWORDS = 16
_TERM_PATTERN = re.compile(r"[^\s,，。；;:：!?！？、|/\\]+")


class LocalChunkSummaryAdapter(ChunkSummaryPort):
    """Create a bounded summary without network or model dependencies.

    This helper is intentionally conservative: it concatenates source text
    in timestamp/order supplied by the application and extracts distinct
    whitespace/punctuation-delimited terms.  It does not invent claims and
    can therefore serve as a deterministic local provider for 10B setup.
    """

    def __init__(
        self,
        *,
        max_summary_chars: int = DEFAULT_LOCAL_SUMMARY_CHARS,
        max_keywords: int = DEFAULT_LOCAL_KEYWORDS,
    ) -> None:
        if isinstance(max_summary_chars, bool) or not isinstance(max_summary_chars, int):
            raise TypeError("max_summary_chars must be an integer")
        if max_summary_chars < 0:
            raise ValueError("max_summary_chars cannot be negative")
        if isinstance(max_keywords, bool) or not isinstance(max_keywords, int):
            raise TypeError("max_keywords must be an integer")
        if max_keywords < 0:
            raise ValueError("max_keywords cannot be negative")
        self.max_summary_chars = max_summary_chars
        self.max_keywords = max_keywords

    async def summarize_chunk(
        self,
        segments: Sequence[VideoSegment],
    ) -> ChunkSummary:
        ordered = tuple(segments)
        if not all(isinstance(segment, VideoSegment) for segment in ordered):
            raise TypeError("chunk summary segments must be VideoSegment values")
        parts: list[str] = []
        for segment in ordered:
            if segment.transcript.strip():
                parts.append(segment.transcript.strip())
            for value in segment.ocr_texts:
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
        text = " ".join(parts)[: self.max_summary_chars]
        keywords: list[str] = []
        seen: set[str] = set()
        for part in parts:
            for term in _TERM_PATTERN.findall(part):
                normalized = term.strip()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                keywords.append(normalized)
                if len(keywords) >= self.max_keywords:
                    return ChunkSummary(segment_summary=text, keywords=tuple(keywords))
        return ChunkSummary(segment_summary=text, keywords=tuple(keywords))


LocalSummaryAdapter = LocalChunkSummaryAdapter


__all__ = [
    "DEFAULT_LOCAL_KEYWORDS",
    "DEFAULT_LOCAL_SUMMARY_CHARS",
    "LocalChunkSummaryAdapter",
    "LocalSummaryAdapter",
]
