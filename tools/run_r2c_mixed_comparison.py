"""Reuse the R2B paired runner; all responses/usage are fixture values."""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import sys
from run_r2b_direct_comparison import ROOT, run_case
from test_r2c_mixed_follow_up import MixedHttp, MIXED_Q, mixed_harness, ask, BLOCKS
from test_r2b_direct_follow_up import QUESTION
from dovideo.application.retrieval_need import RetrievalNeedMode
from dovideo.infrastructure.providers.follow_up import MixedKnowledgeModelAdapter
from dovideo.application.follow_up_budget import capture_follow_up_budget, FollowUpBudgetSettings, FollowUpBudgetMode


async def cached_source_pair():
    """Frozen real source + fixture selection/answers; structural provenance only."""
    from run_v1_pilot import media_context
    from test_m1_conversation_memory import harness
    from dovideo.domain import VideoChunk, VideoEvidenceHit, AnalysisMode
    from dovideo.application.conversation_memory import ConversationIdentity
    from dovideo.application.chunking import chunks_compatible
    from uuid import uuid4
    directory = ROOT/'work/evaluation/optimization-v1-20261009'
    manifest, context = media_context(directory/'media-cuda')
    chunk_file = directory/'run-02/rag/chunks.json'
    raw_chunks = chunk_file.read_bytes()
    chunks = tuple(VideoChunk.model_validate(c) for c in json.loads(raw_chunks))
    assert chunks_compatible(chunks, context.segments)
    selection = None
    for observation in context.observations:
        item = observation.source_item
        if item.source_type != 'ASR' or '时间复杂度' not in observation.text: continue
        segment = next(s for s in context.segments if s.segment_id == item.segment_id)
        quote = observation.text[:120]
        if quote and quote in segment.transcript[:400]:
            selection = observation, segment, quote
            break
    if selection is None: raise ValueError('no bounded cached quote; retain limitation')
    observation, segment, quote = selection
    item = observation.source_item
    hit = VideoEvidenceHit(start_ms=segment.start_ms,end_ms=segment.end_ms,source='ASR',snippet=quote,
        transcript=segment.transcript,source_revision=context.source_revision,segment_id=segment.segment_id,source_item_ids=segment.source_item_ids)
    arms = {}
    for arm in ['A','B']:
        h = list(harness())
        h[1].context, h[1].chunks = context, chunks
        h[5] = ConversationIdentity.create(1,42,'学习存储技术',AnalysisMode.LEARNING,str(uuid4()),context)
        async def source_search(*a,**kw):
            h[2].queries.append(kw.get('context','cached-source'))
            return (hit,)
        h[2].search_evidence = source_search
        h, http = mixed_harness(tuple(h))
        h[0]._mixed_enabled = arm == 'B'
        http.fact_override = {'answer':quote,'evidence':[{'candidateIndex':0,'timestampMs':item.timestamp_ms,'source':'ASR','content':quote,'claim':quote}]}
        http.payload = {'knowledge_blocks':[{'kind':'MODEL_KNOWLEDGE','text':'时间复杂度分析需要明确输入规模、基本操作和增长阶。'},
            {'kind':'INFERENCE','text':'单条引文不足以判断完整算法的复杂度推导，仍需确认其假设与步骤。'}]}
        with capture_follow_up_budget() as budget:
            answer = await ask(h,'作者关于时间复杂度的解释正确吗？')
        state = await h[4].store.load(h[5])
        assert state.turns[-1].answer_kind == ('MIXED' if arm == 'B' else 'VIDEO_GROUNDED')
        assert 'ASR' in answer and len(h[2].queries) == 1
        arms[arm] = {'kind':state.turns[-1].answer_kind,'stages':budget[0]['stages'],'fixture_tokens':budget[0]['charged_chat_tokens'],'retrieval_calls':1}
    assert hashlib.sha256(chunk_file.read_bytes()).digest() == hashlib.sha256(raw_chunks).digest()
    return {'real_source_fixture_only':True,'semantic_quality_measured':False,'source_revision':context.source_revision,
        'media_sha256':manifest['mediaSha256'],'chunks_sha256':hashlib.sha256(raw_chunks).hexdigest(),
        'context_segments':len(context.segments),'source_items':len(context.observations),'chunks':len(chunks),'arms':arms}


async def evaluate(output):
    if output.exists(): raise ValueError("output already exists; preserve previous results")
    dataset = ROOT / "datasets/agentic-rag/retrieval-need-synthetic-v1.json"
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    assert digest == dataset.with_suffix(".sha256").read_text().strip()
    from dovideo.application.conversation_memory import needs_rewrite
    cases = [c for c in json.loads(dataset.read_text(encoding="utf-8"))["cases"] if c["cohort"] == "synthetic_contract" and c["expected"] == "VIDEO_PLUS_KNOWLEDGE" and not needs_rewrite(c["input"]["original_question"])]
    cases += [{"id": "r2c-correctness", "input": {"original_question": MIXED_Q}},
              {"id": "r2c-textbook", "input": {"original_question": "作者的算法定义和标准知识相比有什么不足？"}},
              {"id": "r2c-tradeoff", "input": {"original_question": "作者的优化方法有什么技术问题？"}},
              {"id": "r2c-insufficient", "fault": "empty", "input": {"original_question": "作者没有解释的机制是否正确？"}},
              {"id": "r2c-knowledge-failure", "fault": "provider", "input": {"original_question": MIXED_Q}},
              {"id": "r2c-timeout", "fault": "timeout", "input": {"original_question": MIXED_Q}},
              {"id": "r2c-budget-denial", "fault": "budget", "input": {"original_question": MIXED_Q}}]
    rows = []
    for case in cases:
        def configure(h, http, *, on):
            h[0]._mixed_enabled = on
            h[0]._mixed_knowledge_model = MixedKnowledgeModelAdapter(h[0]._model._chat)
            if case.get("fault") == "empty":
                original = h[0]._retrieval.search_evidence
                async def empty(*a, **kw):
                    await original(*a, **kw)
                    return ()
                h[0]._retrieval.search_evidence = empty
            if case.get("fault") == "provider": http.statuses["MIXED_KNOWLEDGE"] = [503]
            if case.get("fault") == "timeout": http.errors["MIXED_KNOWLEDGE"] = TimeoutError()
            if case.get("fault") == "budget":
                h[0]._budget_settings = FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED, 20000)
                http.usage["FOLLOW_UP"] = {"total_tokens": 19000}
        a = await run_case(case, RetrievalNeedMode.ENABLED, configure=lambda h,http:configure(h,http,on=False), transport_factory=MixedHttp, capture_failure=True)
        b = await run_case(case, RetrievalNeedMode.ENABLED, configure=lambda h,http:configure(h,http,on=True), transport_factory=MixedHttp, capture_failure=True)
        assert a["calls"] == b["calls"] and b["memory_source_correct"]
        if not case.get("fault"): assert b["kind"] == "MIXED" and b["rendered_source_separation"]
        else: assert b["error"] and not b["memory_committed"]
        rows.append({"id":case["id"], "fault":case.get("fault"), "A":a, "B":b})
    # Three actual routes in a shared conversation, including an M1 referent.
    h, http = mixed_harness()
    conversation = []
    for q in ["作者讲了什么？", MIXED_Q, QUESTION, "它为什么更快？"]:
        with capture_follow_up_budget() as budget: await ask(h,q)
        state = await h[4].store.load(h[5])
        conversation.append({"kind":state.turns[-1].answer_kind, "stages":budget[0]["stages"]})
    assert [x["kind"] for x in conversation] == ["VIDEO_GROUNDED", "MIXED", "MODEL_KNOWLEDGE", "VIDEO_GROUNDED"]
    cached = await cached_source_pair()
    summary = {"fixture_only":True, "real_provider_run":False, "dataset_sha256":digest, "pairs":len(rows),
        "successful_mixed":sum(r["B"]["kind"] == "MIXED" for r in rows), "video_retrieval_counts_equal":True,
        "failures_B":dict(Counter(r["B"]["error"] for r in rows if r["B"]["error"])), "memory_source_correct":True,
        "arms":{arm:{"chat_attempts":sum(r[arm]["chat_attempts"] for r in rows), "fixture_chat_tokens":sum(r[arm]["fixture_chat_tokens"] for r in rows),
            "fixture_e2e_median_ms":statistics.median(r[arm]["fixture_e2e_ms"] for r in rows)} for arm in ["A","B"]},
        "normal_pairs_extra_knowledge_attempts":1, "normal_pair_extra_fixture_tokens":20}
    output.mkdir(parents=True)
    (output/'comparison.json').write_text(json.dumps({"summary":summary,"rows":rows,"conversation":conversation,"cached_source_pair":cached},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    p=argparse.ArgumentParser(); p.add_argument('--output',type=Path,required=True)
    asyncio.run(evaluate(p.parse_args().output))
