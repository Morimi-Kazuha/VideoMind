"""Reproducible retrieval comparison through the existing X3 runner/metrics.

Fixtures and embedding/summary/planner are deterministic OFFLINE components.
This measures synthetic retrieval behavior, not BGE-M3 or live reranker quality.
The baseline is loaded from its frozen Git source only in this evaluation tool.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dovideo.application.chunking import VideoChunkingService
from dovideo.application.context import VideoContextBuilder
from dovideo.application.evaluation_contracts import EvaluationCase, EvaluationDataset, ExpectedEvidenceRef, ExpectedTemporalRegion, read_dataset, dataset_to_json
from dovideo.application.evaluation_runner import EvaluationRunner, EvaluationRunnerConfig, EvaluationSourceArtifact, MappingEvaluationSourceResolver, RetrievalEvaluationAdapter
from dovideo.application.retrieval import VideoEvidenceRetrievalService
from dovideo.application.value_objects import AsrBranchOutcome, OcrBranchOutcome, MediaObservationBundle, TranscriptSpan, OcrObservation
from dovideo.infrastructure.providers.local_embedding import LocalTfidfEmbeddingAdapter
from dovideo.infrastructure.providers.summary import LocalChunkSummaryAdapter
from dovideo.presentation.composition import InMemoryVectorIndex, LocalRetrievalPlanner

BASELINE_SHA = "c7c02707911c8cb823d7f7a46c328469689256b7"


def prepared_fixture(dataset_version: str = "retrieval-focused-v2"):
    if dataset_version not in {"retrieval-focused-v1", "retrieval-focused-v2"}:
        raise ValueError("unsupported retrieval fixture version")
    raw = json.loads((ROOT / f"datasets/x3/{dataset_version}-source.json").read_text(encoding="utf-8"))
    asr = tuple(TranscriptSpan(start_ms=i * 60_000, end_ms=(i + 1) * 60_000, text=text)
                for i, text in enumerate(raw["transcripts"]))
    ocr = tuple(OcrObservation(timestamp_ms=item["minute"] * 60_000, text=item["text"], frame_ref=f"synthetic-{item['minute']}.png")
                for item in raw["ocr"])
    context = VideoContextBuilder().build(raw["mediaRef"], "", MediaObservationBundle(
        asr=AsrBranchOutcome(observations=asr, attempted=1),
        ocr=OcrBranchOutcome(observations=ocr, attempted=len(ocr)),
    ), media_content_identity="synthetic-" + dataset_version)
    by_minute = {s.start_ms // 60_000: s for s in context.segments}
    cases = []
    for specification in raw["cases"]:
        segments = [by_minute[minute] for minute in specification["targets"]]
        source_type = specification.get("sourceType")
        refs = tuple(ExpectedEvidenceRef(source_revision=context.source_revision,
            segment_id=s.segment_id, match_level="SEGMENT", source_type=source_type)
            for s in segments)
        regions = tuple(ExpectedTemporalRegion(start_ms=s.start_ms, end_ms=s.end_ms, source_type=source_type) for s in segments)
        cases.append(EvaluationCase(case_id=specification["id"], dataset_version=dataset_version,
            media_ref=raw["mediaRef"], source_revision=context.source_revision,
            query=specification["query"], mode="GENERAL", query_category="EVIDENCE_HEAVY",
            expected_evidence_refs=refs, expected_temporal_regions=regions, tags=(specification["category"],)))
    dataset = EvaluationDataset(dataset_version=dataset_version, cases=tuple(cases),
        target_case_count=len(cases), completeness="COMPLETE", data_classification="SYNTHETIC",
        description="Twelve synthetic retrieval probes. Offline TF-IDF, local summary/planner; no pretrained/live quality claim.")
    artifact = EvaluationSourceArtifact(media_ref=raw["mediaRef"], source_revision=context.source_revision,
                                        context=context, source_types=("ASR", "OCR"))
    return dataset, artifact


def frozen_module(path: str, name: str, sha: str):
    source = subprocess.run(["git", "-c", f"safe.directory={ROOT.as_posix()}", "show", f"{sha}:{path}"],
                            cwd=ROOT, capture_output=True, check=True).stdout.decode("utf-8")
    module = ModuleType(name)
    module.__package__ = name.rpartition(".")[0]
    sys.modules[name] = module
    exec(compile(source, f"git:{sha}:{path}", "exec"), module.__dict__)
    return module


def baseline_services(sha: str):
    provenance = frozen_module("src/dovideo/domain/provenance.py", "dovideo.domain._eval_frozen_provenance", sha)
    chunking = frozen_module("src/dovideo/application/chunking.py", "dovideo.application._eval_frozen_chunking", sha)
    retrieval = frozen_module("src/dovideo/application/retrieval.py", "dovideo.application._eval_frozen_retrieval", sha)
    chunking.CHUNKING_CONTRACT_VERSION = provenance.CHUNKING_CONTRACT_VERSION
    chunking.chunk_id_for = provenance.chunk_id_for
    return chunking.VideoChunkingService, retrieval.VideoEvidenceRetrievalService


class StructuralMetrics:
    def __init__(self):
        self.counts = Counter()
        self.observations = {}
    def increment(self, name, amount=1):
        self.counts[name] += amount
    def observe(self, name, value):
        self.observations.setdefault(name, []).append(value)


def aggregate(results):
    measurements = [r.deterministic_metrics for r in results]
    def mean(values):
        known = [value for value in values if value is not None]
        return sum(known) / len(known) if known else "NOT_MEASURED"
    out = {}
    for k in (1, 3, 5):
        out[f"recall@{k}"] = mean([(m.retrieval_recall_at_k_by_k or {}).get(str(k)) for m in measurements])
        out[f"precision@{k}"] = mean([(m.retrieval_precision_at_k_by_k or {}).get(str(k)) for m in measurements])
    for name in ("mrr", "temporal_hit", "temporal_coverage"):
        out[name] = mean([getattr(m, name) for m in measurements])
    return out


async def compare(output: Path, baseline_sha: str = BASELINE_SHA, dataset_version: str = "retrieval-focused-v2"):
    dataset, artifact = prepared_fixture(dataset_version)
    dataset_path = ROOT / f"datasets/x3/{dataset_version}.json"
    if dataset_path.exists():
        if read_dataset(dataset_path) != dataset:
            raise ValueError("versioned dataset differs from source fixture; do not overwrite")
    else:
        dataset_path.write_text(dataset_to_json(dataset) + "\n", encoding="utf-8")
    if output.exists() and any(output.iterdir()):
        raise ValueError("output must be empty; preserve previous experiments")
    baseline_chunking, baseline_retrieval = baseline_services(baseline_sha)
    results = {}
    for label, chunking_type, retrieval_type in (("baseline", baseline_chunking, baseline_retrieval),
                                               ("final", VideoChunkingService, VideoEvidenceRetrievalService)):
        context = artifact.context
        embedding = LocalTfidfEmbeddingAdapter(max_features=2048).fit(
            [s.transcript + " " + " ".join(s.ocr_texts) for s in context.segments])
        metrics = StructuralMetrics()
        retrieval = retrieval_type(LocalRetrievalPlanner(), embedding, InMemoryVectorIndex(), metrics)
        chunking = chunking_type(LocalChunkSummaryAdapter(), embedding, metrics)
        adapter = RetrievalEvaluationAdapter(chunking, retrieval)
        runner = EvaluationRunner(adapter, MappingEvaluationSourceResolver({artifact.media_ref: artifact}), repo_root=ROOT)
        outcome = await runner.run(dataset, EvaluationRunnerConfig(retrieval_only=True,
            artifact_output=str(output / label), tools_enabled=False, critic_enabled=False))
        if outcome.run.failed_count or outcome.run.excluded_count or outcome.run.not_run_count:
            raise RuntimeError("comparison execution failed; inspect X3 artifacts")
        categories = sorted({case.tags[0] for case in dataset.cases})
        results[label] = {
            "cases": len(outcome.results), "metrics": aggregate(outcome.results),
            "categories": {category: aggregate([r for r in outcome.results if dataset.case(r.case_id).tags[0] == category])
                           for category in categories},
            "perCase": {r.case_id: r.deterministic_metrics.model_dump(mode="json", by_alias=True) for r in outcome.results},
            "structuralMetrics": {"counts": dict(metrics.counts), "observations": metrics.observations},
        }
    report = {"classification": "SYNTHETIC", "datasetDigest": dataset.dataset_digest,
              "baselineSha": baseline_sha, "embedding": "offline LocalTfidfEmbeddingAdapter; BGE-M3 NOT_RUN",
              "reranker": "disabled; live cross-encoder NOT_RUN", "noAnswer": "NOT_MEASURED",
              **results}
    (output / "comparison.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({label: results[label]["metrics"] for label in results}, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "work" / ("retrieval-comparison-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")))
    parser.add_argument("--baseline-sha", default=BASELINE_SHA)
    parser.add_argument("--dataset-version", choices=("retrieval-focused-v1", "retrieval-focused-v2"), default="retrieval-focused-v2")
    args = parser.parse_args()
    asyncio.run(compare(args.output, args.baseline_sha, args.dataset_version))


if __name__ == "__main__":
    main()
