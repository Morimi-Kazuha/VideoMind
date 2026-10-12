"""Synthetic contract regressions, not measurements of the real course video."""
import pytest
from pydantic import ValidationError

from dovideo.application.context import VideoContextBuilder
from dovideo.application.value_objects import AsrBranchOutcome, OcrBranchOutcome, MediaObservationBundle, TranscriptSpan
from dovideo.application.evaluation_contracts import EvaluationCase, EvaluationDataset, ExpectedEvidenceRef, RequiredFact
from dovideo.application.evaluation_runner import EvaluationRetrievedEvidence
from dovideo.application.evaluation_pilot import (PilotAnnotations, PilotCaseAnnotation,
    PilotFactAnnotation, validate_pilot, score_pilot_retrieval, literal_variant_diagnostic)


def fixture(*, no_answer=False):
    context = VideoContextBuilder().build("synthetic-contract-test", "", MediaObservationBundle(
        asr=AsrBranchOutcome(observations=(TranscriptSpan(start_ms=1000, end_ms=2000, text="red"),
            TranscriptSpan(start_ms=61000, end_ms=62000, text="blue")), attempted=1),
        ocr=OcrBranchOutcome(observations=(), attempted=0)), media_content_identity="synthetic-only")
    refs = tuple(ExpectedEvidenceRef(ref_id=f"r{i}", source_revision=context.source_revision,
        source_item_id=o.source_item.source_item_id, segment_id=o.source_item.segment_id,
        source_type="ASR", timestamp_ms=o.source_item.timestamp_ms,
        start_ms=o.source_item.timestamp_ms, end_ms=o.source_item.end_ms)
        for i,o in enumerate(context.observations))
    case = EvaluationCase(case_id="case", dataset_version="pilot-test", media_ref=context.source,
        source_revision=context.source_revision, query="a runtime question", mode="GENERAL",
        query_category="DIRECT_SPEECH_FACT", expected_evidence_refs=() if no_answer else refs,
        reference_answer="source is absent" if no_answer else "gold-only secret answer",
        required_facts=() if no_answer else (RequiredFact(fact_id="f", description="two facts",
            acceptable_variants=("an equivalent answer",), linked_evidence_refs=("r0", "r1")),))
    dataset = EvaluationDataset(dataset_version="pilot-test", cases=(case,), target_case_count=1,
        completeness="COMPLETE", data_classification="SYNTHETIC")
    meta = PilotCaseAnnotation(case_id="case", answerability="NO_ANSWER_IN_VIDEO" if no_answer else "ANSWERABLE",
        annotation_notes="synthetic test only", absence_review_scope="whole synthetic source" if no_answer else None,
        fact_annotations=() if no_answer else (PilotFactAnnotation(fact_id="f", sufficient_refs=("r0","r1"),
            verification_notes="synthetic test", support_rule="ALL"),))
    annotation = PilotAnnotations(dataset_version=dataset.dataset_version, dataset_digest=dataset.dataset_digest,
        source_revision=context.source_revision, media_sha256="a"*64, generator_identity="test",
        verifier_identity="test", verification_independence="not independent", cases=(meta,))
    ranks = tuple(EvaluationRetrievedEvidence(source_revision=context.source_revision,
        source_item_ids=(o.source_item.source_item_id,), segment_id=o.source_item.segment_id,
        source_type="ASR", start_ms=o.source_item.timestamp_ms, end_ms=o.source_item.end_ms, rank=i+1)
        for i,o in enumerate(context.observations))
    return context, case, dataset, meta, annotation, ranks


def test_no_answer_is_valid_without_fabricated_evidence():
    ctx, case, ds, meta, ann, ranks = fixture(no_answer=True)
    assert validate_pilot(ds, ann, ctx, duration_ms=120000)["no_answer"] == 1
    score = score_pilot_retrieval(case, meta, ranks)
    assert score["retrieval_metrics"] == "NOT_APPLICABLE"
    assert score["refusal_correctness"] == "NOT_MEASURED"
    assert score["retrieved_count"] == 2


def test_no_answer_cannot_claim_source_absence_from_retrieval_failure():
    with pytest.raises(ValidationError, match="review scope"):
        PilotCaseAnnotation(case_id="absent", answerability="NO_ANSWER_IN_VIDEO", annotation_notes="not retrieved")


def test_positive_source_identity_and_exact_timestamp_are_checked():
    ctx, _, ds, _, ann, _ = fixture()
    assert validate_pilot(ds, ann, ctx, duration_ms=120000)["resolved_evidence_refs"] == 2
    ref = ds.cases[0].expected_evidence_refs[0].model_copy(update={"timestamp_ms": 1001})
    case = ds.cases[0].model_copy(update={"expected_evidence_refs": (ref, ds.cases[0].expected_evidence_refs[1])})
    # Test the independent source check even if a caller bypasses strict digest parsing.
    with pytest.raises(ValueError, match="timestamp mismatch"):
        validate_pilot(ds.model_copy(update={"cases": (case,)}), ann, ctx, duration_ms=120000)


def test_foreign_revision_and_missing_source_item_are_rejected():
    ctx, _, ds, _, ann, _ = fixture()
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_pilot(ds, ann.model_copy(update={"source_revision":"b"*64}), ctx, duration_ms=120000)
    ref = ds.cases[0].expected_evidence_refs[0].model_copy(update={"source_item_id":"missing"})
    case = ds.cases[0].model_copy(update={"expected_evidence_refs": (ref, ds.cases[0].expected_evidence_refs[1])})
    with pytest.raises(ValueError, match="exact SourceItem"):
        validate_pilot(ds.model_copy(update={"cases":(case,)}), ann, ctx, duration_ms=120000)


def test_approval_and_freeze_require_recorded_human_review():
    _, _, _, meta, ann, _ = fixture()
    with pytest.raises(ValidationError, match="reviewer"):
        PilotCaseAnnotation(**{**meta.model_dump(), "annotation_status":"HUMAN_APPROVED"})
    with pytest.raises(ValidationError, match="freeze"):
        PilotAnnotations(**{**ann.model_dump(), "frozen":True})


def test_gold_changes_do_not_change_runtime_input():
    _, case, _, _, _, _ = fixture()
    changed = case.model_copy(update={"reference_answer":"another secret", "required_facts":()})
    assert changed.execution_input() == case.execution_input()
    assert set(case.execution_input()) == {"media_ref", "query", "mode"}


def test_fact_recall_requires_all_sufficient_evidence_by_default():
    _, case, _, meta, _, ranks = fixture()
    result = score_pilot_retrieval(case, meta, ranks)
    assert result["recall_at_k"][1] == 0.5
    assert result["fact_evidence_recall_at_k"]["1"]["coverage"] == 0
    assert result["fact_evidence_recall_at_k"]["3"]["coverage"] == 1
    assert result["claim_entailment"] == "NOT_MEASURED"


def test_alternative_support_is_explicit_and_related_only_cannot_pass():
    ctx, case, ds, meta, ann, ranks = fixture()
    alt_fact = meta.fact_annotations[0].model_copy(update={"support_rule":"ANY"})
    alternative = meta.model_copy(update={"fact_annotations":(alt_fact,)})
    assert score_pilot_retrieval(case, alternative, ranks)["fact_evidence_recall_at_k"]["1"]["coverage"] == 1
    bad = alt_fact.model_copy(update={"related_only_refs":("r0",)})
    badmeta = meta.model_copy(update={"fact_annotations":(bad,)})
    with pytest.raises(ValueError, match="related-only"):
        validate_pilot(ds, ann.model_copy(update={"cases":(badmeta,)}), ctx, duration_ms=120000)


def test_variant_normalization_does_not_become_semantic_accuracy():
    _, case, _, _, _, _ = fixture()
    matched = literal_variant_diagnostic(case.required_facts, "An equivalent answer!")
    assert matched["matched_fraction"] == 1
    assert matched["semantic_fact_coverage"] == "NOT_MEASURED"
    # Even a negated phrase can hit a literal variant; explicitly never score it as truth.
    negated = literal_variant_diagnostic(case.required_facts, "NOT an equivalent answer")
    assert negated["semantic_fact_coverage"] == "NOT_MEASURED"


def test_scoring_is_repeatable_without_another_execution():
    _, case, _, meta, _, ranks = fixture()
    assert score_pilot_retrieval(case, meta, ranks) == score_pilot_retrieval(case, meta, ranks)
