"""Sanitized retrieval quality closure. No production ranking changes.

Environment files are optional and explicit; inherited process settings win.
Only provider identity, ranks and numerical scores may be persisted.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import secrets
import time
from types import MethodType
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from dovideo.infrastructure.providers.embedding import OpenAICompatibleEmbeddingAdapter
from dovideo.infrastructure.providers.reranker import RerankerConfig, SiliconFlowRerankerAdapter
from dovideo.infrastructure.vector.qdrant import QdrantVectorIndex
from dovideo.application.ports.retrieval import RerankerDocument
from dovideo.presentation.composition import embedding_provider_config_from_environment
from dovideo.application.chunking import VideoChunkingService
from dovideo.application.retrieval import VideoEvidenceRetrievalService, vector_scope
from dovideo.application.evaluation_contracts import read_dataset
from dovideo.application.evaluation_runner import (EvaluationRunner, EvaluationRunnerConfig,
    EvaluationExecutionObservation, MappingEvaluationSourceResolver, project_retrieval_hits)
from dovideo.infrastructure.providers.summary import LocalChunkSummaryAdapter
from dovideo.presentation.composition import LocalRetrievalPlanner
from run_retrieval_comparison import BASELINE_SHA, baseline_services, prepared_fixture, aggregate, StructuralMetrics


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def git(*args):
    return subprocess.run(["git", "-c", f"safe.directory={ROOT.as_posix()}", *args],
                          cwd=ROOT, capture_output=True, check=True).stdout.decode("utf-8").strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_environment(paths):
    inherited = set(os.environ)
    loaded = []
    for path in paths:
        if not path.is_file():
            loaded.append({"filename": path.name, "status": "NOT_CONFIGURED"})
            continue
        names = []
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if not match:
                continue
            name, value = match.groups()
            if not name.startswith(("DOVIDEO_EMBEDDING_", "DOVIDEO_QDRANT_", "DOVIDEO_RERANKER_")):
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if name not in inherited:
                os.environ[name] = value
            names.append(name)
        loaded.append({"filename": path.name, "status": "LOADED", "names": sorted(names)})
    return loaded


def configured(value):
    return bool(value and value.strip() and value.strip().lower() not in {"replace-me", "changeme"})


def inherit_reranker_configuration(values):
    """Explicit eval CLI opt-in; never write credentials to a file or stdout."""
    inherited = False
    if not values.get("DOVIDEO_RERANKER_API_KEY", "").strip():
        if configured(values.get("DOVIDEO_EMBEDDING_API_KEY")):
            values["DOVIDEO_RERANKER_API_KEY"] = values["DOVIDEO_EMBEDDING_API_KEY"]
            inherited = True
    defaults = RerankerConfig()
    defaulted = []
    for name, value in (("DOVIDEO_RERANKER_URL", defaults.url),
                        ("DOVIDEO_RERANKER_MODEL", defaults.model)):
        if not values.get(name, "").strip():
            values[name] = value
            defaulted.append(name)
    return {"credentialSource": "DOVIDEO_EMBEDDING_API_KEY_RUNTIME_INHERITANCE" if inherited else "DOVIDEO_RERANKER_API_KEY",
            "defaultedSettings": defaulted, "processOnly": True}


def host(url):
    return urlsplit(url).hostname


def safe_failure(exc):
    # Do not serialize exception messages, HTTP bodies, URLs or tracebacks.
    result = {"status": "FAILED", "errorType": type(exc).__name__}
    reason = exc.__cause__
    if reason:
        result["causeType"] = type(reason).__name__
    return result


async def preflight():
    report = {}
    embedding = vector = reranker = None
    vector_value = None
    if not configured(os.environ.get("DOVIDEO_EMBEDDING_API_KEY")):
        report["bgeM3"] = {"status": "NOT_CONFIGURED", "missing": ["DOVIDEO_EMBEDDING_API_KEY"]}
    else:
        try:
            config = embedding_provider_config_from_environment(required=True)
            report["bgeM3"] = {"model": config.embedding_model, "host": host(config.embeddings_url)}
            if config.embedding_model != "BAAI/bge-m3":
                raise ValueError("noncanonical model")
            embedding = OpenAICompatibleEmbeddingAdapter(replace(config, timeout_seconds=min(config.timeout_seconds, 20)))
            vector_value = await embedding.embed("retrieval quality preflight")
            if len(vector_value) != 1024 or any(not math.isfinite(v) for v in vector_value):
                raise ValueError("noncanonical vector")
            report["bgeM3"].update(status="PASS", dimension=len(vector_value))
        except Exception as exc:
            report.setdefault("bgeM3", {}).update(safe_failure(exc))
    if not configured(os.environ.get("DOVIDEO_QDRANT_URL")):
        report["qdrant"] = {"status": "NOT_CONFIGURED", "missing": ["DOVIDEO_QDRANT_URL"]}
    else:
        try:
            vector = QdrantVectorIndex(base_url=os.environ["DOVIDEO_QDRANT_URL"],
                api_key=os.environ.get("DOVIDEO_QDRANT_API_KEY"),
                collection=os.environ.get("DOVIDEO_QDRANT_COLLECTION", "video_chunks"))
            report["qdrant"] = {"host": host(vector.base_url), "collection": vector.collection,
                "write": "NOT_RUN", "dimension": "NOT_RUN"}
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(vector.base_url + "/readyz",
                    headers={"api-key": vector.api_key} if vector.api_key else {})
            if response.status_code != 200:
                raise RuntimeError("not healthy")
            report["qdrant"]["health"] = "PASS"
        except Exception as exc:
            report.setdefault("qdrant", {}).update(safe_failure(exc))
        if vector is not None:
            try:
                response = await vector._send("GET", f"/collections/{vector.collection}", None)
                if response.status_code != 200:
                    raise RuntimeError("collection unreadable")
                report["qdrant"]["read"] = "PASS"
                if report["qdrant"].get("health") != "PASS":
                    raise RuntimeError("health check did not pass")
                if vector_value is not None:
                    from dovideo.infrastructure.vector.qdrant import _extract_dimension
                    if _extract_dimension(response.body) != 1024:
                        raise ValueError("collection dimension is not canonical")
                    await vector._ensure_collection(1024)
                    report["qdrant"]["dimension"] = 1024
                    from dovideo.domain import VideoChunk, CHUNKING_CONTRACT_VERSION
                    probe_media = secrets.randbelow(100_000_000) * 10 + 800_000_000_000
                    if await count_media(vector, probe_media) or await count_media(vector, probe_media + 1):
                        raise RuntimeError("probe namespace collision")
                    probe = VideoChunk(start_ms=0, end_ms=1000, embedding=vector_value,
                        source_revision="e" * 64, chunk_id="closure-preflight", chunking_version=CHUNKING_CONTRACT_VERSION)
                    try:
                        await vector.upsert(probe_media, (probe,))
                        hits = await vector.search(probe_media, vector_value, limit=1, **vector_scope((probe,)))
                        if len(hits) != 1 or hits[0].chunk_id != probe.chunk_id:
                            raise RuntimeError("probe write/read failed")
                        report["qdrant"]["write"] = "PASS"
                        report["qdrant"]["scopeVerification"] = await verify_scope(vector, (probe,), probe_media)
                    finally:
                        await vector.delete_media(probe_media)
                        if await count_media(vector, probe_media):
                            raise RuntimeError("probe cleanup failed")
                        report["qdrant"]["cleanup"] = "PASS"
                report["qdrant"]["status"] = "PASS"
            except Exception as exc:
                report["qdrant"].update(safe_failure(exc))
    if not configured(os.environ.get("DOVIDEO_RERANKER_API_KEY")):
        report["reranker"] = {"status": "NOT_CONFIGURED", "missing": ["DOVIDEO_RERANKER_API_KEY"],
                              "configuredEnabled": os.environ.get("DOVIDEO_RERANKER_ENABLED", "false")}
    else:
        try:
            values = dict(os.environ, DOVIDEO_RERANKER_ENABLED="true")
            config = RerankerConfig.from_environment(values)
            report["reranker"] = {"host": host(config.url), "model": config.model,
                                  "configuredEnabled": os.environ.get("DOVIDEO_RERANKER_ENABLED", "false")}
            if config.model != "BAAI/bge-reranker-v2-m3":
                raise ValueError("noncanonical reranker")
            reranker = SiliconFlowRerankerAdapter(config)
            await reranker.rerank("retrieval quality", (RerankerDocument("probe", "retrieval quality preflight"),))
            report["reranker"].update(status="PASS", modelExists="PASS", endpoint="PASS")
        except Exception as exc:
            report.setdefault("reranker", {}).update(safe_failure(exc))
    return report, embedding, vector, reranker


class DimensionCheckedEmbedding:
    def __init__(self, inner):
        self.inner = inner
    async def embed(self, text):
        value = tuple(await self.inner.embed(text))
        if len(value) != 1024 or any(not math.isfinite(v) for v in value):
            raise ValueError("noncanonical embedding")
        return value


class RecordingVector:
    """Observe actual adapter output; never provide synthetic candidates."""
    enabled = True
    def __init__(self, inner):
        self.inner, self.hits, self.scope = inner, (), {}
    async def upsert(self, media_id, chunks):
        return await self.inner.upsert(media_id, chunks)
    async def search(self, media_id, embedding, *, limit, **scope):
        self.scope = {"mediaId": media_id, "limit": limit, **scope}
        self.hits = await self.inner.search(media_id, embedding, limit=limit, **scope)
        return self.hits
    async def delete_media(self, media_id):
        return await self.inner.delete_media(media_id)


class RecordingSparse:
    def __init__(self, inner, trace):
        self.inner, self.trace = inner, trace
    def rank(self, *args, **kwargs):
        result = self.inner.rank(*args, **kwargs)
        self.trace["sparse"] = result
        return result


def attach_trace(service, *, baseline=False):
    """Wrap instance boundaries, always invoking the original frozen/current code.

    No gold, alternate scoring, monkeypatch of module globals or replayed output.
    Raw text remains transient and is never written by this tool.
    """
    trace = {}
    original_rank = service._rank
    async def rank(_self, *args, **kwargs):
        trace.clear()
        value = await original_rank(*args, **kwargs)
        trace["segments"] = value
        return value
    service._rank = MethodType(rank, service)
    original_intent = service._retrieval_intent
    async def intent(_self, *args):
        value = await original_intent(*args)
        trace["intent"] = value
        return value
    service._retrieval_intent = MethodType(intent, service)
    if baseline:
        original_score = service._score
        def score(_self, intent, embedding, vector_scores, chunk):
            value = original_score(intent, embedding, vector_scores, chunk)
            trace.setdefault("chunkScores", {})[chunk.chunk_id] = value
            return value
        service._score = MethodType(score, service)
    else:
        service._sparse = RecordingSparse(service._sparse, trace)
        original_dense = service._dense_candidates
        async def dense(_self, *args, **kwargs):
            value = await original_dense(*args, **kwargs)
            trace["dense"] = value
            return value
        service._dense_candidates = MethodType(dense, service)
        original_rerank = service._rerank
        async def rerank(_self, query, candidates, documents):
            trace["rrf"] = candidates
            value = await original_rerank(query, candidates, documents)
            trace["reranker"] = value
            return value
        service._rerank = MethodType(rerank, service)
    return trace


def chunk_ranks(candidates, chunks):
    return [{"chunkId": chunks[c.index].chunk_id, "rank": rank, "score": c.score}
            for rank, c in enumerate(candidates, 1)]


def summarize_case(case, chunks, trace, hits, vector, deltas, *, baseline, reranker_on, provider_mode="LIVE_QDRANT"):
    stages = {}
    if baseline:
        stages["dense"] = [{"chunkId": h.chunk_id, "rank": i, "score": h.score}
                           for i, h in enumerate(vector.hits, 1)]
        stages["baselineWeighted"] = [
            {"chunkId": c.chunk_id, "rank": i, "score": trace["chunkScores"][c.chunk_id]}
            for i, c in enumerate(sorted(chunks, key=lambda c: -trace["chunkScores"][c.chunk_id]), 1)]
        stages["final"] = stages["baselineWeighted"][:3]
        stages.update(sparse=[], rrf=[], reranker=[])
    else:
        for name in ("dense", "sparse", "rrf", "reranker"):
            stages[name] = chunk_ranks(trace.get(name, ()), chunks)
        stages["final"] = stages["reranker"][:3]
        if not reranker_on:
            stages["reranker"] = []
    final_lookup = {row["chunkId"]: row for row in stages["final"]}
    scored = trace.get("segments", ())
    segment_rows = [{"segmentId": s.segment.segment_id, "rank": rank,
                     "chunkId": s.chunk_id, "parentRank": final_lookup[s.chunk_id]["rank"],
                     "parentRelevance": final_lookup[s.chunk_id]["score"] if baseline else 1 / final_lookup[s.chunk_id]["rank"],
                     "asrMatch": s.transcript_score, "ocrMatch": s.visual_score,
                     "finalSegmentScore": s.score}
                    for rank, s in enumerate(scored, 1)]
    hit_ranks = {h.segment_id: i for i, h in enumerate(hits, 1)}
    expected = []
    for ref in case.expected_evidence_refs:
        segment = next(s for c in chunks for s in c.raw_segments if s.segment_id == ref.segment_id)
        parents = [c.chunk_id for c in chunks if any(s.segment_id == ref.segment_id for s in c.raw_segments)]
        parent_rows = []
        for parent in parents:
            row = {"chunkId": parent}
            for stage, values in stages.items():
                match = next((v for v in values if v["chunkId"] == parent), None)
                row[stage + "Rank"] = match["rank"] if match else None
                row[stage + "Score"] = match["score"] if match else None
            parent_rows.append(row)
        seg = next((s for s in segment_rows if s["segmentId"] == ref.segment_id), None)
        expected.append({"expectedSegmentId": ref.segment_id,
            "expectedTemporalRange": [segment.start_ms, segment.end_ms], "parents": parent_rows,
            "segmentRank": seg["rank"] if seg else None, "evidenceHitRank": hit_ranks.get(ref.segment_id),
            "segment": seg, "recallAt": {str(k): bool(hit_ranks.get(ref.segment_id) and hit_ranks[ref.segment_id] <= k) for k in (1, 3, 5)},
            "denseTopKHit": any(p["denseRank"] for p in parent_rows),
            "sparseTopKHit": any(p["sparseRank"] for p in parent_rows),
            "final3Hit": any(p["finalRank"] for p in parent_rows)})
    fallback = any(deltas.get(n, 0) for n in ("vectorStoreFallbacks", "embeddingFallbacks", "summaryFallbacks", "retrievalIntentFallbacks", "sparseFallbacks", "rerankerFallbacks"))
    return {"caseId": case.case_id, "category": case.tags[0], "sourceRevision": case.source_revision,
            "providerStatus": "FALLBACK_USED" if fallback else provider_mode + "_SUCCESS",
            "rerankerState": "ON" if reranker_on else "OFF", "fallbackDeltas": deltas,
            "vectorStoreFallbacksDelta": deltas.get("vectorStoreFallbacks", 0),
            "scope": vector.scope, "expected": expected, "stages": stages,
            "candidateCounts": {name: len(values) for name, values in stages.items()},
            "segmentRanking": segment_rows,
            "evidenceRanking": [{"segmentId": h.segment_id, "rank": i, "chunkId": h.chunk_id}
                                for i, h in enumerate(hits, 1)]}


class DiagnosticAdapter:
    def __init__(self, service, chunks, trace, vector, metrics, dataset, *, media_id, baseline, reranker_on, provider_mode="LIVE_QDRANT"):
        self.service, self.chunks, self.trace, self.vector = service, chunks, trace, vector
        self.metrics, self.dataset, self.media_id = metrics, dataset, media_id
        self.baseline, self.reranker_on, self.rows = baseline, reranker_on, []
        self.provider_mode = provider_mode
    async def execute(self, execution_input, *, artifact, strategy, trial_index, timeout_seconds):
        before = self.metrics.counts.copy()
        hits = await self.service.search(self.media_id, str(execution_input["query"]), self.chunks)
        # Gold is read only after the actual production search completes.
        matches = [case for case in self.dataset.cases if case.execution_input() == execution_input]
        if len(matches) != 1:
            raise ValueError("diagnostic input does not identify one frozen case")
        case = matches[0]
        delta = {k: self.metrics.counts[k] - before[k] for k in set(before) | set(self.metrics.counts)}
        row = summarize_case(case, self.chunks, self.trace, hits, self.vector, delta,
                             baseline=self.baseline, reranker_on=self.reranker_on, provider_mode=self.provider_mode)
        self.rows.append(row)
        if row["providerStatus"] == "FALLBACK_USED":
            raise RuntimeError("strict live provider fallback")
        return EvaluationExecutionObservation(retrieved_evidence=project_retrieval_hits(hits, artifact.context))


def chunk_metrics(rows):
    result = {}
    for stage in ("dense", "sparse", "rrf", "reranker", "final", "baselineWeighted"):
        if not any(r["stages"].get(stage) for r in rows):
            continue
        result[stage] = {}
        for k in (1, 3, 5, 8, 10):
            result[stage][f"recall@{k}"] = sum(
                sum(any(p.get(stage + "Rank") and p[stage + "Rank"] <= k for p in e["parents"])
                    for e in r["expected"]) / len(r["expected"]) for r in rows) / len(rows)
    return result


def reranker_changes(off_rows, on_rows):
    details = []
    for off, on in zip(off_rows, on_rows, strict=True):
        if off["caseId"] != on["caseId"]:
            raise ValueError("mismatched cases")
        before = [r["chunkId"] for r in off["stages"]["final"]]
        after = [r["chunkId"] for r in on["stages"]["final"]]
        details.append({"caseId": off["caseId"], "changedTop3": before != after,
            "rescued": any(not b["final3Hit"] and a["final3Hit"] for b, a in zip(off["expected"], on["expected"], strict=True)),
            "harmed": any(b["final3Hit"] and not a["final3Hit"] for b, a in zip(off["expected"], on["expected"], strict=True))})
    return {"counts": {k: sum(r[k] for r in details) for k in ("changedTop3", "rescued", "harmed")},
            "unchanged": [r["caseId"] for r in details if not r["changedTop3"]], "cases": details}


async def count_media(vector, media_id):
    response = await vector._send("POST", f"/collections/{vector.collection}/points/count",
        {"filter": {"must": [{"key": "mediaId", "match": {"value": media_id}}]}, "exact": True})
    if response.status_code != 200:
        raise RuntimeError("point count failed")
    return response.body["result"]["count"]


async def verify_scope(vector, chunks, media_id):
    """Own-media stale probes never share the production media namespace."""
    first = chunks[0]
    probes = (first.model_copy(update={"source_revision": "f" * 64, "chunk_id": "closure-stale-revision"}),
              first.model_copy(update={"chunking_version": "closure-old-version", "chunk_id": "closure-stale-version"}))
    await vector.upsert(media_id, probes)
    hits = await vector.search(media_id, first.embedding, limit=len(chunks) + 2, **vector_scope(chunks))
    if {h.chunk_id for h in hits} != {c.chunk_id for c in chunks}:
        raise RuntimeError("stale scope failed")
    other = await vector.search(media_id + 1, first.embedding, limit=1, **vector_scope(chunks))
    if other:
        raise RuntimeError("media scope failed")
    return {"status": "PASS", "mediaId": media_id, "sourceRevision": first.source_revision,
            "chunkingVersion": first.chunking_version, "staleProbeCount": 2, "scopedCount": len(hits)}


async def run_arms(output, metadata, preflight_report, embedding, vector, reranker):
    dataset, artifact = prepared_fixture()
    if read_dataset(ROOT / "datasets/x3/retrieval-focused-v2.json") != dataset:
        raise ValueError("frozen dataset mismatch")
    metadata.update(datasetDigest=dataset.dataset_digest, sourceRevision=artifact.source_revision,
        baselineSha=BASELINE_SHA, classification="SYNTHETIC", embedding="BAAI/bge-m3",
        summary="frozen deterministic LocalChunkSummaryAdapter", planner="frozen deterministic LocalRetrievalPlanner",
        embeddingInput="summary + newline + keywords", candidateSettings={"dense": 8, "sparse": 8, "fusion": 10,
            "final": 3, "rrfK": 60, "bm25K1": 1.2, "bm25B": .75, "segmentWeights": [.55, .25, .20],
            "windowMs": 300000, "overlapMs": 60000, "strideMs": 240000},
        baselineSemantics="frozen fixed5min + .60/.25/.15 chunk scores + old segment scoring; Qdrant Top6 with intrinsic cosine for remaining chunks")
    write_json(output / "run-metadata.json", metadata)
    baseline_chunking, baseline_retrieval = baseline_services(BASELINE_SHA)
    checked = DimensionCheckedEmbedding(embedding)
    arms, diagnostic_rows, scopes = {}, {}, []
    final_chunks = None
    for label, chunking_type, retrieval_type in (("A", baseline_chunking, baseline_retrieval),
                                                ("B", VideoChunkingService, VideoEvidenceRetrievalService),
                                                ("C", VideoChunkingService, VideoEvidenceRetrievalService)):
        if label == "C" and preflight_report["reranker"]["status"] != "PASS":
            arms[label] = {"status": "NOT_RUN", "reason": "reranker preflight did not pass"}
            continue
        media_id = secrets.randbelow(100_000_000) * 10 + 900_000_000_000
        if await count_media(vector, media_id) or await count_media(vector, media_id + 1):
            raise RuntimeError("evaluation media namespace collision")
        metrics = StructuralMetrics()
        recording_vector = RecordingVector(vector)
        kwargs = {"reranker": reranker if label == "C" else None} if label != "A" else {}
        service = retrieval_type(LocalRetrievalPlanner(), checked, recording_vector, metrics, **kwargs)
        trace = attach_trace(service, baseline=label == "A")
        started = time.perf_counter()
        try:
            chunks = final_chunks if label == "C" else await chunking_type(LocalChunkSummaryAdapter(), checked, metrics).build(artifact.context.segments)
            if not chunks or any(len(c.embedding) != 1024 for c in chunks) or metrics.counts["embeddingFallbacks"] or metrics.counts["summaryFallbacks"]:
                raise RuntimeError("chunk provider fallback")
            if label == "B":
                final_chunks = chunks
            await service.index(media_id, chunks)
            if metrics.counts["vectorStoreFallbacks"] or await count_media(vector, media_id) != len(chunks):
                raise RuntimeError("index write/read failed")
            preflight_report["qdrant"]["write"] = "PASS"
            if label != "A":
                scopes.append(await verify_scope(vector, chunks, media_id))
            adapter = DiagnosticAdapter(service, chunks, trace, recording_vector, metrics, dataset,
                media_id=media_id, baseline=label == "A", reranker_on=label == "C")
            runner = EvaluationRunner(adapter, MappingEvaluationSourceResolver({artifact.media_ref: artifact}), repo_root=ROOT)
            outcome = await runner.run(dataset, EvaluationRunnerConfig(retrieval_only=True,
                artifact_output=str(output / label), tools_enabled=False, critic_enabled=False))
            diagnostic_rows[label] = adapter.rows
            success = outcome.run.successful_count == len(dataset.cases)
            arms[label] = {"status": "PASS" if success else "FAILED", "successfulCases": outcome.run.successful_count,
                "failedCases": outcome.run.failed_count, "metrics": aggregate(outcome.results) if success else None,
                "categories": {category: aggregate([r for r in outcome.results if dataset.case(r.case_id).tags[0] == category])
                               for category in sorted({c.tags[0] for c in dataset.cases})} if success else {},
                "chunkMetrics": chunk_metrics(adapter.rows) if success else {},
                "perCase": {r.case_id: r.deterministic_metrics.model_dump(mode="json", by_alias=True) for r in outcome.results},
                "wallSeconds": time.perf_counter() - started, "chunkCount": len(chunks),
                "structuralCounts": dict(metrics.counts)}
            print(json.dumps({"arm": label, "status": arms[label]["status"], "metrics": arms[label]["metrics"]}), flush=True)
        except Exception as exc:
            arms[label] = safe_failure(exc)
        finally:
            await vector.delete_media(media_id)
            cleanup_count = await count_media(vector, media_id)
            arms[label]["cleanup"] = {"status": "PASS" if cleanup_count == 0 else "FAILED", "remainingPoints": cleanup_count}
        write_json(output / "ranking-diagnostics.json", diagnostic_rows)
        write_json(output / "comparison.json", {"arms": arms})
    value = reranker_changes(diagnostic_rows["B"], diagnostic_rows["C"]) if arms.get("C", {}).get("status") == "PASS" and arms.get("B", {}).get("status") == "PASS" else {"status": "NOT_RUN"}
    write_json(output / "reranker-value.json", value)
    write_json(output / "scope-verification.json", scopes)
    write_json(output / "preflight.json", preflight_report)
    complete = all(arms.get(a, {}).get("status") == "PASS" for a in ("A", "B"))
    write_json(output / "comparison.json", {"classification": "REMOTE BGE-M3 RESULT / SYNTHETIC",
        "status": "PASS WITH NOTES" if complete else "NOT READY", "datasetDigest": dataset.dataset_digest,
        "arms": arms, "rerankerValue": value, "limits": {key: "NOT_MEASURED" for key in
            ("realVideoGeneralization", "providerCost", "crossVideoScale", "noAnswer")}})


async def run_offline_diagnostics(output, metadata):
    """Historical negative controls; never included in live quality claims."""
    from dovideo.infrastructure.providers.local_embedding import LocalTfidfEmbeddingAdapter
    from dovideo.presentation.composition import InMemoryVectorIndex
    dataset, artifact = prepared_fixture()
    if read_dataset(ROOT / "datasets/x3/retrieval-focused-v2.json") != dataset:
        raise ValueError("frozen dataset mismatch")
    chunk_type, service_type = baseline_services(BASELINE_SHA)
    arms, rows = {}, {}
    for label, ctype, rtype in (("A", chunk_type, service_type), ("B", VideoChunkingService, VideoEvidenceRetrievalService)):
        embedding = LocalTfidfEmbeddingAdapter(max_features=2048).fit(
            [s.transcript + " " + " ".join(s.ocr_texts) for s in artifact.context.segments])
        metrics = StructuralMetrics()
        vector = RecordingVector(InMemoryVectorIndex())
        service = rtype(LocalRetrievalPlanner(), embedding, vector, metrics)
        trace = attach_trace(service, baseline=label == "A")
        chunks = await ctype(LocalChunkSummaryAdapter(), embedding, metrics).build(artifact.context.segments)
        await service.index(1, chunks)
        adapter = DiagnosticAdapter(service, chunks, trace, vector, metrics, dataset,
            media_id=1, baseline=label == "A", reranker_on=False, provider_mode="OFFLINE_LOCAL")
        outcome = await EvaluationRunner(adapter, MappingEvaluationSourceResolver({artifact.media_ref: artifact}), repo_root=ROOT).run(
            dataset, EvaluationRunnerConfig(retrieval_only=True, artifact_output=str(output / label),
                                             tools_enabled=False, critic_enabled=False))
        if outcome.run.successful_count != 12:
            raise RuntimeError("offline control execution failed")
        rows[label] = adapter.rows
        arms[label] = {"status": "OFFLINE_LOCAL_SUCCESS", "cases": 12, "metrics": aggregate(outcome.results),
            "categories": {category: aggregate([r for r in outcome.results if dataset.case(r.case_id).tags[0] == category])
                           for category in sorted({c.tags[0] for c in dataset.cases})}, "chunkMetrics": chunk_metrics(adapter.rows)}
    historical = json.loads((ROOT / "docs/retrieval-evaluation-v2/comparison.json").read_text(encoding="utf-8"))
    for label, key in (("A", "baseline"), ("B", "final")):
        if arms[label]["metrics"] != historical[key]["metrics"]:
            raise ValueError("negative control differs from historical metrics")
    write_json(output / "ranking-diagnostics.json", rows)
    write_json(output / "comparison.json", {"classification": "OFFLINE LOCAL RESULT / SYNTHETIC",
        "datasetDigest": dataset.dataset_digest, "baselineSha": BASELINE_SHA, "arms": arms,
        "liveQualityConclusion": "NOT_RUN", "historicalMetricsVerified": True})
    metadata.update(classification="OFFLINE LOCAL RESULT / SYNTHETIC", baselineSha=BASELINE_SHA,
        datasetDigest=dataset.dataset_digest, sourceRevision=artifact.source_revision,
        embedding="LocalTfidfEmbeddingAdapter(max_features=2048)", vector="InMemoryVectorIndex",
        summary="LocalChunkSummaryAdapter", planner="LocalRetrievalPlanner", reranker="OFF")
    write_json(output / "run-metadata.json", metadata)
    print(json.dumps({label: arms[label]["metrics"] for label in arms}, indent=2), flush=True)


async def main_async(args):
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("output must be empty; historical artifacts are immutable")
    args.output.mkdir(parents=True, exist_ok=True)
    env_sources = load_environment(args.env_file)
    metadata = {"startedAt": datetime.now(timezone.utc).isoformat(), "gitSha": git("rev-parse", "HEAD"),
                "workingTree": git("status", "--short"), "environmentSources": env_sources,
                "baselineSha": BASELINE_SHA, "dataset": "retrieval-focused-v2", "classification": "SYNTHETIC",
                "toolDigest": digest(Path(__file__)),
                "candidateSettings": {"dense": 8, "sparse": 8, "fusion": 10, "final": 3, "rrfK": 60,
                    "bm25K1": 1.2, "bm25B": .75, "segmentWeights": [.55, .25, .20],
                    "windowMs": 300000, "overlapMs": 60000, "strideMs": 240000},
                "configPresence": {k: configured(v) for k, v in os.environ.items()
                                   if k.startswith(("DOVIDEO_EMBEDDING_", "DOVIDEO_QDRANT_", "DOVIDEO_RERANKER_"))},
                "python": sys.version, "packages": {n: importlib.metadata.version(n) for n in
                    ("pydantic", "pytest", "pytest-asyncio", "httpx", "fastapi", "SQLAlchemy", "alembic", "redis", "minio", "celery", "PyMySQL", "Pillow", "uvicorn", "python-multipart")},
                "frozenFiles": {p: digest(ROOT / p) for p in
                    ("datasets/x3/retrieval-focused-v2.json", "datasets/x3/retrieval-focused-v2-source.json")}}
    if args.inherit_reranker_key:
        metadata["rerankerRuntimeConfiguration"] = inherit_reranker_configuration(os.environ)
        metadata["configPresence"].update({k: configured(v) for k, v in os.environ.items() if k.startswith("DOVIDEO_RERANKER_")})
    write_json(args.output / "run-metadata.json", metadata)
    if args.offline_diagnostics_only:
        await run_offline_diagnostics(args.output, metadata)
        return
    report, embedding, vector, reranker = await preflight()
    write_json(args.output / "preflight.json", report)
    print(json.dumps(report, indent=2), flush=True)
    if args.preflight_only:
        return
    if report["bgeM3"]["status"] != "PASS" or report["qdrant"]["status"] != "PASS":
        write_json(args.output / "comparison.json", {"status": "NOT READY", "qualityDecision": "E",
            "reason": "Live embedding/Qdrant preflight did not pass", "arms": {label: {"status": "NOT_RUN"} for label in ("A", "B", "C")}})
        write_json(args.output / "ranking-diagnostics.json", {
            "status": "NOT_RUN", "reason": "Strict live Qdrant path unavailable",
            "cases": [{"caseId": c.case_id, "category": c.tags[0], "status": "NOT_RUN",
                       "expectedSegmentIds": [r.segment_id for r in c.expected_evidence_refs],
                       "expectedTemporalRanges": [[r.start_ms, r.end_ms] for r in c.expected_temporal_regions]}
                      for c in read_dataset(ROOT / "datasets/x3/retrieval-focused-v2.json").cases]})
        return
    await run_arms(args.output, metadata, report, embedding, vector, reranker)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--offline-diagnostics-only", action="store_true")
    parser.add_argument("--inherit-reranker-key", action="store_true",
                        help="Explicitly authorize process-only embedding credential inheritance for reranker")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
