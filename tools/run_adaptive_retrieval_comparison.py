"""M2 synthetic A/B and demos: actual Hybrid RAG, offline TF-IDF, mock routing.

No live BGE-M3, Qdrant, reranker, LLM, video or answer-quality claim.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dovideo.application.adaptive_retrieval import (
    AdaptiveRetrievalService, AdaptiveRetrievalSettings, capture_adaptive_retrieval, complexity_reason,
)
from dovideo.application.chunking import VideoChunkingService
from dovideo.application.context import VideoContextBuilder
from dovideo.application.evidence import EvidenceVerificationService
from dovideo.application.retrieval import VideoEvidenceRetrievalService
from dovideo.application.value_objects import MediaObservationBundle, AsrBranchOutcome, OcrBranchOutcome, TranscriptSpan, OcrObservation
from dovideo.domain import AnalysisEvidence
from dovideo.infrastructure.providers.local_embedding import LocalTfidfEmbeddingAdapter
from dovideo.infrastructure.providers.model import RetrievalPlannerModelAdapter
from dovideo.infrastructure.providers.summary import LocalChunkSummaryAdapter
from dovideo.presentation.composition import LocalRetrievalPlanner, InMemoryVectorIndex


class FixtureChat:
    """Query-only extractive mock, no access to annotations or source text."""
    def __init__(self, invalid=False):
        self.calls = 0
        self.invalid = invalid
    async def complete(self, messages, *, stage):
        self.calls += 1
        query = json.loads(messages[-1]["content"].split("Input as JSON:\n")[1])["question"]
        value = {"retrieval_route": "BOUNDED_MULTI_QUERY", "reason_code": complexity_reason(query),
                 "sub_queries": [q.strip() for q in query.split(";")]}
        if self.invalid: value["media_id"] = 999
        return json.dumps(value)


class CountingIntent(LocalRetrievalPlanner):
    def __init__(self): self.calls = []
    async def plan_retrieval(self, goal):
        self.calls.append(goal)
        return await super().plan_retrieval(goal)


async def compare(output: Path):
    if output.exists():
        raise ValueError("output already exists; preserve previous experiments")
    specification = json.loads((ROOT / "datasets/m2/adaptive-source.json").read_text(encoding="utf-8"))
    context = VideoContextBuilder().build("fixture://m2-cross-fragment-v1", "", MediaObservationBundle(
        asr=AsrBranchOutcome(observations=tuple(TranscriptSpan(start_ms=i * 8 * 60_000,
            end_ms=i * 8 * 60_000 + 60_000, text=t) for i, t in enumerate(specification["transcripts"])), attempted=15),
        ocr=OcrBranchOutcome(observations=(OcrObservation(timestamp_ms=48 * 60_000, text="Redis eviction policy expiration TTL", frame_ref="fixture-ttl.png"),), attempted=1),
    ), media_content_identity="synthetic-m2-cross-fragment-v1")
    embedding = LocalTfidfEmbeddingAdapter(max_features=2048).fit(specification["transcripts"])
    chunks = await VideoChunkingService(LocalChunkSummaryAdapter(), embedding).build(context.segments)
    by_minute = {s.start_ms // 60_000: s for s in context.segments}
    verifier = EvidenceVerificationService()
    cases = []
    for case in specification["cases"]:
        target_ids = {by_minute[i * 8].segment_id for i in case["targets"]}
        result = {"case_id": case["id"], "expected_route": case["route"], "target_count": len(target_ids), "arms": {}}
        for label, enabled in (("single", False), ("adaptive", True)):
            intent = CountingIntent()
            hybrid = VideoEvidenceRetrievalService(intent, embedding, InMemoryVectorIndex())
            chat = FixtureChat(case.get("invalidPlan", False))
            service = AdaptiveRetrievalService(hybrid, RetrievalPlannerModelAdapter(chat), AdaptiveRetrievalSettings(enabled=enabled))
            started = time.perf_counter()
            with capture_adaptive_retrieval() as trace:
                hits = await service.search(None, case["query"], chunks)
            latency = (time.perf_counter() - started) * 1000
            retrieved = {h.segment_id for h in hits}
            guard_results = []
            for hit in hits:
                evidence = AnalysisEvidence(timestamp_ms=hit.start_ms + 1000, source="ASR",
                    content=hit.transcript, claim=hit.transcript, source_revision=hit.source_revision,
                    segment_id=hit.segment_id, source_item_ids=hit.source_item_ids[:1])
                guard_results.append(verifier.supported(context, evidence))
            arm = {"retrieval_call_count": len(intent.calls), "routing_model_calls": chat.calls,
                "intent_model_calls": 0, "intent_port_calls": len(intent.calls), "tokens": "NOT_MEASURED",
                "candidate_count": len(hits), "target_hits": len(target_ids & retrieved),
                "target_recall": len(target_ids & retrieved) / len(target_ids) if target_ids else None,
                "irrelevant_candidates": len(retrieved - target_ids), "candidate_guard_passes": sum(guard_results),
                "answer_guard": "NOT_RUN", "measured_wall_latency_ms": round(latency, 3),
                "evidence_minutes": [h.start_ms // 60_000 for h in hits],
                "source_revision": context.source_revision,
                "route": "SINGLE_HYBRID", "coverage_status": "NOT_MEASURED"}
            if trace:
                observation = trace[0]
                arm.update(route=observation.decision.route.value, routing_reason=observation.decision.reason,
                    routing_fallback=observation.decision.fallback, coverage_status=observation.coverage_status,
                    subquery_candidate_counts=list(observation.subquery_candidate_counts))
            # Explicit synthetic demo artifact; never production telemetry.
            arm["executed_queries"] = intent.calls
            result["arms"][label] = arm
        cases.append(result)
    summary = {}
    for label in ("single", "adaptive"):
        arms = [c["arms"][label] for c in cases]
        recalls = [a["target_recall"] for a in arms if a["target_recall"] is not None]
        summary[label] = {"mean_target_recall": sum(recalls) / len(recalls),
            "total_retrieval_calls": sum(a["retrieval_call_count"] for a in arms),
            "total_routing_model_calls": sum(a["routing_model_calls"] for a in arms),
            "candidate_guard_passes": sum(a["candidate_guard_passes"] for a in arms),
            "candidate_count": sum(a["candidate_count"] for a in arms),
            "route_correct_count": sum(a["route"] == c["expected_route"] for a,c in zip(arms,cases))}
    report = {"classification": "SYNTHETIC", "dataset_version": specification["version"],
        "embedding": "offline LocalTfidfEmbeddingAdapter", "routing": "FixtureChat MOCK through real Provider DTO adapter",
        "dense": "actual local cosine ranking", "sparse": "actual BM25", "fusion": "actual RRF",
        "chunking": "unchanged VideoChunkingService sliding windows", "summary_provider": "LocalChunkSummaryAdapter",
        "live_bge_qdrant_llm_video": "NOT_RUN", "reranker": "OFF", "critic": "NOT_RUN",
        "coverage_semantics": "Candidate presence only; irrelevant/no-answer candidates can remain.",
        "summary": summary, "cases": cases}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(compare(parser.parse_args().output))
