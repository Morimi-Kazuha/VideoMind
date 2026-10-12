"""Build, validate, review and smoke-test a private V1 pilot using X3 contracts.

The candidate specification is private source-based annotation, never an Agent
input. Smoke runs exactly once; determinism checks replay only captured ranks.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import html
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from prepare_v1_pilot_media import digest, save, private_output
from dovideo.domain import VideoContext
from dovideo.application.context import VideoContextBuilder
from dovideo.application.value_objects import (AsrBranchOutcome, OcrBranchOutcome,
    MediaObservationBundle, TranscriptSpan, OcrObservation)
from dovideo.application.evaluation_contracts import (EvaluationCase, EvaluationDataset,
    ExpectedEvidenceRef, ExpectedTemporalRegion, RequiredFact, dataset_to_json, read_dataset)
from dovideo.application.evaluation_runner import (EvaluationRunner, EvaluationRunnerConfig,
    EvaluationSourceArtifact, MappingEvaluationSourceResolver, RetrievalEvaluationAdapter)
from dovideo.application.evaluation_pilot import PilotAnnotations, validate_pilot, score_pilot_retrieval
from dovideo.application.chunking import VideoChunkingService
from dovideo.application.retrieval import VideoEvidenceRetrievalService
from dovideo.infrastructure.providers.local_embedding import LocalTfidfEmbeddingAdapter
from dovideo.infrastructure.providers.summary import LocalChunkSummaryAdapter
from dovideo.presentation.composition import InMemoryVectorIndex, LocalRetrievalPlanner


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def fresh(path):
    path = private_output(path)
    if any(path.iterdir()):
        raise ValueError("output must be empty; preserve previous annotations/results")
    return path


def media_context(directory):
    manifest = load(directory / "media-manifest.json")
    if digest(Path(manifest["mediaPath"])) != manifest["mediaSha256"]:
        raise ValueError("original media content changed")
    for name, expected in manifest["artifactDigests"].items():
        if digest(directory / name) != expected:
            raise ValueError("frozen media artifact changed: " + name)
    visual = load(directory / "ocr.json")
    if load(directory / "ocr-complete.json")["count"] != len(visual):
        raise ValueError("incomplete OCR cache")
    for frame in visual:
        if digest(directory / frame["frame_ref"]) != frame["frameSha256"]:
            raise ValueError("frame changed")
    context = VideoContext.model_validate(load(directory / "context.json"))
    speech = load(directory / "asr.json")
    rebuilt = VideoContextBuilder().build(context.source, "", MediaObservationBundle(
        asr=AsrBranchOutcome(observations=tuple(TranscriptSpan(**s) for s in speech), attempted=1),
        ocr=OcrBranchOutcome(observations=tuple(OcrObservation(timestamp_ms=s["timestamp_ms"],
            text=s["text"], frame_ref=s["frame_ref"]) for s in visual), attempted=len(visual))),
        media_content_identity=manifest["mediaSha256"])
    if context != rebuilt or context.source_revision != manifest["sourceRevision"]:
        raise ValueError("context is not the authoritative raw-media projection")
    return manifest, context


def golden(directory, media_dir):
    manifest, context = media_context(media_dir)
    dataset = read_dataset(directory / "dataset.json")
    annotations = PilotAnnotations.model_validate(load(directory / "annotations.json"))
    if annotations.media_sha256 != manifest["mediaSha256"]:
        raise ValueError("sidecar/media hash mismatch")
    checked = validate_pilot(dataset, annotations, context, duration_ms=manifest["durationMs"])
    return manifest, context, dataset, annotations, checked


def build(args):
    manifest, context = media_context(args.media_dir)
    specification = load(args.spec)
    observations = {(o.source_item.source_type, o.source_item.timestamp_ms): o for o in context.observations}
    cases, metadata = [], []
    for spec in specification["cases"]:
        refs = []
        for selector in spec["evidence"]:
            observation = observations[(selector["source_type"], selector["timestamp_ms"])]
            item = observation.source_item
            refs.append(ExpectedEvidenceRef(ref_id=selector["ref_id"], source_revision=context.source_revision,
                segment_id=item.segment_id, source_item_id=item.source_item_id, source_type=item.source_type,
                timestamp_ms=item.timestamp_ms, start_ms=item.timestamp_ms,
                end_ms=item.end_ms or item.timestamp_ms + 1))
        facts = tuple(RequiredFact(**f) for f in spec["required_facts"])
        cases.append(EvaluationCase(case_id=spec["case_id"], dataset_version=specification["dataset_version"],
            media_ref=context.source, source_revision=context.source_revision, query=spec["query"],
            mode=spec["mode"], query_category=spec["query_category"], difficulty=spec["difficulty"],
            reference_answer=spec["reference_answer"], acceptable_answer_variants=tuple(spec["acceptable_variants"]),
            required_facts=facts, expected_evidence_refs=tuple(refs),
            expected_temporal_regions=tuple(ExpectedTemporalRegion(**r) for r in spec["temporal_regions"]),
            tags=tuple(spec["tags"]), annotation_version="v1-pilot-draft-v1"))
        metadata.append({key: spec[key] for key in ("case_id", "answerability", "annotation_status",
            "annotation_notes", "fact_annotations", "underlying_fact_groups", "absence_review_scope")})
    dataset = EvaluationDataset(dataset_version=specification["dataset_version"], cases=tuple(cases),
        annotation_version="v1-pilot-draft-v1", target_case_count=len(cases), completeness="COMPLETE",
        data_classification="MEASURED", description="Real media; machine-proposed DRAFT labels pending human review.")
    annotations = PilotAnnotations(dataset_version=dataset.dataset_version, dataset_digest=dataset.dataset_digest,
        source_revision=context.source_revision, media_sha256=manifest["mediaSha256"],
        generator_identity=specification["generator_identity"], verifier_identity=specification["verifier_identity"],
        verification_independence=specification["verification_independence"], cases=tuple(metadata))
    checked = validate_pilot(dataset, annotations, context, duration_ms=manifest["durationMs"])
    out = fresh(args.output)
    (out / "dataset.json").write_text(dataset_to_json(dataset) + "\n", encoding="utf-8")
    save(out / "annotations.json", annotations.model_dump(mode="json"))
    save(out / "validation.json", checked)
    save(out / "build-manifest.json", {"candidateSpecSha256": digest(args.spec),
        "datasetDigest": dataset.dataset_digest, "sidecarSha256": digest(out/"annotations.json"),
        "builderSha256": digest(__file__), "sourceRevision": context.source_revision,
        "mediaSha256": manifest["mediaSha256"], "goldFreeze": "NOT_FROZEN"})
    review(out, args.media_dir, dataset, annotations, context, manifest)
    print(json.dumps(checked, ensure_ascii=False))


def review(out, media_dir, dataset, annotations, context, manifest):
    by_item = {o.source_item.source_item_id: o for o in context.observations}
    by_case = {c.case_id: c for c in annotations.cases}
    md = ["# V1 私有人工复核清单", "", "所有条目仍待人工审核。来源可解析不代表语义 Gold 已确认。",
          "", f"原视频：{manifest['mediaPath']}", "", "用同目录 review.html 的定位按钮播放原视频；视频不会被复制或上传。", ""]
    cards = []
    for case in dataset.cases:
        meta = by_case[case.case_id]
        evidence, snippets = [], []
        for ref in case.expected_evidence_refs:
            obs = by_item[ref.source_item_id]
            seconds = ref.timestamp_ms / 1000
            # Show enough original context for a reviewer, privately; no public export.
            text = obs.text[:900]
            evidence.append(f"- {seconds:.2f}s {ref.source_type.value}: {text.replace(chr(10), ' ')}")
            frame = ""
            if obs.frame_ref:
                url = quote(os.path.relpath(media_dir / obs.frame_ref, out).replace('\\', '/'), safe='/')
                frame = f'<details><summary>原始画面</summary><img loading="lazy" src="{html.escape(url)}"></details>'
            snippets.append(f'<div><button data-seek="{seconds}">播放 {seconds:.2f}s</button> '
                f'{ref.source_type.value}<pre>{html.escape(text)}</pre>{frame}</div>')
        md.extend([f"## {case.case_id} · {meta.annotation_status}", "", f"问题：{case.query}", "",
            f"建议答案：{case.reference_answer}", "", "关键来源：", "", *evidence,
            "", f"待确认：{meta.annotation_notes}", "", f"审核范围：{meta.absence_review_scope or '按下列证据逐事实核对'}", "",
            "人工记录：审核人 ____ 日期 ____ 判定 ____ 修改/依据 ____", ""])
        cards.append(f'<article><h2>{html.escape(case.case_id)} · {meta.annotation_status}</h2>'
            f'<h3>{html.escape(case.query)}</h3><p>建议答案：{html.escape(case.reference_answer or "")}</p>'
            f'<p>待确认：{html.escape(meta.annotation_notes)}</p><p>{html.escape(meta.absence_review_scope or "")}</p>'
            + ''.join(snippets) + '</article>')
    (out / "REVIEW_CHECKLIST.md").write_text("\n".join(md), encoding="utf-8")
    media_url = quote(os.path.relpath(Path(manifest["mediaPath"]), out).replace('\\', '/'), safe='/')
    document = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>VideoMind 私有复核</title>' \
        '<style>body{font:16px/1.65 system-ui;max-width:1100px;margin:24px auto;padding:0 24px;background:#f4f5f7}' \
        'article{background:white;padding:20px;margin:20px 0;border-radius:12px}pre{white-space:pre-wrap}' \
        'video{width:100%;max-height:420px;background:#111}img{width:100%}button{padding:8px;cursor:pointer}</style>' \
        '<h1>VideoMind V1 人工复核 · NEEDS_REVIEW</h1><p>逐事实核对原视频。自动审核与题目生成来自同一模型；没有人工批准。</p>' \
        f'<video id="media" controls preload="metadata" src="{html.escape(media_url)}"></video>' \
        + ''.join(cards) + '<script>document.querySelectorAll("[data-seek]").forEach(b=>b.onclick=()=>{' \
        'const v=document.getElementById("media");v.currentTime=Math.max(0,Number(b.dataset.seek)-2);' \
        'v.play().catch(()=>{});v.scrollIntoView({behavior:"smooth"});});</script></html>'
    (out / "review.html").write_text(document, encoding="utf-8")


class CachedChunks:
    def __init__(self, chunks, segments):
        self.chunks, self.segments = chunks, segments
    async def build(self, segments):
        if tuple(segments) != self.segments:
            raise ValueError("context changed after chunk preparation")
        return self.chunks


class ObservedAdapter:
    """Checks and records runtime inputs/ranks only; never receives annotations."""
    def __init__(self, inner):
        self.inner, self.records = inner, []
    async def execute(self, execution_input, **kwargs):
        if set(execution_input) != {"media_ref", "query", "mode"}:
            raise ValueError("gold leakage")
        observation = await self.inner.execute(execution_input, **kwargs)
        self.records.append(observation.retrieved_evidence)
        return observation


async def smoke(args):
    manifest, context, dataset, annotations, validation = golden(args.gold_dir, args.media_dir)
    out = fresh(args.output)
    started = time.monotonic()
    embedding = LocalTfidfEmbeddingAdapter(max_features=2048).fit(
        [s.transcript + " " + " ".join(s.ocr_texts) for s in context.segments])
    chunks = await VideoChunkingService(LocalChunkSummaryAdapter(), embedding).build(context.segments)
    save(out / "chunks.json", [c.model_dump(mode="json", by_alias=True) for c in chunks])
    retrieval = VideoEvidenceRetrievalService(LocalRetrievalPlanner(), embedding, InMemoryVectorIndex())
    adapter = ObservedAdapter(RetrievalEvaluationAdapter(CachedChunks(chunks, context.segments), retrieval))
    artifact = EvaluationSourceArtifact(media_ref=context.source, source_revision=context.source_revision,
        context=context, chunks=chunks, source_types=("ASR", "OCR"))
    # Process-local Git exception: no global Git configuration or repository mutation.
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    os.environ.update({"GIT_CONFIG_COUNT": str(count+1), f"GIT_CONFIG_KEY_{count}": "safe.directory",
        f"GIT_CONFIG_VALUE_{count}": ROOT.as_posix()})
    runner = EvaluationRunner(adapter, MappingEvaluationSourceResolver({context.source: artifact}), repo_root=ROOT)
    config = EvaluationRunnerConfig(retrieval_only=True, trial_count=1, max_executions=len(dataset.cases),
        runner_config_version="v1-pilot-offline-smoke-v1", artifact_output=str(out/"x3"),
        tools_enabled=False, critic_enabled=False, timeout_seconds=30)
    save(out / "config.json", config.model_dump(mode="json", by_alias=True))
    outcome = await runner.run(dataset, config)
    by_case = {c.case_id: c for c in annotations.cases}
    if len(adapter.records) != len(dataset.cases):
        save(out / "failure.json", {"failure": "incomplete execution", "capturedCases": len(adapter.records)})
        raise RuntimeError("incomplete execution; inspect X3 per-case failure artifacts")
    scores = {}
    for case, ranks, result in zip(dataset.cases, adapter.records, outcome.results, strict=True):
        score = score_pilot_retrieval(case, by_case[case.case_id], ranks)
        again = score_pilot_retrieval(case, by_case[case.case_id], ranks)
        if score != again:
            raise ValueError("nondeterministic scoring on identical captured ranking")
        scores[case.case_id] = {**score, "latency": result.latency.model_dump(mode="json", by_alias=True),
            "human_status": by_case[case.case_id].annotation_status}
        save(out/(case.case_id+".retrieval.json"), [r.model_dump(mode="json", by_alias=True) for r in ranks])
    save(out / "pilot-metrics.json", scores)
    positive = [s for s in scores.values() if "recall_at_k" in s]
    aggregate = {f"recall@{k}": sum(s["recall_at_k"][k] for s in positive)/len(positive) for k in (1,3,5)}
    aggregate.update({f"precision@{k}": sum(s["precision_at_k"][k] for s in positive)/len(positive) for k in (1,3,5)})
    aggregate["mrr"] = sum(s["mrr"] for s in positive)/len(positive)
    for k in (1,3,5):
        aggregate[f"fact-evidence-recall@{k}"] = sum(s["fact_evidence_recall_at_k"][str(k)]["coverage"] for s in positive)/len(positive)
    metadata = {"datasetDigest": dataset.dataset_digest, "sidecarSha256": digest(args.gold_dir/"annotations.json"),
        "sourceRevision": context.source_revision, "mediaSha256": manifest["mediaSha256"],
        "configFingerprint": config.fingerprint(), "gitSha": outcome.run.git_sha,
        "workingTreeState": outcome.run.working_tree_state.value, "runId": outcome.run.run_id,
        "scope": "REAL_MEDIA_OFFLINE_TFIDF_RETRIEVAL_ONLY", "goldStatus": "DRAFT_NOT_FROZEN",
        "backend": "LocalTfidfEmbeddingAdapter + InMemoryVectorIndex + production retrieval ranking",
        "summary": "LocalChunkSummaryAdapter", "planner": "LocalRetrievalPlanner", "mockLlm": "NOT_USED",
        "providerApiCalls": 0, "providerTokens": "NOT_MEASURED", "providerCost": "NOT_MEASURED",
        "bgeM3": "NOT_RUN_NOT_CONFIGURED", "qdrant": "NOT_RUN", "agentE2E": "NOT_RUN",
        "inheritedEmbeddingConfigured": all(os.environ.get(n) for n in
            ("DOVIDEO_EMBEDDING_BASE_URL", "DOVIDEO_EMBEDDING_API_KEY", "DOVIDEO_EMBEDDING_MODEL")),
        "runs": 1, "deterministicScoringReplays": 2, "chunks": len(chunks),
        "embeddingDimension": embedding.dimension, "elapsedSeconds": time.monotonic()-started,
        "categories": dict(Counter(c.query_category.value for c in dataset.cases)),
        "aggregateAnswerableOnly": aggregate, "validation": validation,
        "successful": outcome.run.successful_count, "failed": outcome.run.failed_count,
        "excluded": outcome.run.excluded_count, "notRun": outcome.run.not_run_count,
        "codeDigests": {str(p.relative_to(ROOT)).replace('\\','/'): digest(p) for p in
            sorted([*list((ROOT/'src'/'dovideo').rglob('*.py')), Path(__file__),
                    ROOT/'tools'/'prepare_v1_pilot_media.py', ROOT/'tools'/'run_retrieval_comparison.py'])}}
    save(out / "run-metadata.json", metadata)
    print(json.dumps({k:v for k,v in metadata.items() if k != "codeDigests"}, ensure_ascii=False, indent=2))
    if outcome.run.failed_count or outcome.run.excluded_count or outcome.run.not_run_count:
        raise RuntimeError("smoke incomplete; inspect artifacts")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("build", "validate", "smoke", "schema"))
    p.add_argument("--media-dir", type=Path)
    p.add_argument("--gold-dir", type=Path)
    p.add_argument("--spec", type=Path)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    if args.action == "schema":
        save(args.output, PilotAnnotations.model_json_schema())
    elif args.action == "build":
        build(args)
    elif args.action == "validate":
        print(json.dumps(golden(args.gold_dir, args.media_dir)[-1]))
    else:
        asyncio.run(smoke(args))


if __name__ == "__main__":
    main()
