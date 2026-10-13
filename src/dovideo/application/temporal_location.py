"""Scoped literal locations over the current checkpoint, never video-wide proof."""
import re

from dovideo.domain import AnalysisEvidence, VideoContext, content_digest
from .evidence import EvidenceVerificationService
from .evidence_projection import _LITERAL, bounded_source_hit
from .execution_budget import AgentExecutionBudget


def first_location_literals(question):
    """Only explicit first-location questions; semantic targets stay unsupported."""
    if not re.search(r"第一次|首次|最早|\bfirst\b|\bearliest\b", question, re.I):
        return None
    if not re.search(r"定位|找出|找到|查找|何时|什么时候|在哪|几分|\bwhen\b|\bwhere\b|\boccurrence\b", question, re.I):
        return None
    return tuple(dict.fromkeys(_LITERAL.findall(re.sub(r"\s+", "", question))))


def first_literal_observations(context, literals):
    """Search all valid cached observations, keeping operators and letter case.

    Empty means not uniquely locatable. OCR ambiguity, unsampled frames and
    semantic equivalence prevent claims about the video's actual first event.
    """
    if not context.source_revision or not 1 <= len(literals) <= 5:
        return ()
    try:
        context = VideoContext.model_validate(context.model_dump())
    except (ValueError, TypeError):
        return ()
    segments = {s.segment_id: s for s in context.segments}
    verifier = EvidenceVerificationService()
    results = []
    for literal in literals:
        # Ignore whitespace *inside* the target without merging neighbouring
        # OCR lines into the same token at its boundaries.
        pattern = re.compile(r"(?<![A-Za-z0-9_])" + r"\s*".join(re.escape(c) for c in literal)
            + r"(?![A-Za-z0-9_]|[ \t]*[+*/^−-])")
        matches = []
        for ordinal, observation in enumerate(context.observations):
            if ordinal % 64 == 0:
                AgentExecutionBudget.check("Cached literal location")
            item = observation.source_item
            segment = segments.get(item.segment_id)
            if (segment is None or segment.source_revision != context.source_revision
                    or item.source_revision != context.source_revision
                    or item not in segment.source_items
                    or item.content_digest != content_digest(observation.text)
                    or not pattern.search(observation.text)):
                continue
            # The original guard also checks authoritative segment text/digest.
            evidence = AnalysisEvidence(timestamp_ms=item.timestamp_ms, source=item.source_type,
                content=observation.text, claim=literal, source_revision=context.source_revision,
                segment_id=item.segment_id, source_item_ids=(item.source_item_id,),
                source_provenance_version=context.provenance_version)
            if verifier.supported(context, evidence) and verifier.timestamp_covered(context, evidence):
                matches.append(observation)
        if not matches:
            return ()
        first_time = min(o.source_item.timestamp_ms for o in matches)
        first = [o for o in matches if o.source_item.timestamp_ms == first_time]
        if len(first) != 1:
            return ()  # no guessing between equally early source identities
        results.append((literal, first[0]))
    return tuple(results)


def literal_location_hit(context, literal, observation):
    """One bounded original quote, with its single authoritative source identity."""
    from dovideo.domain import VideoEvidenceHit
    item = observation.source_item
    segment = next(s for s in context.segments if s.segment_id == item.segment_id)
    hit = VideoEvidenceHit(start_ms=segment.start_ms, end_ms=segment.end_ms,
        source=item.source_type, snippet="",
        transcript=observation.text if item.source_type == "ASR" else "",
        ocr_texts=(observation.text,) if item.source_type == "OCR" else (),
        source_revision=context.source_revision, segment_id=item.segment_id,
        source_item_ids=(item.source_item_id,))
    return hit, bounded_source_hit(hit, literal)
