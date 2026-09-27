"""Programmatic evidence verification and Agent-loop evidence bounds.

The implementation mirrors the Java ``EvidenceVerificationService`` and the
``AgentLoopService.enforceEvidenceBounds`` guard.  It is intentionally a pure
application service: no model provider, persistence, or infrastructure
adapter is involved.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from itertools import combinations
from typing import Any

from dovideo.domain import (
    AnalysisEvidence,
    AnalysisResult,
    CriticResult,
    MAX_EVIDENCE_SOURCE_ITEM_REFS,
    SourceItemIdentity,
    VideoContext,
    VideoSegment,
    content_digest,
)


INVALID_EVIDENCE_FEEDBACK = "为每条结论重新检索并绑定可核验的时间戳证据"
EMPTY_CRITIQUE_FEEDBACK = "重新检查目标覆盖、结构完整性和证据绑定"
NULL_CRITIQUE_FEEDBACK = "Critic 未返回有效结果"


class EvidenceVerificationService:
    """Check timestamp, source, text, and claim binding without side effects."""

    def timestamp_covered(
        self,
        context: VideoContext | None,
        evidence: AnalysisEvidence | None,
    ) -> bool:
        """Return whether an evidence timestamp is in any half-open window."""

        if context is None or evidence is None:
            return False
        return any(
            segment.start_ms <= evidence.timestamp_ms < segment.end_ms
            for segment in context.segments
        )

    def supported(
        self,
        context: VideoContext | None,
        evidence: AnalysisEvidence | None,
    ) -> bool:
        """Verify source kind and normalized evidence-text containment."""

        if context is None or evidence is None:
            return False
        if _has_provenance_reference(evidence):
            return self._supported_with_provenance(context, evidence)
        content = evidence.content
        source = evidence.source
        if not isinstance(content, str) or not content.strip():
            return False
        if not isinstance(source, str):
            return False
        source_upper = source.upper()
        if "ASR" not in source_upper and "OCR" not in source_upper:
            return False

        for segment in context.segments:
            if not (
                segment.start_ms <= evidence.timestamp_ms < segment.end_ms
            ):
                continue
            candidate = _source_text(segment, source_upper)
            normalized_evidence = normalize_evidence_text(content)
            normalized_candidate = normalize_evidence_text(candidate)
            if (
                normalized_evidence
                and normalized_candidate
                and normalized_evidence in normalized_candidate
            ):
                return True
        return False

    def provenance_supported(
        self,
        context: VideoContext | None,
        evidence: AnalysisEvidence | None,
    ) -> bool:
        """Return whether an explicitly supplied provenance reference is valid."""

        if context is None or evidence is None or not _has_provenance_reference(evidence):
            return False
        return self._supported_with_provenance(context, evidence)

    def bind_provenance(
        self,
        context: VideoContext | None,
        result: AnalysisResult | None,
    ) -> AnalysisResult | None:
        """Bind only unambiguous legacy evidence to authoritative source items.

        This is an application-owned enrichment step.  Model-provided IDs are
        never accepted by this method as authority; explicitly supplied IDs are
        still checked by :meth:`supported`.
        """

        if context is None or result is None or not context.source_revision:
            return result
        enriched: list[AnalysisEvidence] = []
        changed = False
        for evidence in result.evidence:
            if _has_provenance_reference(evidence):
                enriched.append(evidence)
                continue
            candidates = _candidate_references(context, evidence)
            if len(candidates) != 1:
                enriched.append(evidence)
                continue
            segment, item_ids = candidates[0]
            enriched.append(
                evidence.model_copy(
                    update={
                        "source_revision": context.source_revision,
                        "segment_id": segment.segment_id,
                        "source_item_id": item_ids[0],
                        "source_item_ids": item_ids,
                        "source_provenance_version": context.provenance_version,
                    }
                )
            )
            changed = True
        if not changed:
            return result
        return result.model_copy(update={"evidence": tuple(enriched)})

    def _supported_with_provenance(
        self,
        context: VideoContext,
        evidence: AnalysisEvidence,
    ) -> bool:
        resolved = _resolve_provenance_items(context, evidence)
        if resolved is None:
            return False
        segment, items = resolved
        source_texts: list[str] = []
        for item in items:
            source_text = _source_item_text(segment, item)
            if source_text is None:
                return False
            source_texts.append(source_text)
        normalized_evidence = normalize_evidence_text(evidence.content)
        normalized_source = normalize_evidence_text(" ".join(source_texts))
        return bool(
            normalized_evidence
            and normalized_source
            and normalized_evidence in normalized_source
        )

    def supports_claim(
        self,
        context: VideoContext | None,
        claim: str | None,
        evidence: AnalysisEvidence | None,
    ) -> bool:
        """Verify normalized claim equality plus full evidence support."""

        if evidence is None:
            return False
        normalized_claim = normalize_evidence_text(claim)
        if not normalized_claim:
            return False
        return (
            normalized_claim == normalize_evidence_text(evidence.claim)
            and self.supported(context, evidence)
        )

    def enforce_evidence_bounds(
        self,
        context: VideoContext | None,
        result: AnalysisResult | None,
        critique: CriticResult | Mapping[str, Any] | None = None,
    ) -> CriticResult:
        """Normalize Critic output and append deterministic evidence repairs.

        This method intentionally does not enforce structure bounds; those
        belong to the later Agent-loop phase.  Every returned collection is a
        tuple owned by the frozen domain model.
        """

        normalized_critique = _normalize_critique(critique)
        has_declared_problems = bool(
            normalized_critique.feedback
            or normalized_critique.missing_requirements
            or normalized_critique.unsupported_claims
            or normalized_critique.required_timestamps
        )
        if normalized_critique.passed and has_declared_problems:
            normalized_critique = CriticResult(
                passed=False,
                feedback=normalized_critique.feedback,
                missing_requirements=normalized_critique.missing_requirements,
                unsupported_claims=normalized_critique.unsupported_claims,
                required_timestamps=normalized_critique.required_timestamps,
            )

        if (
            not normalized_critique.passed
            and not normalized_critique.feedback
            and not normalized_critique.missing_requirements
            and not normalized_critique.unsupported_claims
            and not normalized_critique.required_timestamps
        ):
            normalized_critique = CriticResult(
                passed=False,
                feedback=(EMPTY_CRITIQUE_FEEDBACK,),
                missing_requirements=(),
                unsupported_claims=(),
                required_timestamps=(),
            )

        if result is None or not result.evidence:
            return normalized_critique

        authoritative_items = bool(
            context
            and context.source_revision
            and any(segment.source_items for segment in context.segments)
        )
        invalid_evidence = tuple(
            evidence
            for evidence in result.evidence
            if not self.supported(context, evidence)
            or (authoritative_items and not self.provenance_supported(context, evidence))
        )
        unsupported_claims = tuple(
            claim
            for claim in result.conclusions
            if not any(
                self.supports_claim(context, claim, evidence)
                for evidence in result.evidence
            )
        )
        if not invalid_evidence and not unsupported_claims:
            return normalized_critique

        unsupported = list(normalized_critique.unsupported_claims)
        for claim in unsupported_claims:
            if claim not in unsupported:
                unsupported.append(claim)
        unsupported.extend(
            f"证据无法在原始 ASR/OCR 中核验: {evidence.timestamp_ms}"
            for evidence in invalid_evidence
        )

        feedback = [*normalized_critique.feedback, INVALID_EVIDENCE_FEEDBACK]
        required_timestamps = list(normalized_critique.required_timestamps)
        for evidence in invalid_evidence:
            timestamp = evidence.timestamp_ms
            if timestamp not in required_timestamps:
                required_timestamps.append(timestamp)

        return CriticResult(
            passed=False,
            feedback=tuple(feedback),
            missing_requirements=normalized_critique.missing_requirements,
            unsupported_claims=tuple(unsupported),
            required_timestamps=tuple(required_timestamps),
        )

    # Java migration spellings used by compatibility callers.
    timestampCovered = timestamp_covered
    supportsClaim = supports_claim
    enforceEvidenceBounds = enforce_evidence_bounds
    provenanceSupported = provenance_supported
    bindProvenance = bind_provenance


def _has_provenance_reference(evidence: AnalysisEvidence) -> bool:
    return bool(
        evidence.source_revision
        or evidence.segment_id
        or evidence.source_item_id
        or evidence.source_item_ids
        or evidence.source_provenance_version
    )


def _required_source_types(source: str) -> frozenset[str]:
    normalized = source.upper() if isinstance(source, str) else ""
    values = {
        source_type
        for source_type in ("ASR", "OCR")
        if source_type in normalized
    }
    return frozenset(values)


def _resolve_provenance_items(
    context: VideoContext,
    evidence: AnalysisEvidence,
) -> tuple[VideoSegment, tuple[SourceItemIdentity, ...]] | None:
    """Resolve and validate all identity, source, and timestamp constraints."""

    if not context.source_revision or evidence.source_revision != context.source_revision:
        return None
    if (
        evidence.source_provenance_version
        and context.provenance_version
        and evidence.source_provenance_version != context.provenance_version
    ):
        return None
    if not evidence.segment_id or not evidence.source_item_ids:
        return None
    if len(evidence.source_item_ids) > MAX_EVIDENCE_SOURCE_ITEM_REFS:
        return None

    segment = next(
        (
            candidate
            for candidate in context.segments
            if candidate.segment_id == evidence.segment_id
            and candidate.source_revision == context.source_revision
        ),
        None,
    )
    if segment is None:
        return None

    by_id = {item.source_item_id: item for item in segment.source_items}
    if len(by_id) != len(segment.source_items):
        return None
    try:
        items = tuple(by_id[item_id] for item_id in evidence.source_item_ids)
    except KeyError:
        return None
    if any(
        item.source_revision != context.source_revision
        or item.segment_id != segment.segment_id
        for item in items
    ):
        return None

    required_types = _required_source_types(evidence.source)
    actual_types = frozenset(item.source_type for item in items)
    if not required_types or actual_types != required_types:
        return None
    if not (
        segment.start_ms <= evidence.timestamp_ms < segment.end_ms
    ):
        return None
    if not all(_item_covers_timestamp(item, evidence.timestamp_ms) for item in items):
        return None
    return segment, items


def _item_covers_timestamp(item: SourceItemIdentity, timestamp_ms: int) -> bool:
    if item.source_type == "OCR":
        return item.timestamp_ms == timestamp_ms
    return item.timestamp_ms <= timestamp_ms < (item.end_ms or item.timestamp_ms)


def _source_item_text(
    segment: VideoSegment,
    item: SourceItemIdentity,
) -> str | None:
    """Recover the source text by digest without copying it into the ID."""

    if item.source_type == "ASR":
        candidates = tuple(segment.transcript.split("\n"))
    elif item.source_type == "OCR":
        candidates = tuple(segment.ocr_texts)
    else:
        return None
    for candidate in candidates:
        if content_digest(candidate) == item.content_digest:
            return candidate
    return None


def _candidate_references(
    context: VideoContext,
    evidence: AnalysisEvidence,
) -> list[tuple[VideoSegment, tuple[str, ...]]]:
    required_types = _required_source_types(evidence.source)
    if not required_types or not evidence.content:
        return []
    normalized_content = normalize_evidence_text(evidence.content)
    candidates: list[tuple[VideoSegment, tuple[str, ...]]] = []
    for segment in context.segments:
        if not (
            segment.start_ms <= evidence.timestamp_ms < segment.end_ms
            and segment.source_revision == context.source_revision
            and segment.segment_id
        ):
            continue
        typed: dict[str, list[SourceItemIdentity]] = {"ASR": [], "OCR": []}
        for item in segment.source_items:
            if item.source_type not in required_types:
                continue
            if not _item_covers_timestamp(item, evidence.timestamp_ms):
                continue
            source_text = _source_item_text(segment, item)
            if source_text is None:
                continue
            typed[item.source_type].append(item)

        groups: Iterable[tuple[SourceItemIdentity, ...]]
        if required_types == frozenset({"ASR", "OCR"}):
            groups = (
                (asr, ocr)
                for asr in typed["ASR"]
                for ocr in typed["OCR"]
            )
        else:
            only_type = next(iter(required_types))
            groups = ((item,) for item in typed[only_type])

        for group in groups:
            if len(group) > MAX_EVIDENCE_SOURCE_ITEM_REFS:
                continue
            text = " ".join(
                value
                for item in group
                for value in (_source_item_text(segment, item),)
                if value is not None
            )
            if normalized_content and normalized_content in normalize_evidence_text(text):
                item_ids = tuple(item.source_item_id for item in group)
                candidate = (segment, item_ids)
                if candidate not in candidates:
                    candidates.append(candidate)
    return candidates


def normalize_evidence_text(value: str | None) -> str:
    """Lowercase and remove Unicode punctuation, symbols, and whitespace."""

    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    lowered = value.lower()
    return "".join(
        character
        for character in lowered
        if not character.isspace()
        and not unicodedata.category(character).startswith(("P", "S"))
    )


def _source_text(segment: VideoSegment, source: str) -> str:
    has_asr = "ASR" in source
    has_ocr = "OCR" in source
    if has_asr and has_ocr:
        return segment.transcript + " " + " ".join(segment.ocr_texts)
    if has_asr:
        return segment.transcript
    return " ".join(segment.ocr_texts)


def _normalize_critique(
    critique: CriticResult | Mapping[str, Any] | None,
) -> CriticResult:
    if critique is None:
        return CriticResult(
            passed=False,
            feedback=(NULL_CRITIQUE_FEEDBACK,),
            missing_requirements=(),
            unsupported_claims=(),
            required_timestamps=(),
        )
    if isinstance(critique, Mapping):
        critique = CriticResult.model_validate(critique)
    if not isinstance(critique, CriticResult):
        raise TypeError("critique must be a CriticResult, mapping, or None")
    return CriticResult(
        passed=critique.passed,
        feedback=tuple(critique.feedback),
        missing_requirements=tuple(critique.missing_requirements),
        unsupported_claims=tuple(critique.unsupported_claims),
        required_timestamps=tuple(critique.required_timestamps),
    )


def enforce_evidence_bounds(
    context: VideoContext | None,
    result: AnalysisResult | None,
    critique: CriticResult | Mapping[str, Any] | None = None,
) -> CriticResult:
    """Convenience function using a stateless verifier instance."""

    return EvidenceVerificationService().enforce_evidence_bounds(
        context, result, critique
    )


def bind_evidence_provenance(
    context: VideoContext | None,
    result: AnalysisResult | None,
) -> AnalysisResult | None:
    """Convenience wrapper for application-owned provenance enrichment."""

    return EvidenceVerificationService().bind_provenance(context, result)


# Compact migration aliases for callers that use the Java method names.  These
# are real wrappers rather than unbound method aliases so module-level calls
# retain the same two/three-argument shape as the Java service methods.
normalize = normalize_evidence_text


def timestamp_covered(
    context: VideoContext | None,
    evidence: AnalysisEvidence | None,
) -> bool:
    return EvidenceVerificationService().timestamp_covered(context, evidence)


def supported(
    context: VideoContext | None,
    evidence: AnalysisEvidence | None,
) -> bool:
    return EvidenceVerificationService().supported(context, evidence)


def supports_claim(
    context: VideoContext | None,
    claim: str | None,
    evidence: AnalysisEvidence | None,
) -> bool:
    return EvidenceVerificationService().supports_claim(context, claim, evidence)


__all__ = [
    "EMPTY_CRITIQUE_FEEDBACK",
    "EvidenceVerificationService",
    "INVALID_EVIDENCE_FEEDBACK",
    "NULL_CRITIQUE_FEEDBACK",
    "bind_evidence_provenance",
    "enforce_evidence_bounds",
    "normalize_evidence_text",
    "normalize",
    "supported",
    "supports_claim",
    "timestamp_covered",
]
