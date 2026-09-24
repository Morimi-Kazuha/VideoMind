from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from dovideo.application.evaluation_contracts import (
    EVALUATION_CONTRACT_VERSION,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationFailureCategory,
    EvaluationExclusionCategory,
    EvaluationQueryCategory,
    EvaluationResultStatus,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationStrategy,
    ExpectedEvidenceRef,
    ExpectedTemporalRegion,
    RequiredFact,
    DeterministicMetrics,
    MeasurementState,
    case_results_from_jsonl,
    case_results_to_jsonl,
    dataset_from_json,
    dataset_to_json,
    read_dataset,
)


REVISION = "a" * 64


def _case(case_id: str = "speech-001") -> EvaluationCase:
    ref = ExpectedEvidenceRef(
        ref_id="evidence-1",
        source_revision=REVISION,
        source_item_id="item-1",
        source_type="ASR",
        timestamp_ms=0,
        start_ms=0,
        end_ms=1_000,
    )
    return EvaluationCase(
        case_id=case_id,
        dataset_version="golden-dataset-v1",
        media_ref="work/media/representative-long.mp4",
        source_revision=REVISION,
        query="What was said?",
        mode="GENERAL",
        query_category=EvaluationQueryCategory.DIRECT_SPEECH_FACT,
        expected_evidence_refs=(ref,),
        expected_temporal_regions=(ExpectedTemporalRegion(start_ms=0, end_ms=1_000),),
        allowed_source_types=("ASR",),
        required_facts=(
            RequiredFact(
                fact_id="fact-1",
                description="The answer identifies the spoken fact.",
                linked_evidence_refs=("evidence-1",),
            ),
        ),
    )


def test_dataset_contract_is_strict_versioned_and_non_leaking() -> None:
    dataset = EvaluationDataset(cases=(_case(),))

    assert dataset.evaluation_contract_version == EVALUATION_CONTRACT_VERSION
    assert dataset.case_count == 1
    assert dataset.source_revision_set == (REVISION,)
    assert dataset.dataset_digest
    assert dataset.cases[0].execution_input() == {
        "media_ref": "work/media/representative-long.mp4",
        "query": "What was said?",
        "mode": "GENERAL",
    }
    assert "required_facts" not in dataset.cases[0].execution_input()
    assert "expected_evidence_refs" not in dataset.cases[0].execution_input()


def test_dataset_json_round_trip_is_stable_and_uses_camel_aliases() -> None:
    dataset = EvaluationDataset(cases=(_case(),))
    payload = dataset_to_json(dataset)
    restored = dataset_from_json(payload)

    assert restored == dataset
    assert json.loads(payload)["evaluationContractVersion"] == EVALUATION_CONTRACT_VERSION
    assert json.loads(payload)["cases"][0]["sourceRevision"] == REVISION
    assert dataset_to_json(restored) == payload


def test_unknown_fields_and_invalid_auto_mode_fail_closed() -> None:
    with pytest.raises(ValidationError):
        EvaluationCase(**{**_case().model_dump(), "unexpected": True})

    with pytest.raises(ValidationError):
        EvaluationCase(**{**_case().model_dump(), "mode": "AUTO"})


def test_duplicate_cases_refs_and_revision_mismatch_are_not_repaired() -> None:
    with pytest.raises(ValidationError):
        EvaluationDataset(cases=(_case("same"), _case("same")))

    duplicate_ref = ExpectedEvidenceRef(
        source_revision=REVISION,
        source_item_id="item-1",
        source_type="ASR",
        timestamp_ms=0,
        start_ms=0,
        end_ms=1_000,
    )
    with pytest.raises(ValidationError):
        EvaluationCase(**{**_case().model_dump(), "expected_evidence_refs": (duplicate_ref, duplicate_ref)})

    mismatched = ExpectedEvidenceRef(
        source_revision="b" * 64,
        source_item_id="item-1",
        source_type="ASR",
    )
    with pytest.raises(ValidationError):
        EvaluationCase(
            **{
                **_case().model_dump(),
                "expected_evidence_refs": (mismatched,),
            }
        )


def test_case_result_identity_digest_and_jsonl_are_deterministic() -> None:
    result = EvaluationCaseResult(
        run_id="run-1",
        case_id="speech-001",
        strategy=EvaluationStrategy.ALWAYS_FAST,
        trial_index=0,
        mode="GENERAL",
        query_category=EvaluationQueryCategory.DIRECT_SPEECH_FACT,
        source_revision=REVISION,
        status=EvaluationResultStatus.SUCCESSFUL,
    )
    payload = case_results_to_jsonl((result,))
    restored = case_results_from_jsonl(payload)

    assert restored == (result,)
    assert result.result_digest
    assert result.identity() == ("speech-001", EvaluationStrategy.ALWAYS_FAST, 0)

    with pytest.raises(ValueError):
        case_results_to_jsonl((result, result))


def test_failure_and_exclusion_statuses_require_their_reason_contract() -> None:
    with pytest.raises(ValidationError):
        EvaluationCaseResult(
            run_id="run-1",
            case_id="speech-001",
            strategy="ALWAYS_FAST",
            trial_index=0,
            mode="GENERAL",
            query_category="DIRECT_SPEECH_FACT",
            source_revision=REVISION,
            status="FAILED",
        )

    failed = EvaluationCaseResult(
        run_id="run-1",
        case_id="speech-001",
        strategy="ALWAYS_FAST",
        trial_index=0,
        mode="GENERAL",
        query_category="DIRECT_SPEECH_FACT",
        source_revision=REVISION,
        status="FAILED",
        failure_category=EvaluationFailureCategory.SCHEMA_FAILURE,
    )
    assert failed.status is EvaluationResultStatus.FAILED

    with pytest.raises(ValidationError):
        EvaluationCaseResult(
            run_id="run-1",
            case_id="speech-001",
            strategy="ALWAYS_FAST",
            trial_index=0,
            mode="GENERAL",
            query_category="DIRECT_SPEECH_FACT",
            source_revision=REVISION,
            status="EXCLUDED",
            exclusion_reason="artifact mismatch",
        )

    excluded = EvaluationCaseResult(
        run_id="run-1",
        case_id="speech-001",
        strategy="ALWAYS_FAST",
        trial_index=0,
        mode="GENERAL",
        query_category="DIRECT_SPEECH_FACT",
        source_revision=REVISION,
        status="EXCLUDED",
        exclusion_category=EvaluationExclusionCategory.ARTIFACT_INCOMPATIBILITY,
        exclusion_reason="artifact mismatch",
    )
    assert excluded.status is EvaluationResultStatus.EXCLUDED


def test_initial_real_artifact_dataset_loads_without_claiming_completeness() -> None:
    dataset_path = Path(__file__).parents[2] / "datasets" / "x3" / "golden-dataset-v1.json"
    dataset = read_dataset(dataset_path)

    assert dataset.case_count == 16
    assert dataset.completeness.value == "DATASET_INCOMPLETE"
    assert len(dataset.source_revision_set) == 1
    assert {case.mode.value for case in dataset.cases} == {
        "GENERAL",
        "LEARNING",
        "REVIEW",
        "CREATION",
    }
    assert all(case.source_revision == dataset.source_revision_set[0] for case in dataset.cases)


def test_run_contract_tracks_publishability_and_rejects_bad_denominators() -> None:
    run = EvaluationRun(
        run_id="run-1",
        dataset_version="golden-dataset-v1",
        git_sha="a" * 40,
        working_tree_state="CLEAN",
        planned_count=2,
        executed_count=1,
        successful_count=1,
        status=EvaluationRunStatus.RUNNING,
    )
    assert run.publishable is True

    with pytest.raises(ValidationError):
        EvaluationRun(
            run_id="run-2",
            dataset_version="golden-dataset-v1",
            planned_count=1,
            executed_count=2,
        )

    with pytest.raises(ValidationError):
        EvaluationRun(
            run_id="run-3",
            dataset_version="golden-dataset-v1",
            environment={"provider_key": "must-not-be-stored"},
        )


def test_metric_values_require_explicit_measurement_state() -> None:
    with pytest.raises(ValidationError):
        DeterministicMetrics(required_fact_coverage=1.0)

    measured = DeterministicMetrics(
        required_fact_coverage=1.0,
        schema_valid=True,
        measurement_state=MeasurementState.MEASURED,
    )
    assert measured.required_fact_coverage == 1.0


def test_judge_projection_is_post_execution_only() -> None:
    case = _case()
    execution = case.execution_input()
    judge = case.judge_input({"title": "answer"})

    assert "required_facts" not in execution
    assert judge["required_facts"][0]["fact_id"] == "fact-1"
    assert judge["model_output"] == {"title": "answer"}
