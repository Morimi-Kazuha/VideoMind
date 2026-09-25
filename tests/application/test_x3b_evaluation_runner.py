from __future__ import annotations

import asyncio
import json

import pytest

from dovideo.application.evaluation_contracts import (
    DataClassification,
    EvaluationCase,
    EvaluationDataset,
    EvaluationFailureCategory,
    EvaluationResultStatus,
    EvaluationStrategy,
    ExpectedEvidenceRef,
    ExpectedTemporalRegion,
    MeasurementState,
    TokenUsageMeasurement,
)
from dovideo.application.evaluation_runner import (
    AgentLoopEvaluationAdapter,
    EvaluationDuplicateResultError,
    EvaluationExecutionObservation,
    EvaluationObservedFailure,
    EvaluationProviderError,
    EvaluationRetrievedEvidence,
    EvaluationRunner,
    EvaluationRunnerConfig,
    EvaluationSourceArtifact,
    MappingEvaluationSourceResolver,
    PricingCatalog,
    PricingEntry,
    calculate_retrieval_metrics,
    evidence_matches,
)
from dovideo.domain import AgentState, AnalysisEvidence, AnalysisResult, CriticResult


REVISION = "a" * 64
OTHER_REVISION = "b" * 64


def _ref(
    *,
    ref_id: str = "ref-1",
    match_level: str = "SOURCE_ITEM",
    source_revision: str = REVISION,
    source_item_id: str | None = "item-1",
    segment_id: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> ExpectedEvidenceRef:
    return ExpectedEvidenceRef(
        refId=ref_id,
        sourceRevision=source_revision,
        sourceItemId=source_item_id,
        segmentId=segment_id,
        sourceType="ASR",
        startMs=start_ms,
        endMs=end_ms,
        matchLevel=match_level,
    )


def _case(
    case_id: str = "case-1",
    *,
    source_revision: str = REVISION,
    expected_refs: tuple[ExpectedEvidenceRef, ...] = (),
) -> EvaluationCase:
    return EvaluationCase(
        caseId=case_id,
        datasetVersion="golden-dataset-v1",
        mediaRef="prepared/video.mp4",
        sourceRevision=source_revision,
        query="find the answer",
        mode="GENERAL",
        queryCategory="DIRECT_SPEECH_FACT",
        expectedEvidenceRefs=expected_refs,
    )


def _dataset(*cases: EvaluationCase) -> EvaluationDataset:
    return EvaluationDataset(
        cases=cases,
        dataClassification=DataClassification.SYNTHETIC,
    )


def _artifact(revision: str = REVISION) -> EvaluationSourceArtifact:
    return EvaluationSourceArtifact(
        mediaRef="prepared/video.mp4",
        sourceRevision=revision,
    )


def _result(revision: str = REVISION, source_item_id: str = "item-1") -> AnalysisResult:
    return AnalysisResult(
        title="synthetic result",
        conclusions=("answer",),
        evidence=(
            AnalysisEvidence(
                source="ASR",
                content="answer",
                claim="answer",
                timestamp_ms=100,
                source_revision=revision,
                source_item_id=source_item_id,
            ),
        ),
    )


class FakeAdapter:
    def __init__(
        self,
        *,
        fail: bool = False,
        with_usage: bool = False,
        provider_model: bool = False,
    ) -> None:
        self.fail = fail
        self.with_usage = with_usage
        self.provider_model = provider_model
        self.calls: list[dict[str, object]] = []

    async def execute(self, execution_input, **kwargs):
        self.calls.append(dict(execution_input))
        assert "expectedEvidenceRefs" not in execution_input
        assert "requiredFacts" not in execution_input
        assert "referenceAnswer" not in execution_input
        if self.fail:
            raise EvaluationProviderError("provider failure is not persisted")
        usage = TokenUsageMeasurement()
        if self.with_usage:
            usage = TokenUsageMeasurement(
                inputTokens=100,
                outputTokens=50,
                totalTokens=150,
                measurementState=MeasurementState.MEASURED,
            )
        retrieved = (
            EvaluationRetrievedEvidence(
                sourceRevision=REVISION,
                sourceItemId="item-1",
                sourceType="ASR",
                timestampMs=100,
                rank=1,
            ),
        )
        return EvaluationExecutionObservation(
            result=_result(),
            retrieved_evidence=retrieved,
            token_usage=usage,
            execution_id="synthetic-execution",
            provider=("test-provider" if self.provider_model else None),
            model=("test-model" if self.provider_model else None),
        )


def _run(
    dataset: EvaluationDataset,
    adapter: FakeAdapter,
    tmp_path,
    *,
    resolver_revision: str = REVISION,
    config: EvaluationRunnerConfig | None = None,
    pricing_catalog: PricingCatalog | None = None,
):
    runner = EvaluationRunner(
        execution_adapter=adapter,
        source_resolver=MappingEvaluationSourceResolver(
            {"prepared/video.mp4": _artifact(resolver_revision)}
        ),
        pricing_catalog=pricing_catalog,
        repo_root=tmp_path,
        run_id="synthetic-run",
    )
    return asyncio.run(
        runner.run(
            dataset,
            config
            or EvaluationRunnerConfig(
                artifactOutput=str(tmp_path / "artifacts"),
            ),
        )
    )


def test_successful_case_writes_jsonl_digest_summary_and_cost(tmp_path) -> None:
    case = _case(expected_refs=(_ref(),))
    adapter = FakeAdapter(with_usage=True)
    catalog = PricingCatalog(
        pricingVersion="pricing-v1",
        entries=(
            PricingEntry(
                provider="test-provider",
                model="test-model",
                inputRatePerMillion=1.0,
                outputRatePerMillion=2.0,
            ),
        ),
    )
    # The fake does not identify a provider/model, so this run intentionally
    # proves the measured quality path remains valid without calculated cost.
    outcome = _run(_dataset(case), adapter, tmp_path, pricing_catalog=catalog)

    assert outcome.run.status.value == "COMPLETED"
    assert outcome.run.planned_count == outcome.run.executed_count == 1
    assert outcome.results[0].status is EvaluationResultStatus.SUCCESSFUL
    assert outcome.results[0].result_digest
    assert outcome.results[0].deterministic_metrics.retrieval_recall_at_k == 1.0
    assert outcome.results[0].cost.measurement_state is MeasurementState.NOT_MEASURED
    assert (tmp_path / "artifacts" / "results.jsonl").exists()
    assert (tmp_path / "artifacts" / "summary.json").exists()
    payload = json.loads((tmp_path / "artifacts" / "summary.json").read_text(encoding="utf-8"))
    assert payload["successfulCount"] == 1
    assert payload["metrics"]["retrieval_recall_at_1"]["denominator"] == 1


def test_failed_case_is_persisted_and_keeps_planned_denominator(tmp_path) -> None:
    adapter = FakeAdapter(fail=True)
    outcome = _run(_dataset(_case()), adapter, tmp_path)

    result = outcome.results[0]
    assert result.status is EvaluationResultStatus.FAILED
    assert result.failure_category is EvaluationFailureCategory.PROVIDER_FAILURE
    assert outcome.run.planned_count == 1
    assert outcome.summary.failed_count == 1
    assert len((tmp_path / "artifacts" / "results.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_controlled_partial_run_reports_not_run_without_losing_jsonl(tmp_path) -> None:
    config = EvaluationRunnerConfig(
        artifactOutput=str(tmp_path / "artifacts"),
        maxExecutions=1,
    )
    outcome = _run(
        _dataset(_case("case-1"), _case("case-2")),
        FakeAdapter(),
        tmp_path,
        config=config,
    )

    assert outcome.run.status.value == "PARTIAL"
    assert outcome.run.planned_count == 2
    assert outcome.run.executed_count == 1
    assert outcome.run.not_run_count == 1
    assert len((tmp_path / "artifacts" / "results.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_source_revision_mismatch_is_excluded_without_adapter_call(tmp_path) -> None:
    adapter = FakeAdapter()
    outcome = _run(_dataset(_case()), adapter, tmp_path, resolver_revision=OTHER_REVISION)

    assert adapter.calls == []
    assert outcome.results[0].status is EvaluationResultStatus.EXCLUDED
    assert outcome.results[0].exclusion_category.value == "SOURCE_REVISION_MISMATCH"


def test_missing_usage_and_missing_pricing_are_not_measured(tmp_path) -> None:
    outcome = _run(_dataset(_case()), FakeAdapter(), tmp_path)
    result = outcome.results[0]

    assert result.token_usage.measurement_state is MeasurementState.NOT_MEASURED
    assert result.cost.measurement_state is MeasurementState.NOT_MEASURED
    assert result.latency.measurement_state is MeasurementState.MEASURED
    assert result.latency.total_e2e_ms is not None


def test_pricing_catalog_calculates_input_output_cost_without_vendor_logic(tmp_path) -> None:
    catalog = PricingCatalog(
        pricingVersion="pricing-v1",
        entries=(
            PricingEntry(
                provider="test-provider",
                model="test-model",
                inputRatePerMillion=1.0,
                outputRatePerMillion=2.0,
            ),
        ),
    )
    config = EvaluationRunnerConfig(
        artifactOutput=str(tmp_path / "artifacts"),
        pricingVersion="pricing-v1",
    )
    outcome = _run(
        _dataset(_case()),
        FakeAdapter(with_usage=True, provider_model=True),
        tmp_path,
        config=config,
        pricing_catalog=catalog,
    )
    cost = outcome.results[0].cost
    assert cost.measurement_state is MeasurementState.MEASURED
    assert cost.calculated_cost == pytest.approx(0.0002)
    assert cost.pricing_version == "pricing-v1"


def test_unavailable_requested_pricing_does_not_block_quality_run(tmp_path) -> None:
    config = EvaluationRunnerConfig(
        artifactOutput=str(tmp_path / "artifacts"),
        pricingVersion="missing-pricing-v1",
    )
    outcome = _run(
        _dataset(_case()),
        FakeAdapter(with_usage=True, provider_model=True),
        tmp_path,
        config=config,
    )
    assert outcome.results[0].status is EvaluationResultStatus.SUCCESSFUL
    assert outcome.results[0].cost.measurement_state is MeasurementState.NOT_MEASURED
    assert "pricing unavailable; cost NOT_MEASURED" in outcome.summary.caveats


def test_retrieval_metrics_respect_k_and_revision() -> None:
    refs = (_ref(), _ref(ref_id="ref-2", source_item_id="item-2"))
    retrieved = (
        EvaluationRetrievedEvidence(sourceRevision=REVISION, sourceItemId="item-1", sourceType="ASR", rank=1),
        EvaluationRetrievedEvidence(sourceRevision=REVISION, sourceItemId="wrong", sourceType="ASR", rank=2),
        EvaluationRetrievedEvidence(sourceRevision=REVISION, sourceItemId="item-2", sourceType="ASR", rank=3),
    )
    metrics = calculate_retrieval_metrics(refs, retrieved, k_values=(1, 3))

    assert metrics.recall_at_k == {1: 0.5, 3: 1.0}
    assert metrics.precision_at_k == {1: 1.0, 3: 2 / 3}
    assert metrics.mrr == 1.0
    mismatch = EvaluationRetrievedEvidence(
        sourceRevision=OTHER_REVISION,
        sourceItemId="item-1",
        sourceType="ASR",
        rank=1,
    )
    assert calculate_retrieval_metrics((_ref(),), (mismatch,), k_values=(1,)).mrr == 0.0
    assert not evidence_matches(refs[0], mismatch)


@pytest.mark.parametrize(
    ("start", "end", "expected_hit", "expected_coverage"),
    ((2_000, 3_000, False, 0.0), (500, 1_500, True, 0.5), (0, 2_000, True, 1.0)),
)
def test_temporal_metrics_report_no_partial_and_full_overlap(
    start: int,
    end: int,
    expected_hit: bool,
    expected_coverage: float,
) -> None:
    ref = _ref(
        match_level="TEMPORAL_REGION",
        source_item_id=None,
        start_ms=0,
        end_ms=2_000,
    )
    candidate = EvaluationRetrievedEvidence(
        sourceRevision=REVISION,
        sourceType="ASR",
        startMs=start,
        endMs=end,
        rank=1,
    )
    metrics = calculate_retrieval_metrics((ref,), (candidate,), k_values=(1,))
    assert metrics.temporal_hit is expected_hit
    assert metrics.temporal_coverage == expected_coverage


def test_temporal_region_projection_uses_case_revision() -> None:
    region_ref = _ref(
        match_level="TEMPORAL_REGION",
        source_item_id=None,
        start_ms=0,
        end_ms=2_000,
    )
    candidate = EvaluationRetrievedEvidence(
        sourceRevision=OTHER_REVISION,
        sourceType="ASR",
        startMs=0,
        endMs=2_000,
        rank=1,
    )
    metrics = calculate_retrieval_metrics(
        (),
        (candidate,),
        k_values=(1,),
        expected_temporal_regions=(
            ExpectedTemporalRegion(startMs=0, endMs=2_000, sourceType="ASR"),
        ),
        source_revision=REVISION,
    )
    assert metrics.temporal_hit is False
    assert metrics.temporal_coverage == 0.0


def test_config_fingerprint_and_duplicate_identity_are_stable(tmp_path) -> None:
    config_a = EvaluationRunnerConfig(artifactOutput=str(tmp_path / "a"))
    config_b = EvaluationRunnerConfig(artifactOutput=str(tmp_path / "a"))
    assert config_a.fingerprint() == config_b.fingerprint()

    adapter = FakeAdapter()
    outcome = _run(_dataset(_case()), adapter, tmp_path)
    writer = outcome.preflight.writer
    assert writer is not None
    with pytest.raises(EvaluationDuplicateResultError):
        changed = outcome.results[0].model_copy(
            update={"execution_order": 99, "result_digest": None}
        )
        changed = type(outcome.results[0])(**changed.model_dump(by_alias=True))
        writer.append_result(changed)


def test_incomplete_dataset_runs_and_summary_preserves_caveat(tmp_path) -> None:
    dataset = EvaluationDataset(
        cases=(_case(),),
        dataClassification=DataClassification.SYNTHETIC,
        completeness="DATASET_INCOMPLETE",
    )
    outcome = _run(dataset, FakeAdapter(), tmp_path)
    assert outcome.summary.dataset_completeness.value == "DATASET_INCOMPLETE"
    assert "OCR provenance coverage incomplete" in outcome.summary.caveats


def test_missing_git_is_local_non_publishable(tmp_path) -> None:
    outcome = _run(_dataset(_case()), FakeAdapter(), tmp_path)
    assert outcome.run.git_sha is None
    assert outcome.run.publishable is False
    assert outcome.run.working_tree_state.value == "UNKNOWN"


def test_privacy_artifact_does_not_contain_query_or_execution_body(tmp_path) -> None:
    secret_query = "raw transcript secret should stay out"
    case = _case()
    case = case.model_copy(update={"query": secret_query})
    outcome = _run(_dataset(case), FakeAdapter(fail=True), tmp_path)
    raw = (tmp_path / "artifacts" / "results.jsonl").read_text(encoding="utf-8")

    assert secret_query not in raw
    assert "provider failure is not persisted" not in raw
    assert "expectedEvidenceRefs" not in raw
    assert "requiredFacts" not in raw


def test_agent_loop_adapter_uses_prepared_context_and_normal_production_port(tmp_path) -> None:
    class FakeAgentLoop:
        def __init__(self) -> None:
            self.calls = []

        async def run(self, context, *, media_id=None, profile=None):
            self.calls.append((context, media_id, profile))
            return AgentState(
                goal=context.user_goal,
                result=_result(),
                critique=CriticResult(passed=True),
                round=1,
            )

    agent = FakeAgentLoop()
    adapter = AgentLoopEvaluationAdapter(agent)
    context_artifact = EvaluationSourceArtifact(
        mediaRef="prepared/video.mp4",
        sourceRevision=REVISION,
        context={"source": "prepared", "userGoal": "old", "sourceRevision": REVISION},
    )
    # The artifact model intentionally validates the same VideoContext used by
    # production.  Replacing the mapping above with a real context keeps this
    # test entirely offline while exercising the adapter boundary.
    from dovideo.domain import VideoContext

    context_artifact = context_artifact.model_copy(
        update={"context": VideoContext(source="prepared", sourceRevision=REVISION)}
    )
    observation = asyncio.run(
        adapter.execute(
            {"media_ref": "prepared/video.mp4", "query": "fresh query", "mode": "GENERAL"},
            artifact=context_artifact,
            strategy=EvaluationStrategy.CURRENT_PRODUCTION,
            trial_index=0,
            timeout_seconds=1.0,
        )
    )
    assert observation.result is not None
    assert observation.evidence_guard_pass is False
    assert observation.unsupported_claim_rate == 1.0
    assert agent.calls[0][0].user_goal == "fresh query"
    assert agent.calls[0][2].mode.value == "GENERAL"


def test_budget_exhaustion_has_its_own_failure_category() -> None:
    from dovideo.application.errors import BudgetExceededError
    from dovideo.application.evaluation_contracts import EvaluationFailureCategory
    from dovideo.application.evaluation_runner import _map_failure_category

    assert _map_failure_category(BudgetExceededError("limit")) is EvaluationFailureCategory.BUDGET_EXCEEDED


def test_invalid_model_dto_is_classified_as_schema_failure() -> None:
    from dovideo.application.evaluation_runner import _map_failure_category
    from dovideo.infrastructure.providers.errors import ModelResponseError

    assert _map_failure_category(ModelResponseError("invalid DTO")) is EvaluationFailureCategory.SCHEMA_FAILURE


def test_canonical_provider_fallback_is_classified_as_provider_failure() -> None:
    from dovideo.application.evaluation_runner import _map_failure_category
    from dovideo.infrastructure.r4_runtime import R4ProviderFallbackError

    assert _map_failure_category(R4ProviderFallbackError("fallback")) is EvaluationFailureCategory.PROVIDER_FAILURE


def test_failed_execution_keeps_provider_reported_usage(tmp_path) -> None:
    from dovideo.application.errors import BudgetExceededError

    class PartialAdapter:
        async def execute(self, *_args, **_kwargs):
            raise EvaluationObservedFailure(
                BudgetExceededError("limit"),
                EvaluationExecutionObservation(
                    token_usage=TokenUsageMeasurement(
                        inputTokens=100, outputTokens=20, totalTokens=120,
                        providerReported=True, measurementState=MeasurementState.MEASURED,
                    ),
                ),
            )

    outcome = _run(_dataset(_case()), PartialAdapter(), tmp_path)
    result = outcome.results[0]
    assert result.status is EvaluationResultStatus.FAILED
    assert result.failure_category is EvaluationFailureCategory.BUDGET_EXCEEDED
    assert result.token_usage.total_tokens == 120
    assert result.token_usage.measurement_state is MeasurementState.MEASURED
