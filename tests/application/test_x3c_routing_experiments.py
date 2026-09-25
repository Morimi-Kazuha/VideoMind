from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from dovideo.application.evaluation_contracts import (
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationResultStatus,
    EvaluationStrategy,
    MeasurementState,
    TokenUsageMeasurement,
    canonical_json,
)
from dovideo.application.evaluation_runner import (
    ConditionalPricingRate,
    EvaluationExecutionObservation,
    EvaluationPreflightError,
    EvaluationRunner,
    EvaluationRunnerConfig,
    EvaluationSourceArtifact,
    PricingCatalog,
    PricingEntry,
    PricingRateAvailability,
    RuleRouterEvaluationStrategy,
    calculate_cost,
)
from dovideo.application.model_routing import ModelRouteLane
from dovideo.application.routing_experiments import (
    DEFAULT_ROUTING_QUALITY_GATE_CONFIGURATION,
    DEFAULT_RULE_ROUTER_CONFIGURATION,
    RuleRouterConfiguration,
    RuleRouterInputError,
    RuleRouterRuntimeSignals,
    RuleRouterV1,
    RoutingQualityGateConfiguration,
    RoutingQualityGateV1,
    rule_router_signals_from_execution,
)
from dovideo.domain import VideoContext, VideoSegment


ROOT = Path(__file__).resolve().parents[2]
REVISION = "a" * 64
EXECUTION_INPUT = {
    "media_ref": "prepared/synthetic-video.mp4",
    "query": "Summarize this recording.",
    "mode": "GENERAL",
}


def _signals(**updates) -> RuleRouterRuntimeSignals:
    values = {
        "queryLengthChars": len(EXECUTION_INPUT["query"]),
        "mode": "GENERAL",
        "mediaDurationMs": 300_000,
        "segmentCount": 5,
        "chunkCount": 1,
        "asrAvailable": True,
        "ocrAvailable": False,
    }
    values.update(updates)
    return RuleRouterRuntimeSignals.model_validate(values)


def _artifact(
    *,
    duration_ms: int = 300_000,
    segment_count: int = 5,
    chunks: int = 1,
    asr: bool = True,
    ocr: bool = False,
) -> EvaluationSourceArtifact:
    segments = tuple(
        VideoSegment(
            start_ms=index * 60_000,
            end_ms=min((index + 1) * 60_000, duration_ms),
            transcript=("source speech" if asr else ""),
            ocr_texts=(("visible text",) if ocr else ()),
        )
        for index in range(segment_count)
    )
    context = VideoContext(
        source="prepared/synthetic-video.mp4",
        userGoal=EXECUTION_INPUT["query"],
        segments=segments,
        sourceRevision=REVISION,
    )
    return EvaluationSourceArtifact(
        mediaRef=EXECUTION_INPUT["media_ref"],
        sourceRevision=REVISION,
        context=context,
        chunks=tuple(object() for _ in range(chunks)),
    )


def _case() -> EvaluationCase:
    return EvaluationCase(
        caseId="synthetic-case",
        datasetVersion="golden-dataset-v1",
        mediaRef=EXECUTION_INPUT["media_ref"],
        sourceRevision=REVISION,
        query=EXECUTION_INPUT["query"],
        mode="GENERAL",
        queryCategory="DIRECT_SPEECH_FACT",
        requiredFacts=(
            {"factId": "fact-1", "description": "a gold-only fact"},
        ),
        referenceAnswer="gold-only answer",
        difficulty="HARD",
        toolBeneficial=True,
        criticSensitive=True,
    )


def _result(**updates) -> EvaluationCaseResult:
    values = {
        "runId": "synthetic-run",
        "caseId": "synthetic-case",
        "strategy": EvaluationStrategy.RULE_ROUTER,
        "trialIndex": 0,
        "mode": "GENERAL",
        "queryCategory": "DIRECT_SPEECH_FACT",
        "sourceRevision": REVISION,
        "status": EvaluationResultStatus.SUCCESSFUL,
        "deterministicMetrics": {
            "requiredFactExactCoverage": 0.90,
            "schemaValid": True,
            "modeSectionsValid": True,
            "evidenceGuardPass": True,
            "measurementState": MeasurementState.MEASURED,
        },
    }
    values.update(updates)
    return EvaluationCaseResult.model_validate(values)


def _router() -> RuleRouterV1:
    payload = json.loads(
        (ROOT / "configs/x3c/rule-router-v1.json").read_text(encoding="utf-8")
    )
    return RuleRouterV1(RuleRouterConfiguration.model_validate(payload))


def _gate() -> RoutingQualityGateV1:
    payload = json.loads(
        (ROOT / "configs/x3c/routing-quality-gate-v1.json").read_text(
            encoding="utf-8"
        )
    )
    return RoutingQualityGateV1(RoutingQualityGateConfiguration.model_validate(payload))


def _pricing_catalog() -> PricingCatalog:
    payload = json.loads(
        (ROOT / "configs/x3c/pricing-x3c-v1.json").read_text(encoding="utf-8")
    )
    return PricingCatalog.model_validate(payload)


def test_rule_router_is_deterministic_and_fast_boundaries_are_inclusive() -> None:
    router = _router()
    signals = _signals(
        queryLengthChars=120,
        mediaDurationMs=300_000,
        segmentCount=5,
        chunkCount=1,
    )
    assert router.route(signals) is ModelRouteLane.FAST
    assert router.route(signals) is router.route(signals)


@pytest.mark.parametrize(
    "updates",
    [
        {"queryLengthChars": 121},
        {"mode": "LEARNING"},
        {"mediaDurationMs": 300_001},
        {"segmentCount": 6},
        {"chunkCount": 2},
        {"asrAvailable": False},
        {"ocrAvailable": True},
        {"mediaDurationMs": None},
    ],
)
def test_rule_router_balanced_boundary_falls_back_for_non_small_runtime_state(updates) -> None:
    assert _router().route(_signals(**updates)) is ModelRouteLane.BALANCED


@pytest.mark.parametrize(
    "updates",
    [
        {"queryLengthChars": 300},
        {"mediaDurationMs": 1_800_000},
        {"segmentCount": 30},
        {"chunkCount": 6},
        {"mediaDurationMs": 900_000, "ocrAvailable": True},
    ],
)
def test_rule_router_deep_boundaries_are_inclusive(updates) -> None:
    assert _router().route(_signals(**updates)) is ModelRouteLane.DEEP


@pytest.mark.parametrize(
    "payload",
    [
        {"queryLengthChars": 0},
        {"queryLengthChars": 501},
        {"mediaDurationMs": -1},
        {"segmentCount": True},
        {"chunkCount": 1_000_001},
        {"mode": "AUTO"},
    ],
)
def test_rule_router_rejects_invalid_runtime_data(payload) -> None:
    with pytest.raises((RuleRouterInputError, ValueError)):
        _signals(**payload)


def test_rule_router_runtime_projection_rejects_gold_and_annotation_fields() -> None:
    case = _case()
    projected = case.execution_input()
    assert set(projected) == {"media_ref", "query", "mode"}
    assert "required_facts" not in projected
    assert "reference_answer" not in projected
    assert "difficulty" not in projected
    assert "query_category" not in projected
    with pytest.raises(RuleRouterInputError):
        rule_router_signals_from_execution(
            {**projected, "required_facts": ["gold-only fact"]}, _artifact()
        )
    with pytest.raises(RuleRouterInputError):
        rule_router_signals_from_execution(
            {**projected, "jev_suggestion": "DEEP"}, _artifact()
        )


def test_rule_router_derives_only_runtime_context_signals() -> None:
    artifact = _artifact(duration_ms=180_000, segment_count=3, chunks=1, asr=True, ocr=True)
    signals = rule_router_signals_from_execution(EXECUTION_INPUT, artifact)
    assert signals.query_length_chars == len(EXECUTION_INPUT["query"])
    assert signals.mode.value == "GENERAL"
    assert signals.media_duration_ms == 180_000
    assert signals.segment_count == 3
    assert signals.chunk_count == 1
    assert signals.asr_available is True
    assert signals.ocr_available is True
    assert _router().route(signals) is ModelRouteLane.BALANCED


def test_rule_router_evaluation_strategy_dispatches_only_to_selected_lane() -> None:
    class LaneAdapter:
        def __init__(self, lane: ModelRouteLane) -> None:
            self.lane = lane
            self.calls = 0

        async def execute(self, execution_input, **kwargs):
            self.calls += 1
            assert set(execution_input) == {"media_ref", "query", "mode"}
            assert kwargs["strategy"] is EvaluationStrategy.RULE_ROUTER
            return EvaluationExecutionObservation(model=f"model-{self.lane.value.lower()}")

    adapters = {lane: LaneAdapter(lane) for lane in ModelRouteLane}
    strategy = RuleRouterEvaluationStrategy(adapters, router=_router())
    observation = asyncio.run(
        strategy.execute(
            EXECUTION_INPUT,
            artifact=_artifact(),
            trial_index=0,
            timeout_seconds=10,
        )
    )
    assert adapters[ModelRouteLane.FAST].calls == 1
    assert adapters[ModelRouteLane.BALANCED].calls == 0
    assert adapters[ModelRouteLane.DEEP].calls == 0
    assert observation.route_decision.suggested_lane is ModelRouteLane.FAST
    assert observation.route_decision.resolved_lane is ModelRouteLane.FAST
    assert observation.route_decision.resolved_model_id == "model-fast"


def test_runner_requires_explicit_rule_router_strategy_before_preflight(tmp_path) -> None:
    runner = EvaluationRunner(adapter=object(), repo_root=tmp_path)
    with pytest.raises(EvaluationPreflightError, match="explicit evaluation-only"):
        runner.preflight(
            EvaluationDataset(cases=(_case(),)),
            EvaluationRunnerConfig(strategy=EvaluationStrategy.RULE_ROUTER),
        )


def test_rule_router_artifact_digest_is_stable_and_matches_code_defaults() -> None:
    router = _router()
    second = _router()
    assert router.digest == second.digest
    assert router.digest == DEFAULT_RULE_ROUTER_CONFIGURATION.digest
    assert router.digest == "358089a27926ba86653b6ea2330696594c828e31d06ae7fdda53ec3bf0f737d9"


def test_routing_quality_gate_passes_when_every_preregistered_condition_passes() -> None:
    assert _gate().passes(_result()) is True


@pytest.mark.parametrize(
    "updates",
    [
        {"status": EvaluationResultStatus.PLANNED},
        {
            "deterministicMetrics": {
                "requiredFactExactCoverage": 0.90,
                "schemaValid": False,
                "modeSectionsValid": True,
                "evidenceGuardPass": True,
                "measurementState": MeasurementState.MEASURED,
            }
        },
        {
            "deterministicMetrics": {
                "requiredFactExactCoverage": 0.90,
                "schemaValid": True,
                "modeSectionsValid": False,
                "evidenceGuardPass": True,
                "measurementState": MeasurementState.MEASURED,
            }
        },
        {
            "deterministicMetrics": {
                "requiredFactExactCoverage": 0.90,
                "schemaValid": True,
                "modeSectionsValid": True,
                "evidenceGuardPass": False,
                "measurementState": MeasurementState.MEASURED,
            }
        },
        {
            "deterministicMetrics": {
                "requiredFactExactCoverage": 0.899,
                "schemaValid": True,
                "modeSectionsValid": True,
                "evidenceGuardPass": True,
                "measurementState": MeasurementState.MEASURED,
            }
        },
    ],
)
def test_routing_quality_gate_fails_each_individual_condition(updates) -> None:
    assert _gate().passes(_result(**updates)) is False


@pytest.mark.parametrize("coverage", [0.899, 0.90, 0.901])
def test_routing_quality_gate_coverage_below_at_and_above_threshold(coverage) -> None:
    assert _gate().passes(_result(
        deterministicMetrics={
            "requiredFactExactCoverage": coverage,
            "schemaValid": True,
            "modeSectionsValid": True,
            "evidenceGuardPass": True,
            "measurementState": MeasurementState.MEASURED,
        }
    )) is (coverage >= 0.90)


def test_routing_quality_gate_missing_required_metric_fails_closed() -> None:
    result = _result(
        deterministicMetrics={
            "schemaValid": True,
            "modeSectionsValid": True,
            "evidenceGuardPass": True,
            "measurementState": MeasurementState.MEASURED,
        }
    )
    assert result.deterministic_metrics.required_fact_exact_coverage is None
    assert _gate().passes(result) is False


def test_routing_quality_gate_artifact_digest_is_stable_and_matches_code_defaults() -> None:
    gate = _gate()
    second = _gate()
    assert gate.digest == second.digest
    assert gate.digest == DEFAULT_ROUTING_QUALITY_GATE_CONFIGURATION.digest
    assert gate.digest == "e90a1aa26e2bba6187902928e31b92356e80f759cfeb2533b13818322258be9c"


def test_pricing_catalog_has_authoritative_model_identity_and_missing_scalar_rates() -> None:
    catalog = _pricing_catalog()
    flash = catalog.lookup("deepseek", "deepseek-flash")
    deep = catalog.lookup("deepseek", "deepseek-v4-pro")
    jev = catalog.lookup("openrouter", "typesafe/jev-1.13")
    assert flash is not None and deep is not None and jev is not None
    assert flash.rate_availability is PricingRateAvailability.CONDITIONAL
    assert deep.rate_availability is PricingRateAvailability.CONDITIONAL
    assert len(flash.conditional_rates) == 4
    assert len(deep.conditional_rates) == 4
    assert flash.input_rate_per_million is None
    assert deep.output_rate_per_million is None
    assert jev.rate_availability is PricingRateAvailability.AVAILABLE
    assert jev.input_rate_per_million == 0.042
    assert jev.output_rate_per_million == 0.0
    assert catalog.lookup("deepseek", "unlisted-model") is None


def test_pricing_rate_models_reject_inconsistent_available_or_conditional_data() -> None:
    with pytest.raises(ValueError, match="scalar rate pair"):
        PricingEntry(
            provider="test",
            model="available-without-price",
            inputRatePerMillion=None,
            outputRatePerMillion=None,
        )
    with pytest.raises(ValueError, match="conditional rate rows"):
        PricingEntry(
            provider="test",
            model="conditional-with-scalar",
            inputRatePerMillion=1.0,
            outputRatePerMillion=2.0,
            rateAvailability=PricingRateAvailability.CONDITIONAL,
            conditionalRates=(
                ConditionalPricingRate(
                    period="PEAK",
                    inputCache="MISS",
                    inputRatePerMillion=1.0,
                    outputRatePerMillion=2.0,
                ),
            ),
        )


def test_pricing_catalog_calculates_only_when_input_output_split_and_scalar_price_exist() -> None:
    catalog = PricingCatalog(
        pricingVersion="pricing-x3c-test",
        entries=(
            PricingEntry(
                provider="test-provider",
                model="test-model",
                inputRatePerMillion=0.5,
                outputRatePerMillion=1.25,
                currency="USD",
            ),
        ),
    )
    split = calculate_cost(
        TokenUsageMeasurement(
            inputTokens=100,
            outputTokens=50,
            totalTokens=150,
            measurementState=MeasurementState.MEASURED,
        ),
        catalog,
        provider="test-provider",
        model="test-model",
    )
    assert split.calculated_cost == pytest.approx(0.0001125)
    assert split.currency == "USD"
    assert split.pricing_version == "pricing-x3c-test"

    total_only = calculate_cost(
        TokenUsageMeasurement(
            totalTokens=150,
            measurementState=MeasurementState.MEASURED,
        ),
        catalog,
        provider="test-provider",
        model="test-model",
    )
    assert total_only.calculated_cost is None
    assert total_only.measurement_state is MeasurementState.NOT_MEASURED


def test_not_available_price_never_produces_a_calculated_cost() -> None:
    catalog = PricingCatalog(
        pricingVersion="pricing-missing-test",
        entries=(
            PricingEntry(
                provider="test-provider",
                model="unpriced-model",
                inputRatePerMillion=None,
                outputRatePerMillion=None,
                rateAvailability=PricingRateAvailability.NOT_AVAILABLE,
            ),
        ),
    )
    cost = calculate_cost(
        TokenUsageMeasurement(
            inputTokens=10,
            outputTokens=20,
            totalTokens=30,
            measurementState=MeasurementState.MEASURED,
        ),
        catalog,
        provider="test-provider",
        model="unpriced-model",
    )
    assert cost.calculated_cost is None
    assert cost.currency is None


def test_jev_provider_reported_cost_stays_separate_from_catalog_calculation() -> None:
    catalog = _pricing_catalog()
    cost = calculate_cost(
        TokenUsageMeasurement(
            inputTokens=100,
            outputTokens=50,
            totalTokens=150,
            measurementState=MeasurementState.MEASURED,
        ),
        catalog,
        provider="openrouter",
        model="typesafe/jev-1.13",
        provider_reported_cost=0.000019152,
    )
    assert cost.provider_reported_cost == 0.000019152
    assert cost.calculated_cost == pytest.approx(0.0000042)
    assert cost.provider_reported_cost != cost.calculated_cost
    assert cost.currency == "USD"
    assert cost.pricing_version == "pricing-x3c-v1"


def test_pricing_catalog_digest_source_date_and_version_identity_are_stable() -> None:
    first = _pricing_catalog()
    second = _pricing_catalog()
    assert first.pricing_version == "pricing-x3c-v1"
    assert first.digest == second.digest
    assert first.digest == "d6d6902af7e0d4e9f9906ec21768fe27eb8cf5aa69168d4dd7dce4127b653a25"
    assert first.canonical_json() == canonical_json(first)
    assert all(entry.source_date.isoformat() == "2026-09-25" for entry in first.entries)
    jev = first.lookup("openrouter", "typesafe/jev-1.13")
    assert jev is not None and jev.effective_date is None
