"""Frozen developer cases + unreviewed real Drafts; labels never enter prompts.

Default is an honest offline input/contract audit. --live uses only an already
configured Provider, one attempt per case, request ceiling and shared deadline.
No fixture output is claimed as semantic grading accuracy.
"""
import argparse
import asyncio
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dovideo.application.evidence_projection import source_payload
from dovideo.application.evidence_reflection import EvidenceReflection, ReflectionMode, capture_evidence_reflection
from dovideo.application.execution_budget import AgentExecutionBudget
from dovideo.application.follow_up_budget import FollowUpBudgetSettings, FollowUpBudgetMode, follow_up_budget_scope, capture_follow_up_budget
from dovideo.domain import VideoEvidenceHit, VideoChunk
from dovideo.infrastructure.providers import ProviderConfig, OpenAICompatibleChatClient
from dovideo.infrastructure.providers.evidence_reflection import EvidenceReflectionModelAdapter


def read(p): return json.loads(p.read_text(encoding="utf-8"))
def save(p,d): p.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding="utf-8")


def cases():
    def sample(key,q,text,status,gap=None,mixed=False,raw=None):
        hit=VideoEvidenceHit(start_ms=0,end_ms=10000,source="ASR",snippet="",transcript=text)
        return {"id":key,"label_origin":"DEVELOPER_SEMANTIC_JUDGMENT", "human_approved":False,
            "input":{"question":q,"candidates":[source_payload(0,hit)] if text else [],"mixed":mixed},
            "expected_status":status,"expected_gap":gap, "raw_segment_tail_only":raw}
    data=[
        sample("complete","作者为什么选 B？","作者选 B，因为它减少内存占用。","SUFFICIENT"),
        sample("related","作者怎样处理 Redis 缓存失效？","Redis 是内存数据库，作者使用 Redis。","INSUFFICIENT","TOPIC_RELATED_BUT_NOT_ANSWERABLE"),
        sample("cause","作者为什么选 B？","作者采用 B。A 和 B 均使用内存。","INSUFFICIENT","MISSING_CAUSAL_EVIDENCE"),
        sample("comparison","作者比较 A 和 B 的内存开销如何？","A 使用 10MB 内存。","INSUFFICIENT","MISSING_COMPARISON_SIDE"),
        sample("temporal","方案在初期、中期和后期如何变化？","初期使用 A，后期使用 C。","INSUFFICIENT","MISSING_TEMPORAL_CONTEXT"),
        sample("multi","作者在开头和结尾分别推荐什么？","开头推荐 A。","INSUFFICIENT","PARTIAL_FACT_COVERAGE"),
        sample("clipped","作者为什么选 B？","方案介绍。"*80+"作者选择 B 因为减少内存。","INSUFFICIENT","INSUFFICIENT_SOURCE_TEXT",raw="作者选择 B 因为减少内存。"),
        sample("empty","作者为什么选 B？","", "UNKNOWN"),
        sample("mixed","作者对 Redis 的解释正确吗？","作者解释：Redis 使用内存存储数据，可用于缓存。","SUFFICIENT",mixed=True),
        sample("legal_unrelated","作者如何实现红黑树删除旋转？","我们今天讨论时间复杂度。","INSUFFICIENT","TOPIC_RELATED_BUT_NOT_ANSWERABLE"),
    ]
    # Read real cached chunks; choose first evidence region only as a declared
    # partial-input stress case. Draft refs select inputs, never semantic labels.
    base=ROOT/"work/evaluation/optimization-v1-20261009"
    chunks=tuple(VideoChunk.model_validate(c) for c in read(base/"run-02/rag/chunks.json"))
    draft=read(base/"draft-v1/dataset.json")
    for case in draft["cases"]:
        if case["caseId"] not in {"tc-17","tc-18","tc-20"}: continue
        refs=case["expectedEvidenceRefs"]
        segment=next((s for c in chunks for s in c.raw_segments if refs and s.segment_id==refs[0]["segmentId"]),chunks[0].raw_segments[0])
        hit=VideoEvidenceHit(start_ms=segment.start_ms,end_ms=segment.end_ms,source="ASR",snippet="",transcript=segment.transcript,ocr_texts=segment.ocr_texts,
            source_revision=segment.source_revision,segment_id=segment.segment_id,source_item_ids=segment.source_item_ids)
        data.append({"id":case["caseId"]+"-partial-cache","label_origin":"UNREVIEWED_REAL_DRAFT",
            "human_approved":False,"input":{"question":case["query"],"candidates":[source_payload(0,hit)],"mixed":False},
            "expected_status":None,"expected_gap":None,"selection":"first Draft-linked segment only; not live retrieval; no whole-video absence label"})
    return data


async def run(args):
    from reflection_validation_support import load_chat_environment
    load_chat_environment(getattr(args, "env_file", []))
    out=args.output; out.mkdir(parents=True,exist_ok=False)
    data=cases(); save(out/"sidecar-v1.json",{"version":"r3-reflection-v1","cases":data})
    digest=hashlib.sha256((out/"sidecar-v1.json").read_bytes()).hexdigest()
    (out/"sidecar-v1.sha256").write_text(digest,encoding="ascii")
    config=ProviderConfig.from_environment() if args.live else None
    live=bool(config and config.api_key)
    adapter=EvidenceReflectionModelAdapter(OpenAICompatibleChatClient(replace(config,max_attempts=1,timeout_seconds=5))) if live else None
    results=[]
    for case in data:
        inp=case["input"]
        # Only this projection reaches model. All labels/raw tails stay outside.
        messages=EvidenceReflectionModelAdapter.messages(inp["question"],tuple(inp["candidates"]),mixed=inp["mixed"])
        started=time.perf_counter()
        with capture_evidence_reflection() as obs, capture_follow_up_budget() as usage:
            with AgentExecutionBudget.open(60000),follow_up_budget_scope(FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED,50000)):
                await EvidenceReflection(adapter,ReflectionMode.SHADOW).observe(inp["question"],tuple(inp["candidates"]),mixed=inp["mixed"],messages=messages,answer_reserve=0,time_reserve=0)
        observation=obs[0]; suggestion=observation.assessment.suggestion
        results.append({"id":case["id"],"outcome":observation.outcome,"status":observation.assessment.status,
            "scope_reason":observation.assessment.scope_reason,"assessment":suggestion.model_dump() if suggestion else None,
            "latency_ms":(time.perf_counter()-started)*1000,"usage":usage[0]})
    save(out/"results.json",results)
    measured=[(c,r) for c,r in zip(data,results) if c["label_origin"]=="DEVELOPER_SEMANTIC_JUDGMENT" and r["outcome"]=="ASSESSED"]
    matrix=Counter((c["expected_status"],r["status"]) for c,r in measured)
    gaps=[(c,r) for c,r in measured if c["expected_gap"]]
    save(out/"summary.json",{"provider_status":"MEASURED_DEVELOPER_SET" if live else "NOT_MEASURED",
        "provider_unavailable_reason":"No configured process Provider/API key" if args.live and not live else None,
        "sidecar_sha256":digest,"human_reviewed":0,"case_count":len(data),
        "confusion":[{"expected":a,"predicted":b,"count":n} for (a,b),n in matrix.items()] if measured else None,
        "false_sufficient":sum(c["expected_status"]=="INSUFFICIENT" and r["status"]=="SUFFICIENT" for c,r in measured) if measured else None,
        "false_insufficient":sum(c["expected_status"]=="SUFFICIENT" and r["status"]=="INSUFFICIENT" for c,r in measured) if measured else None,
        "gap_correct":sum(any(g["reason_code"]==c["expected_gap"] for g in r["assessment"]["missing_requirements"]) for c,r in gaps) if gaps else None,
        "gap_evaluated":len(gaps),"unavailable_or_skipped":sum(r["outcome"]!="ASSESSED" for r in results),
        "outcomes":dict(Counter(r["outcome"] for r in results)),
        "http_attempts":sum(r["usage"]["chat_http_attempts"] for r in results),
        "chat_tokens":sum(r["usage"]["charged_chat_tokens"] for r in results),
        "dto_invalid":sum(r["outcome"]=="INVALID_RESPONSE" for r in results),
        "timeout":sum(r["outcome"]=="TIMEOUT" for r in results),
        "budget_rejections":sum(r["usage"]["hypothetical_denials"] for r in results),
        "note":"Developer semantic labels are not independent Gold; real Drafts unscored. No labels, raw tail or reference answers in prompts."})
    print(json.dumps(read(out/"summary.json"),ensure_ascii=False,indent=2))


if __name__=="__main__":
    p=argparse.ArgumentParser(); p.add_argument("--output",required=True,type=Path); p.add_argument("--live",action="store_true")
    p.add_argument("--env-file", action="append", type=Path, default=[], help="Explicitly authorized project chat configuration; process only")
    asyncio.run(run(p.parse_args()))
