"""R4 OFF/ENABLED engineering A/B over controlled initial projections.

No remote Provider, Qdrant, reranker or extraction. Read existing real cache;
run existing Strict Hybrid + BM25 with fixture planner/vector/reranker ports.
Recovered facts must occur in the FINAL bounded projection, not merely hits.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import statistics
import time

from r4_validation_support import ROOT, harness, insufficient, FeedbackRetrievalMode as Mode
from dovideo.application.evidence_reflection import capture_evidence_reflection
from dovideo.application.retrieval_budget import capture_retrieval_budget, retrieval_budget_scope
from dovideo.application.retrieval_repair import capture_feedback_retrieval
from dovideo.application.follow_up_budget import capture_follow_up_budget
from dovideo.domain import AnalysisMode, VideoContext, VideoChunk


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def synthetic_cases():
    def case(id, question, span, first, target, *, trigger=True, behavior=None, proposal=None, parts=()):
        return {"id": id, "question": question, "proposal": proposal or insufficient(span),
            "texts": [first, "本段展示设置界面。", "本段展示操作过程。", target],
            "target_needles": [target], "expected_trigger": trigger, "parts": list(parts), "behavior": behavior,
            "label_origin": "DEVELOPER_LITERAL_FIXTURE_CHECK_NOT_GOLD", "initial_projection_kind": "CONTROLLED_FIRST_CANONICAL_SEGMENT"}
    sufficient = {"status": "SUFFICIENT", "reason_code": "SEMANTIC_COVERAGE", "covered_requirements": [
        {"requirement_key": "covered_reason", "question_span": "作者为什么选择 B？", "candidate_references": [0]}],
        "missing_requirements": [], "candidate_references": [0]}
    return [
        case("cause", "作者为什么选择 B？", "作者为什么选择 B？", "作者选择 B。", "作者选择 B 的原因是减少内存占用。"),
        case("comparison", "作者比较 A 和 B 的内存开销如何？", "A 和 B 的内存开销", "A 的内存开销为 8 MB。", "B 的内存开销为 4 MB。"),
        case("stage", "讲者在拍摄前、拍摄中和拍摄后分别怎样校准颜色？", "拍摄中", "拍摄前设白平衡，拍摄后用色卡校色。", "拍摄中用测试片校准颜色。"),
        case("multiple_facts", "作者在开头和结尾分别推荐什么？", "开头和结尾", "作者开头推荐 A。", "作者结尾推荐 B。"),
        case("mixed_video_step", "作者描述的缓存命中、未命中及回填流程合理吗？", "回填流程", "作者命中返回，未命中查数据库。", "作者查询数据库后回填缓存。"),
        case("sufficient", "作者为什么选择 B？", "作者为什么选择 B？", "作者选择 B 的原因是减少内存占用。", "作者选择 B 的原因是减少内存占用。", trigger=False, proposal=sufficient),
        case("mixed_verdict_block", "作者的缓存方案合理吗？", "合理吗", "作者命中返回，未命中查数据库再回填。", "方案背景介绍。", trigger=False),
        case("duplicate_only", "作者为什么选择 B？", "作者为什么选择 B？", "作者选择 B。", "作者选择 B 的原因是减少内存占用。", behavior="DUPLICATE_ONLY"),
        case("irrelevant_new_segments", "作者为什么选择 B？", "作者为什么选择 B？", "作者选择 B。", "作者选择 B 的原因是减少内存占用。", behavior="IRRELEVANT_NEW_SEGMENTS"),
        case("invalid_increment", "作者为什么选择 B？", "作者为什么选择 B？", "作者选择 B。", "作者选择 B 的原因是减少内存占用。", behavior="INVALID_REVISION"),
        case("m2_three_then_one", "作者比较 A 和 B 的内存开销如何；开头推荐谁；结尾推荐谁？", "A 和 B 的内存开销",
            "A 的内存开销为 8 MB。", "B 的内存开销为 4 MB。", parts=("A 和 B 的内存开销如何", "开头推荐谁", "结尾推荐谁")),
    ]


def cached_cases():
    base = ROOT / "work/evaluation/optimization-v1-20261009"
    context_path = base / "media-cuda/context.json"
    chunks_path = base / "run-02/rag/chunks.json"
    dataset_path = base / "draft-v1/dataset.json"
    manifest = read(base / "media-cuda/media-manifest.json")
    if digest(context_path) != manifest["artifactDigests"]["context.json"]:
        raise ValueError("frozen real context changed")
    context = VideoContext.model_validate(read(context_path))
    chunks = tuple(VideoChunk.model_validate(c) for c in read(chunks_path))
    from dovideo.application.retrieval_repair import TrustedRepairScope
    TrustedRepairScope.create(42, context, chunks)
    frozen = {c["caseId"]: c for c in read(dataset_path)["cases"]}
    r37 = {r["id"]: r for r in read(ROOT / "docs/agentic-rag/r37-validation-v1/development-none-1024/results.json")}
    r36 = {r["id"]: r for r in read(ROOT / "docs/agentic-rag/r36-validation-v1/draft-none-1024/results.json")}
    cases = []
    for id in ("tc-17", "tc-18"):
        case = frozen[id]
        first_sid = case["expectedEvidenceRefs"][0]["segmentId"]
        initial = next(s for s in context.segments if s.segment_id == first_sid)
        proposal = r37[id + "-partial-cache"]["assessment"] if id == "tc-17" else r36[id + "-partial-cache"]["assessment"]
        cases.append({"id": id, "question": case["query"], "proposal": proposal,
            "context": context, "chunks": chunks, "initial_segments": (initial,),
            "target_needles": ["3n+1003"] if id == "tc-17" else [],
            "target_refs": case["expectedEvidenceRefs"][1:], "expected_trigger": id == "tc-17",
            "label_origin": "REAL_DRAFT_LITERAL_SCOPE_CHECK_NOT_GOLD",
            "initial_projection_kind": "DECLARED_PARTIAL_CACHE_FIRST_DRAFT_LINKED_SEGMENT_NOT_NATURAL_RANKING",
            "reflection_fixture_origin": "R3.7 actual response replay" if id == "tc-17" else "R3.6 actual response replay"})
    return cases, {"media_context_sha256": digest(context_path), "chunks_sha256": digest(chunks_path),
                   "draft_dataset_sha256": digest(dataset_path), "source_revision": context.source_revision,
                   "existing_media_manifest_sha256": digest(base / "media-cuda/media-manifest.json"),
                   "extraction_or_reindex_calls": 0}


def normalized(value):
    return "".join(value.casefold().split())


def target_coverage(case, projection, context):
    def text(p):
        return normalized(p["asrExcerpt"] + " ".join(p["ocrExcerpts"]))
    all_text = "\n".join(text(p) for p in projection)
    needles = case.get("target_needles", [])
    if needles and not all(normalized(needle) in all_text for needle in needles):
        return False
    for ref in case.get("target_refs", []):
        segment = next(s for s in context.segments if s.segment_id == ref["segmentId"])
        if not any(p["startMs"] == segment.start_ms and p["endMs"] == segment.end_ms for p in projection):
            return False
        if ref["sourceItemId"] not in segment.source_item_ids or segment.source_revision != ref["sourceRevision"]:
            return False
        # No credit for a hidden full-source fact lost by answer clipping.
        observation = next(o for o in context.observations if o.source_item.source_item_id == ref["sourceItemId"])
        excerpt = normalized(observation.text[:80])
        matching = [p for p in projection if p["startMs"] == segment.start_ms and p["endMs"] == segment.end_ms]
        if not excerpt or not any(excerpt in text(p) for p in matching):
            return False
    return bool(needles or case.get("target_refs"))


def target_regions_present(case, projection, context):
    refs = case.get("target_refs", [])
    return bool(refs) and all(any(p["startMs"] == s.start_ms and p["endMs"] == s.end_ms
        for p in projection for s in context.segments if s.segment_id == ref["segmentId"])
        for ref in refs)


async def arm(case, mode):
    h = await harness(case["question"], case["proposal"], mode=mode,
        context=case.get("context"), chunks=case.get("chunks"), initial_segments=case.get("initial_segments"),
        texts=case.get("texts"), parts=tuple(case.get("parts", ())))
    service, context, chunks, initial, counts, http, hybrid = h
    if case.get("behavior"):
        baseline = service._retrieval.search_evidence_baseline
        async def injected(*a, **kw):
            hits = await baseline(*a, **kw)  # one actual metered Strict Hybrid
            if case["behavior"] == "DUPLICATE_ONLY":
                return initial
            if case["behavior"] == "INVALID_REVISION":
                return (hits[0].model_copy(update={"source_revision": "foreign-revision"}),)
            # Return genuinely new canonical source segments, but not the
            # requested fact. Added candidates are not automatically success.
            from r4_validation_support import hit_for
            return tuple(hit_for(s, chunks) for s in context.segments[1:3])
        service._retrieval.search_evidence_baseline = injected
    # A-only nonenforcing instrumentation; production R4 OFF has no scope.
    capture_scope = retrieval_budget_scope(True, enforced=False) if mode == Mode.OFF else nullcontext()
    started = time.perf_counter()
    error, answer = None, None
    with capture_retrieval_budget() as budgets, capture_feedback_retrieval() as repairs, capture_evidence_reflection() as reflections, capture_follow_up_budget() as usage, capture_scope:
        try:
            answer = await service.answer(42, case["question"], "视频学习", AnalysisMode.LEARNING)
        except Exception as exc:
            from dovideo.application.follow_up import FollowUpFailure
            if not isinstance(exc, FollowUpFailure):
                raise
            error = exc.category
    latency_ms = (time.perf_counter() - started) * 1000
    inputs = []
    for payload in http.payloads:
        content = payload["messages"][-1]["content"]
        if "Input as JSON:\n" in content:
            value = json.loads(content.split("Input as JSON:\n")[-1])
            if "retrievedSourceCandidates" in value:
                inputs.append(value)
    projection = inputs[-1]["retrievedSourceCandidates"] if inputs else []
    initial_payload = next(p for p in http.payloads if "Grade semantic sufficiency" in p["messages"][0]["content"])
    before = json.loads(initial_payload["messages"][-1]["content"])["retrievedSourceCandidates"]
    observation = repairs[0] if repairs else None
    regions_present = target_regions_present(case, projection, context)
    source_has_fact = bool(case.get("target_needles")) and all(any(normalized(needle) in normalized(o.text)
        for o in context.observations if o.source_item.source_item_id in {ref["sourceItemId"] for ref in case.get("target_refs", [])})
        for needle in case.get("target_needles", []))
    result = {"mode": mode.value, "error": error, "guard_rejected": error == "evidence_rejected",
        "answer_generated": answer is not None, "initial_projection": before, "final_projection": projection,
        "initial_target_present": target_coverage(case, before, context), "final_target_present": target_coverage(case, projection, context),
        "target_regions_present": regions_present,
        "original_target_source_contains_fact": source_has_fact,
        "target_lost_by_bounded_projection": bool(regions_present and source_has_fact and not target_coverage(case, projection, context)),
        "retrieval_budget": budgets[0], "counts": dict(counts), "fixture_chat_attempts": usage[0]["chat_http_attempts"],
        "fixture_chat_tokens": usage[0]["charged_chat_tokens"], "stages": usage[0]["stages"], "fixture_e2e_ms": latency_ms,
        "reflection_outcome": reflections[0].outcome,
        "state": observation.state if observation else "OFF",
        "eligible": observation.eligible if observation else False, "attempted": observation.attempted if observation else False,
        "new_segments": observation.new_segments if observation else 0, "invalid_candidates": observation.invalid_candidates if observation else 0,
        "unhandled_gaps": observation.unhandled_gaps if observation else 0,
        "plan": observation.plan.model_dump() if observation and observation.plan else None,
        "real_provider_usage": "NOT_MEASURED", "real_provider_cost_usd": "NOT_MEASURED",
        "real_embedding_qdrant_reranker_http": "NOT_MEASURED"}
    assert len(projection) <= 8
    assert counts["reflection_http"] == 1  # same result, no second Grader
    assert not usage[0]["stages"].get("QUERY_REPAIR", 0)
    if mode == Mode.ENABLED:
        assert budgets[0]["repair_base_calls"] <= 1 and budgets[0]["total_base_calls"] <= 4
        assert result["attempted"] == case["expected_trigger"]
    return result


async def run(output, *, synthetic_only=False):
    output.mkdir(parents=True, exist_ok=False)
    cached, sources = ([], {}) if synthetic_only else cached_cases()
    cases = synthetic_cases() + cached
    frozen = [{k: v for k, v in case.items() if k not in {"context", "chunks", "initial_segments"}} for case in cases]
    save(output / "frozen-cases.json", {"cases": frozen, "source_digests": sources,
        "kind": "CONTROLLED_INITIAL_EVIDENCE_ENGINEERING_AB_NO_LIVE_PROVIDER", "independent_gold": False,
        "same_request_limits_both_arms": {"deadline_seconds": 60, "token_ceiling": 500000, "provider_fixture_attempts": 1},
        "reflection_mode_both_arms": "SHADOW", "initial_model_configuration_equal": True})
    rows = []
    for case in cases:
        a, b = await arm(case, Mode.OFF), await arm(case, Mode.ENABLED)
        assert a["initial_projection"] == b["initial_projection"]
        recovered = not a["final_target_present"] and b["final_target_present"]
        rows.append({"id": case["id"], "label_origin": case["label_origin"], "initial_projection_kind": case["initial_projection_kind"],
            "expected_trigger": case["expected_trigger"], "recovered_requirement": recovered, "A": a, "B": b})
        save(output / "results.json", rows)
        print(case["id"], b["state"], "RECOVERED" if recovered else "NOT_RECOVERED", flush=True)
    attempts = [r for r in rows if r["B"]["attempted"]]
    recovered = [r for r in attempts if r["recovered_requirement"]]
    summary = {"status": "PASS_ENGINEERING_AB_WITH_CACHE_PROJECTION_LIMITATION_NOT_PRODUCTION", "cases": len(rows),
        "eligible_requests": sum(r["B"]["eligible"] for r in rows), "trigger_count": len(attempts),
        "actual_repair_attempts": sum(r["B"]["retrieval_budget"]["repair_base_calls"] for r in rows),
        "extra_base_hybrid_calls": sum(r["B"]["retrieval_budget"]["total_base_calls"] - r["A"]["retrieval_budget"]["total_base_calls"] for r in rows),
        "stopped_or_not_required": dict(Counter(r["B"]["state"] for r in rows)),
        "recovered_requirements": len(recovered), "recovered_case_ids": [r["id"] for r in recovered],
        "cache_target_region_recovered_but_fact_clipped": [r["id"] for r in rows if r["B"]["target_lost_by_bounded_projection"]],
        "new_trusted_segments": sum(r["B"]["new_segments"] for r in rows),
        "target_no_progress_rate_among_attempts": (len(attempts) - len(recovered)) / len(attempts),
        "duplicate_only_rate_among_attempts": sum(r["B"]["state"] == "NO_NEW_EVIDENCE" for r in attempts) / len(attempts),
        "invalid_increment_rate_among_attempts": sum(r["B"]["state"] == "INVALID_EVIDENCE" for r in attempts) / len(attempts),
        "wrong_trigger_observed": sum(r["B"]["attempted"] and not r["expected_trigger"] for r in rows),
        "guard_rejections": sum(r["B"]["guard_rejected"] for r in rows), "cross_source_accepted": 0,
        "extra_query_repair_chat_attempts": 0,
        "extra_existing_planner_fixture_http": sum(r["B"]["counts"].get("planner_http", 0) - r["A"]["counts"].get("planner_http", 0) for r in rows),
        "fixture_chat_tokens_A": sum(r["A"]["fixture_chat_tokens"] for r in rows),
        "fixture_chat_tokens_B": sum(r["B"]["fixture_chat_tokens"] for r in rows),
        "fixture_latency_median_ms_A": statistics.median(r["A"]["fixture_e2e_ms"] for r in rows),
        "fixture_latency_median_ms_B": statistics.median(r["B"]["fixture_e2e_ms"] for r in rows),
        "component_calls": {arm: {name: sum(r[arm]["counts"].get(name, 0) for r in rows) for name in ("embedding", "vector_query_fixture", "bm25", "reranker_fixture")} for arm in ("A", "B")},
        "new_paid_provider_calls": 0, "real_provider_quality_tokens_cost_latency": "NOT_MEASURED",
        "production_shadow_skip_ratio": "NOT_MEASURED", "independent_human_gold": False,
        "scope_note": "Initial deficits are controlled fixtures. Source cache and supplemental application Hybrid/BM25/provenance are reused. Vector, reranker, generation and Usage are fixtures. No natural-ranking recall or real Provider accuracy claim."}
    save(output / "summary.json", summary)
    assert summary["wrong_trigger_observed"] == summary["guard_rejections"] == 0
    if not synthetic_only:
        tc17 = next(r for r in rows if r["id"] == "tc-17")
        assert tc17["B"]["target_regions_present"] and tc17["B"]["original_target_source_contains_fact"]
        assert not tc17["recovered_requirement"] and tc17["B"]["target_lost_by_bounded_projection"]
    # The real source target occurs beyond the unchanged OCR400 limit. This
    # negative check deliberately forbids crediting a retrieved region as a
    # recovered formula. Changing clipping is outside this R4 experiment.
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--synthetic-only", action="store_true", help="Run public synthetic cases without private cached-video inputs")
    args = parser.parse_args()
    asyncio.run(run(args.output, synthetic_only=args.synthetic_only))
