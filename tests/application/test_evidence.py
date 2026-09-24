from __future__ import annotations

import pytest

from dovideo.application.evidence import (
    EMPTY_CRITIQUE_FEEDBACK,
    INVALID_EVIDENCE_FEEDBACK,
    NULL_CRITIQUE_FEEDBACK,
    EvidenceVerificationService,
    enforce_evidence_bounds,
    normalize_evidence_text,
    supported,
    supports_claim,
    timestamp_covered,
)
from dovideo.domain import (
    AnalysisEvidence,
    AnalysisResult,
    CriticResult,
    VideoContext,
    VideoSegment,
)


def _segment(
    start_ms: int,
    end_ms: int,
    *,
    transcript: str = "",
    ocr_texts: tuple[str, ...] = (),
) -> VideoSegment:
    return VideoSegment(
        start_ms=start_ms,
        end_ms=end_ms,
        transcript=transcript,
        ocr_texts=ocr_texts,
    )


def _context(*segments: VideoSegment) -> VideoContext:
    return VideoContext(source="video.mp4", user_goal="goal", segments=segments)


def _evidence(
    timestamp_ms: int,
    *,
    source: str = "ASR",
    content: str = "speech",
    claim: str = "claim",
) -> AnalysisEvidence:
    return AnalysisEvidence(
        timestamp_ms=timestamp_ms,
        source=source,
        content=content,
        claim=claim,
    )


def _result(
    *,
    conclusions: tuple[str, ...] = (),
    evidence: tuple[AnalysisEvidence, ...] = (),
) -> AnalysisResult:
    return AnalysisResult(conclusions=conclusions, evidence=evidence)


@pytest.fixture()
def verifier() -> EvidenceVerificationService:
    return EvidenceVerificationService()


def test_timestamp_coverage_is_half_open_and_checks_overlapping_windows(
    verifier: EvidenceVerificationService,
) -> None:
    context = _context(_segment(0, 10), _segment(10, 20))

    assert verifier.timestamp_covered(context, _evidence(0))
    assert verifier.timestamp_covered(context, _evidence(9))
    assert verifier.timestamp_covered(context, _evidence(10))
    assert verifier.timestamp_covered(context, _evidence(19))
    assert not verifier.timestamp_covered(context, _evidence(20))
    assert not verifier.timestamp_covered(None, _evidence(0))
    assert not verifier.timestamp_covered(context, None)


def test_supported_uses_asr_ocr_or_combined_source_text(
    verifier: EvidenceVerificationService,
) -> None:
    context = _context(
        _segment(
            0,
            100,
            transcript="Speech says hello",
            ocr_texts=("Screen title", "Second line"),
        )
    )

    assert verifier.supported(context, _evidence(50, content="HELLO", source="asr"))
    assert verifier.supported(
        context,
        _evidence(50, source="OCR", content="screen title"),
    )
    assert verifier.supported(
        context,
        _evidence(50, source="ASR+OCR", content="hello screen title"),
    )
    assert not verifier.supported(
        context,
        _evidence(50, source="ASR", content="screen title"),
    )
    assert not verifier.supported(
        context,
        _evidence(50, source="MANUAL", content="hello"),
    )
    assert not verifier.supported(
        context,
        _evidence(50, source="OCR", content="   "),
    )
    assert not verifier.supported(
        context,
        _evidence(100, source="ASR", content="hello"),
    )


def test_normalization_removes_unicode_punctuation_symbols_and_whitespace() -> None:
    value = "  HÉLLO\u2003世界，温度：23℃！\n"

    assert normalize_evidence_text(value) == "héllo世界温度23"
    assert normalize_evidence_text(None) == ""
    assert normalize_evidence_text("，！？—©") == ""


def test_supported_matches_punctuation_case_symbols_and_unicode_space(
    verifier: EvidenceVerificationService,
) -> None:
    context = _context(_segment(0, 100, transcript="Hello,\u2003WORLD! 23℃"))

    assert verifier.supported(
        context,
        _evidence(1, content="hello world 23", source="ASR"),
    )
    assert not verifier.supported(
        context,
        _evidence(1, content="hello fabricated", source="ASR"),
    )


def test_supports_claim_requires_normalized_claim_equality_and_support(
    verifier: EvidenceVerificationService,
) -> None:
    context = _context(_segment(0, 100, transcript="Claim text is here"))
    evidence = _evidence(
        1,
        content="claim text",
        claim="Claim, text!",
    )

    assert verifier.supports_claim(context, "claim text", evidence)
    assert verifier.supports_claim(context, "CLAIM TEXT", evidence)
    assert not verifier.supports_claim(context, "different claim", evidence)
    assert not verifier.supports_claim(context, "", evidence)
    assert not verifier.supports_claim(context, None, evidence)
    assert not verifier.supports_claim(context, "claim text", None)
    assert not verifier.supports_claim(
        context,
        "Claim text",
        _evidence(1, content="fabricated", claim="Claim text"),
    )


def test_paraphrased_evidence_claim_does_not_bind_to_conclusion(
    verifier: EvidenceVerificationService,
) -> None:
    context = _context(_segment(0, 100, transcript="The speaker opens the talk"))
    result = _result(
        conclusions=("The speaker opens the talk",),
        evidence=(
            _evidence(
                1,
                content="The speaker opens the talk",
                claim="The presenter begins speaking",
            ),
        ),
    )

    critique = verifier.enforce_evidence_bounds(
        context,
        result,
        CriticResult(passed=True),
    )

    assert critique.passed is False
    assert critique.unsupported_claims == ("The speaker opens the talk",)


def test_none_inputs_are_rejected_without_exceptions(
    verifier: EvidenceVerificationService,
) -> None:
    assert not verifier.supported(None, None)
    assert not verifier.supports_claim(None, "claim", None)
    assert verifier.enforce_evidence_bounds(None, None) == CriticResult(
        passed=False,
        feedback=(NULL_CRITIQUE_FEEDBACK,),
    )


def test_passed_critique_with_declared_problems_is_normalized_to_failed(
    verifier: EvidenceVerificationService,
) -> None:
    critique = CriticResult(
        passed=True,
        feedback=("feedback",),
        missing_requirements=("missing",),
        unsupported_claims=("unsupported",),
        required_timestamps=(4,),
    )

    normalized = verifier.enforce_evidence_bounds(None, None, critique)

    assert normalized.passed is False
    assert normalized.feedback == critique.feedback
    assert normalized.missing_requirements == critique.missing_requirements
    assert normalized.unsupported_claims == critique.unsupported_claims
    assert normalized.required_timestamps == critique.required_timestamps


def test_empty_failed_critique_gets_java_generic_feedback(
    verifier: EvidenceVerificationService,
) -> None:
    output = verifier.enforce_evidence_bounds(
        None,
        _result(),
        CriticResult(passed=False),
    )

    assert output == CriticResult(
        passed=False,
        feedback=(EMPTY_CRITIQUE_FEEDBACK,),
    )


def test_invalid_evidence_and_unsupported_conclusions_preserve_order_and_dedupe(
    verifier: EvidenceVerificationService,
) -> None:
    context = _context(_segment(0, 10, transcript="supported claim"))
    valid = _evidence(0, content="supported", claim="supported claim")
    invalid_text = _evidence(0, content="not present", claim="bad")
    invalid_timestamp = _evidence(20, content="supported", claim="bad2")
    result = _result(
        conclusions=("supported claim", "missing claim", "missing claim"),
        evidence=(valid, invalid_text, invalid_timestamp, invalid_text),
    )
    critique = CriticResult(
        passed=True,
        feedback=("existing feedback",),
        missing_requirements=("keep this",),
        unsupported_claims=("missing claim",),
        required_timestamps=(20,),
    )

    output = verifier.enforce_evidence_bounds(context, result, critique)

    assert output.passed is False
    assert output.feedback == ("existing feedback", INVALID_EVIDENCE_FEEDBACK)
    assert output.missing_requirements == ("keep this",)
    assert output.unsupported_claims == (
        "missing claim",
        "证据无法在原始 ASR/OCR 中核验: 0",
        "证据无法在原始 ASR/OCR 中核验: 20",
        "证据无法在原始 ASR/OCR 中核验: 0",
    )
    assert output.required_timestamps == (20, 0)


def test_unsupported_conclusion_is_detected_even_when_other_evidence_is_valid(
    verifier: EvidenceVerificationService,
) -> None:
    context = _context(_segment(0, 10, transcript="first claim"))
    result = _result(
        conclusions=("first claim", "fabricated claim"),
        evidence=(_evidence(0, content="first", claim="first claim"),),
    )

    output = verifier.enforce_evidence_bounds(
        context,
        result,
        CriticResult(passed=True),
    )

    assert output.passed is False
    assert output.unsupported_claims == ("fabricated claim",)
    assert output.feedback == (INVALID_EVIDENCE_FEEDBACK,)
    assert output.required_timestamps == ()


def test_fully_valid_result_leaves_critique_fields_unchanged(
    verifier: EvidenceVerificationService,
) -> None:
    context = _context(_segment(0, 10, transcript="valid claim"))
    result = _result(
        conclusions=("valid claim",),
        evidence=(_evidence(0, content="valid", claim="valid claim"),),
    )
    critique = CriticResult(
        passed=True,
        feedback=(),
        missing_requirements=(),
        unsupported_claims=(),
        required_timestamps=(),
    )

    output = verifier.enforce_evidence_bounds(context, result, critique)

    assert output == critique
    assert output.passed is True


def test_empty_evidence_returns_normalized_critique_without_claim_scan(
    verifier: EvidenceVerificationService,
) -> None:
    critique = CriticResult(passed=True)
    output = verifier.enforce_evidence_bounds(
        _context(_segment(0, 10, transcript="unrelated")),
        _result(conclusions=("fabricated",)),
        critique,
    )

    assert output == critique
    assert output.passed is True


def test_camel_case_critique_mapping_is_accepted_and_output_is_immutable(
    verifier: EvidenceVerificationService,
) -> None:
    context = _context(_segment(0, 10, transcript="supported"))
    result = _result(
        conclusions=("unsupported",),
        evidence=(_evidence(0, content="supported", claim="supported"),),
    )
    mapping = {
        "passed": True,
        "missingRequirements": ["requirement"],
        "unsupportedClaims": ["existing"],
        "requiredTimestamps": [3],
    }

    output = verifier.enforce_evidence_bounds(context, result, mapping)

    assert output.passed is False
    assert output.missing_requirements == ("requirement",)
    assert output.unsupported_claims == ("existing", "unsupported")
    assert output.required_timestamps == (3,)
    assert isinstance(output.feedback, tuple)
    assert isinstance(output.unsupported_claims, tuple)
    assert isinstance(output.required_timestamps, tuple)


def test_bounds_does_not_mutate_frozen_domain_inputs(
    verifier: EvidenceVerificationService,
) -> None:
    context = _context(_segment(0, 10, transcript="valid"))
    evidence = _evidence(0, content="fabricated", claim="claim")
    result = _result(conclusions=("claim",), evidence=(evidence,))
    critique = CriticResult(
        passed=False,
        feedback=("keep",),
        missing_requirements=("missing",),
        unsupported_claims=(),
        required_timestamps=(),
    )
    before = (context, result, critique)

    output = verifier.enforce_evidence_bounds(context, result, critique)

    assert (context, result, critique) == before
    assert result.evidence == (evidence,)
    assert output is not critique
    with pytest.raises((TypeError, ValueError)):
        output.feedback += ("mutate",)  # type: ignore[misc]


def test_convenience_function_matches_stateless_service() -> None:
    context = _context(_segment(0, 10, transcript="supported"))
    result = _result(
        conclusions=("supported",),
        evidence=(_evidence(0, content="supported", claim="supported"),),
    )
    critique = CriticResult(passed=True)

    assert enforce_evidence_bounds(context, result, critique) == (
        EvidenceVerificationService().enforce_evidence_bounds(
            context,
            result,
            critique,
        )
    )


def test_module_level_java_named_wrappers_bind_their_arguments() -> None:
    context = _context(_segment(0, 10, transcript="supported"))
    evidence = _evidence(0, content="supported", claim="claim")

    assert timestamp_covered(context, evidence)
    assert supported(context, evidence)
    assert supports_claim(context, "claim", evidence)
