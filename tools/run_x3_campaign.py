"""Run one bounded X3 campaign against already prepared canonical media.

Each case/trial gets a fresh durable SQLite checkpoint. The prepared context
and chunks are copied from the canonical MySQL checkpoint; no ingestion or
media extraction is part of the measured execution.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import traceback
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from run_r4_live import load_local_environment

from dovideo.application import AgentCheckpointService
from dovideo.application.evaluation_campaign import CAMPAIGN_STRATEGIES, EvaluationCampaignRunner
from dovideo.application.evaluation_contracts import (
    CostMeasurement, EvaluationStrategy, MeasurementState, RoutingMeasurement,
    TokenStageUsage, TokenUsageMeasurement, read_dataset,
)
from dovideo.application.evaluation_runner import (
    AdapterEvaluationStrategy, AgentLoopEvaluationAdapter, EvaluationExecutionObservation,
    EvaluationObservedFailure,
    EvaluationRunner, EvaluationRunnerConfig, EvaluationSourceArtifact,
    MappingEvaluationSourceResolver, PricingCatalog, RuleRouterEvaluationStrategy,
)
from dovideo.application.model_routing import ModelRouteLane
from dovideo.application.value_objects import TaskKey
from dovideo.domain import AnalysisMode
from dovideo.infrastructure import create_r2_infrastructure
from dovideo.infrastructure.model_routing import ModelRoutingProductionSettings
from dovideo.infrastructure.persistence import (
    CheckpointRepository, InMemoryHotCheckpointCache, SqliteCheckpointStore,
)
from dovideo.infrastructure.providers.jev import JevRouterSettings, JevTransport
from dovideo.infrastructure.providers.config import ProviderConfig
from dovideo.infrastructure.r4_runtime import R4AgentTelemetry, create_r4_provider_stack
from dovideo.infrastructure.redis_observability import RedisAgentTelemetry
from dovideo.infrastructure.x1_config import X1ToolCallingSettings


ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "datasets" / "x3" / "golden-dataset-v2.json"
PREPARED_MARKER = ROOT / "work" / "x3-v2-prepared-state.json"
PRICING_PATH = ROOT / "configs" / "x3c" / "pricing-x3c-openrouter-v1.json"
EXPECTED_PROFILES = {
    ModelRouteLane.FAST: "bd605d01eade256e6d3412b43fa4b376444c08e24cfb013027d45eb67b4de55b",
    ModelRouteLane.BALANCED: "44dae58f205e22f929484dd5ddce4580653e356c70bb61122fcb65c56edfa4e0",
    ModelRouteLane.DEEP: "8f03c705bb0a084982e1339d958a80e6bf0d70bbf96e9051d65ddff5bf0932f2",
}


def frozen_routing_settings(base_model: str) -> ModelRoutingProductionSettings:
    if base_model != "deepseek/deepseek-v4.1-flash":
        raise RuntimeError("frozen BALANCED OpenRouter model changed")
    settings = ModelRoutingProductionSettings(
        enabled=True,
        confidence_threshold=0.70,
        fast_model="deepseek/deepseek-v4.1-flash",
        balanced_model=base_model,
        deep_model="deepseek/deepseek-v4-pro-0813",
        fast_reasoning_effort="none",
        deep_reasoning_effort="max",
        deep_max_tokens=65536,
        jev=JevRouterSettings(
            transport=JevTransport.OPENROUTER,
            openrouter_api_key=os.environ.get("DOVIDEO_OPENROUTER_API_KEY"),
        ),
    )
    settings.jev.validate_for_use()
    config = ProviderConfig.from_environment(required=True)
    if config is None or config.transport != "openrouter" or config.provider_only != ("nextbit/fp8",):
        raise RuntimeError("frozen NextBit OpenRouter provider pin changed")
    if config.provider_data_collection != "deny" or config.provider_zdr is not True:
        raise RuntimeError("frozen OpenRouter data policy changed")
    for lane, digest in EXPECTED_PROFILES.items():
        if settings.effective_profile_identity(lane, provider_config=config).fingerprint != digest:
            raise RuntimeError(f"frozen {lane.value} model profile changed")
    return settings


def _usage(records: list[dict]) -> TokenUsageMeasurement:
    def stage(rows: list[dict]) -> TokenStageUsage:
        if not rows:
            return TokenStageUsage(measurementState=MeasurementState.NOT_MEASURED)
        def total(field: str) -> int | None:
            values = [row[field] for row in rows]
            if not all(isinstance(value, (int, float)) and
                       not isinstance(value, bool) and float(value).is_integer()
                       for value in values):
                return None
            return sum(int(value) for value in values)
        return TokenStageUsage(
            inputTokens=total("inputTokens"), outputTokens=total("outputTokens"),
            totalTokens=total("totalTokens"), providerReported=True,
            measurementState=MeasurementState.MEASURED,
        )

    stages = {name: stage([row for row in records if row["stage"] == name])
              for name in ("PLANNER", "EXECUTOR", "CRITIC", "ROUTER")}
    all_usage = stage(records)
    return TokenUsageMeasurement(
        inputTokens=all_usage.input_tokens, outputTokens=all_usage.output_tokens,
        totalTokens=all_usage.total_tokens, providerReported=True if records else None,
        measurementState=all_usage.measurement_state,
        planner=stages["PLANNER"], executor=stages["EXECUTOR"],
        critic=stages["CRITIC"], router=stages["ROUTER"],
    )


class IsolatedR4Adapter:
    """Construct one fresh production AgentLoop per case/trial execution."""

    def __init__(self, infra, settings, media_id, prepared_chunks, work_root, lane):
        self.infra = infra
        self.settings = settings
        self.media_id = media_id
        self.prepared_chunks = prepared_chunks
        self.work_root = Path(work_root)
        self.lane = lane

    async def execute(self, execution_input, *, artifact, strategy, trial_index, timeout_seconds):
        del timeout_seconds
        if artifact.context is None:
            raise RuntimeError("prepared context is missing")
        execution_dir = self.work_root / strategy.value / uuid4().hex
        execution_dir.mkdir(parents=True, exist_ok=False)
        sqlite = SqliteCheckpointStore(execution_dir / "checkpoint.sqlite3")
        checkpoint = AgentCheckpointService(
            CheckpointRepository(sqlite, InMemoryHotCheckpointCache())
        )
        provider = None
        trace_id = None
        key = None
        records: list[dict] = []
        try:
            await checkpoint.save_context(self.media_id, artifact.context)
            await checkpoint.save_chunks(self.media_id, self.prepared_chunks)
            telemetry = R4AgentTelemetry(RedisAgentTelemetry(self.infra.redis_client))
            goal = str(execution_input["query"])
            mode = AnalysisMode.from_request(str(execution_input["mode"]))
            key = TaskKey(self.media_id, goal, mode)
            trace_id = telemetry.start(key)
            token = telemetry.bind(key)
            try:
                provider = create_r4_provider_stack(
                    checkpoint, self.infra.vector_index, telemetry, None,
                    routing_settings=self.settings,
                    tool_settings=X1ToolCallingSettings(enabled=False),
                )
                loop = provider.agent_loop if self.lane is None else provider.agent_loop.lane_agent_loops[self.lane]
                model = None if self.lane is None else provider.resolved_model_ids[self.lane]
                bridge = AgentLoopEvaluationAdapter(
                    loop, media_id_resolver=lambda _ref: self.media_id,
                    provider="openrouter", model=model,
                )
                with telemetry.capture_chat_usage() as records:
                    observation = await bridge.execute(
                        execution_input, artifact=artifact, strategy=strategy,
                        trial_index=trial_index, timeout_seconds=900.0,
                    )
                decision = await checkpoint.load_model_routing(key) if self.lane is None else None
                resolved_lane = self.lane if decision is None else decision.lane
                if resolved_lane is None:
                    raise RuntimeError("Jev execution has no durable route decision")
                route = RoutingMeasurement(
                    suggestedLane=(None if decision is None else decision.suggested_lane),
                    resolvedLane=resolved_lane,
                    confidence=(None if decision is None else decision.confidence),
                    fallback=(False if decision is None else decision.fallback_used),
                    reasonCode=(None if decision is None else decision.reason_code),
                    resolvedModelId=provider.resolved_model_ids[resolved_lane],
                    measurementState=MeasurementState.MEASURED,
                )
                cost_values = [row["providerReportedCost"] for row in records]
                measured_cost = (
                    sum(float(value) for value in cost_values)
                    if cost_values and all(value is not None for value in cost_values)
                    else None
                )
                cost = (
                    CostMeasurement(providerReportedCost=measured_cost, currency="USD",
                                    measurementState=MeasurementState.MEASURED)
                    if measured_cost is not None else CostMeasurement()
                )
                metadata = {
                    "traceId": trace_id, "strategy": strategy.value,
                    "trialIndex": trial_index, "route": route.model_dump(mode="json", by_alias=True),
                    "providerUsage": records, "providerCostComplete": measured_cost is not None,
                }
                (execution_dir / "measurement.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
                )
                return replace(
                    observation, route_decision=route, token_usage=_usage(records),
                    cost=cost, resolved_model=provider.resolved_model_ids[resolved_lane],
                    model=provider.resolved_model_ids[resolved_lane],
                )
            finally:
                telemetry.reset(token)
        except Exception as error:
            (execution_dir / "failure.json").write_text(json.dumps({
                "traceId": trace_id, "strategy": strategy.value,
                "trialIndex": trial_index, "errorType": type(error).__name__,
                "providerUsage": records,
                "stack": [
                    {"file": Path(frame.filename).name, "line": frame.lineno,
                     "function": frame.name}
                    for frame in traceback.extract_tb(error.__traceback__)[-12:]
                ],
            }, sort_keys=True, indent=2), encoding="utf-8")
            decision = None
            if self.lane is None and key is not None:
                try:
                    decision = await checkpoint.load_model_routing(key)
                except Exception:
                    pass
            failure_lane = self.lane if decision is None else decision.lane
            failure_route = (
                RoutingMeasurement(
                    suggestedLane=(None if decision is None else decision.suggested_lane),
                    resolvedLane=failure_lane,
                    confidence=(None if decision is None else decision.confidence),
                    fallback=(None if decision is None else decision.fallback_used),
                    reasonCode=(None if decision is None else decision.reason_code),
                    resolvedModelId=self.settings.model_for(failure_lane),
                    measurementState=MeasurementState.MEASURED,
                ) if failure_lane is not None else RoutingMeasurement()
            )
            raise EvaluationObservedFailure(
                error,
                EvaluationExecutionObservation(
                    token_usage=_usage(records), route_decision=failure_route,
                    resolved_model=(None if failure_lane is None else self.settings.model_for(failure_lane)),
                ),
            ) from error
        finally:
            if provider is not None:
                await provider.close()
            sqlite.close()


def _clean_sha() -> str:
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
    if status.strip():
        raise RuntimeError("benchmark requires a clean committed working tree")
    return sha


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", help="one case ID for a bounded smoke")
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--artifact-root", default=str(ROOT / "work" / "x3-campaigns"))
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--diagnostic-one", action="store_true")
    args = parser.parse_args()
    load_local_environment()
    os.environ["DOVIDEO_MODEL_TRANSPORT"] = "openrouter"
    os.environ["DOVIDEO_MODEL_PROVIDER_TAG"] = "nextbit/fp8"
    os.environ["DOVIDEO_BALANCED_MODEL"] = "deepseek/deepseek-v4.1-flash"
    os.environ["DOVIDEO_FAST_MODEL"] = "deepseek/deepseek-v4.1-flash"
    os.environ["DOVIDEO_DEEP_MODEL"] = "deepseek/deepseek-v4-pro-0813"
    freeze_sha = _clean_sha()
    dataset = read_dataset(DATASET_PATH)
    pricing = PricingCatalog.model_validate_json(PRICING_PATH.read_text(encoding="utf-8"))
    marker = json.loads(PREPARED_MARKER.read_text(encoding="utf-8"))
    if marker.get("status") != "PREPARED" or marker.get("sourceRevision") != dataset.source_revision_set[0]:
        raise RuntimeError("canonical prepared media does not match dataset v2")
    infra = create_r2_infrastructure()
    try:
        await infra.initialize()
        media_id = int(marker["mediaId"])
        media = await infra.media_repository.get(media_id)
        if media is None or media.content_hash != marker["contentHash"]:
            raise RuntimeError("prepared media identity mismatch")
        durable = AgentCheckpointService(infra.checkpoint_repository)
        context = await durable.load_context(media_id)
        chunks = await durable.load_chunks(media_id)
        if context is None or not chunks or context.source_revision != dataset.source_revision_set[0]:
            raise RuntimeError("canonical context or chunks unavailable")
        if tuple(chunk.chunk_id for chunk in chunks) != tuple(marker["chunkIds"]):
            raise RuntimeError("prepared chunk identities changed")
        from dovideo.infrastructure.providers.config import ProviderConfig
        settings = frozen_routing_settings(ProviderConfig.from_environment(required=True).model)
        output = Path(args.artifact_root) / args.campaign_id
        work_root = output / "executions"
        source_ref = dataset.cases[0].media_ref
        artifact = EvaluationSourceArtifact(
            mediaRef=source_ref, sourceRevision=context.source_revision,
            context=context, chunks=chunks, artifactId=f"media-{media_id}-x2-a-v3",
        )
        resolver = MappingEvaluationSourceResolver({source_ref: artifact})
        adapters = {
            lane: IsolatedR4Adapter(infra, settings, media_id, chunks, work_root, lane)
            for lane in ModelRouteLane
        }
        jev_adapter = IsolatedR4Adapter(infra, settings, media_id, chunks, work_root, None)
        if args.diagnostic_one:
            case = dataset.case(args.case or "long-general-001")
            try:
                await adapters[ModelRouteLane.FAST].execute(
                    case.execution_input(), artifact=artifact,
                    strategy=EvaluationStrategy.ALWAYS_FAST, trial_index=0,
                    timeout_seconds=900.0,
                )
                print(json.dumps({"status": "DIAGNOSTIC_EXECUTED"}))
            except Exception as error:
                print(json.dumps({"status": "DIAGNOSTIC_FAILED",
                                  "errorType": type(error).__name__,
                                  "artifact": str(work_root)}))
            return
        strategies = {
            EvaluationStrategy.ALWAYS_FAST: AdapterEvaluationStrategy(EvaluationStrategy.ALWAYS_FAST, adapters[ModelRouteLane.FAST]),
            EvaluationStrategy.ALWAYS_BALANCED: AdapterEvaluationStrategy(EvaluationStrategy.ALWAYS_BALANCED, adapters[ModelRouteLane.BALANCED]),
            EvaluationStrategy.ALWAYS_DEEP: AdapterEvaluationStrategy(EvaluationStrategy.ALWAYS_DEEP, adapters[ModelRouteLane.DEEP]),
            EvaluationStrategy.RULE_ROUTER: RuleRouterEvaluationStrategy(adapters),
            EvaluationStrategy.JEV_ROUTER: AdapterEvaluationStrategy(EvaluationStrategy.JEV_ROUTER, jev_adapter),
        }
        runners = {
            name: EvaluationRunner(strategy=strategies[name], source_resolver=resolver,
                                   pricing_catalog=pricing, repo_root=ROOT)
            for name in CAMPAIGN_STRATEGIES
        }
        config = EvaluationRunnerConfig(
            trialCount=1, caseFilter=() if args.case is None else (args.case,),
            pricingVersion=pricing.pricing_version, toolsEnabled=False,
            criticEnabled=True,
        )
        campaign = EvaluationCampaignRunner(runners, artifact_root=args.artifact_root,
                                            campaign_id=args.campaign_id)
        if args.preflight_only:
            for name in CAMPAIGN_STRATEGIES:
                selected = config.model_copy(update={
                    "strategy": name, "artifact_output": str(output / name.value),
                })
                runners[name].preflight(dataset, selected)
            print(json.dumps({"status": "PREFLIGHT_PASS", "sha": freeze_sha,
                              "datasetDigest": dataset.dataset_digest,
                              "sourceRevision": context.source_revision,
                              "mediaId": media_id, "chunkCount": len(chunks)}))
            return
        result = await campaign.run(dataset, config)
        (output / "freeze.json").write_text(json.dumps({
            "sha": freeze_sha, "datasetDigest": dataset.dataset_digest,
            "sourceRevision": context.source_revision,
            "modelProfileFingerprints": {lane.value: digest for lane, digest in EXPECTED_PROFILES.items()},
            "jevModel": settings.jev.model, "pricingDigest": pricing.digest,
            "qualityGateDigest": campaign._gate.digest,
            "ruleRouterDigest": strategies[EvaluationStrategy.RULE_ROUTER].router_digest,
        }, sort_keys=True, indent=2), encoding="utf-8")
        print(json.dumps({"campaignId": result.campaign_id, "status": result.status,
                          "runs": [run.run.run_id for run in result.runs],
                          "oracle": result.oracle, "artifact": str(result.artifact_path)}))
    finally:
        infra.close()


if __name__ == "__main__":
    asyncio.run(main())
