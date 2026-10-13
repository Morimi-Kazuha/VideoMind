"""V2 extension of the optimization benchmark; private natural Follow-up runs."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from badcase_registry import BadCaseRegistry, identity
from run_optimization_benchmark import ROOT, environment, ObservedEmbedding, Metrics
from run_v1_pilot import golden, fresh, load
from run_r5_offline import targets, visibility
from dovideo.application.evaluation_runner import EvaluationArtifactWriter
from dovideo.application.follow_up import GroundedFollowUpService, FollowUpFailure
from dovideo.application.long_context import LongVideoContextService
from dovideo.application.adaptive_retrieval import AdaptiveRetrievalService, AdaptiveRetrievalSettings
from dovideo.application.retrieval_need import RetrievalNeedMode, RetrievalNeedSettings, capture_retrieval_need
from dovideo.application.evidence_reflection import ReflectionMode, capture_evidence_reflection
from dovideo.application.retrieval_repair import FeedbackRetrievalMode, TrustedRepairScope, capture_feedback_retrieval
from dovideo.application.evidence_projection import source_payload
from dovideo.application.retrieval_budget import capture_retrieval_budget
from dovideo.application.follow_up_budget import FollowUpBudgetMode, FollowUpBudgetSettings, capture_follow_up_budget, estimate_chat_input
from dovideo.application.retrieval_observation import capture_retrieval
from dovideo.domain import AnalysisMode, VideoChunk
from dovideo.infrastructure.providers.config import ProviderConfig, ModelRequestSettings
from dovideo.infrastructure.providers.model import OpenAICompatibleChatClient, RetrievalPlannerModelAdapter
from dovideo.infrastructure.providers.follow_up import GroundedFollowUpModelAdapter, DirectFollowUpModelAdapter, MixedKnowledgeModelAdapter
from dovideo.infrastructure.providers.evidence_reflection import EvidenceReflectionModelAdapter
from dovideo.infrastructure.providers.embedding import OpenAICompatibleEmbeddingAdapter
from dovideo.infrastructure.providers.reranker import RerankerConfig, SiliconFlowRerankerAdapter
from dovideo.infrastructure.providers.http import StdlibAsyncJsonPostClient
from dovideo.infrastructure.vector.qdrant import QdrantVectorIndex
from dovideo.infrastructure.r4_runtime import _StrictRetrievalService
from dovideo.presentation.composition import embedding_provider_config_from_environment

ARMS = {
    "A": (RetrievalNeedMode.OFF, ReflectionMode.OFF, FeedbackRetrievalMode.OFF),
    "B": (RetrievalNeedMode.ENABLED, ReflectionMode.OFF, FeedbackRetrievalMode.OFF),
    "C": (RetrievalNeedMode.ENABLED, ReflectionMode.SHADOW, FeedbackRetrievalMode.SHADOW),
    "D": (RetrievalNeedMode.ENABLED, ReflectionMode.SHADOW, FeedbackRetrievalMode.ENABLED),
}
_call_stage = ContextVar("evaluation_provider_stage", default="UNKNOWN")


def followup_schedule(cases, selected, arm_override="", max_requests=None):
    if len(set(selected)) != len(selected):
        raise ValueError("duplicate selected cases")
    forced = tuple(arm_override.split(",")) if arm_override else ()
    if any(arm not in ARMS for arm in forced) or len(set(forced)) != len(forced):
        raise ValueError("invalid or duplicate arms")
    by_id = {case["caseId"]: case for case in cases}
    plan = []
    for cid in selected:
        if cid not in by_id:
            raise ValueError("unknown case")
        case = by_id[cid]
        arms = forced or (("A", "B") if case.get("expected_route") in {"DIRECT_LLM", "VIDEO_PLUS_KNOWLEDGE"} else ("C", "D"))
        plan.extend((case, arm) for arm in arms)
    if max_requests is not None and (type(max_requests) is not int or max_requests < 1 or len(plan) > max_requests):
        raise ValueError("request plan exceeds explicit Follow-up ceiling")
    return plan


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2,
        default=lambda v: v.model_dump(mode="json") if hasattr(v, "model_dump") else str(v)), encoding="utf-8")


def prepare(base):
    manifest, context, dataset, annotations, validation = golden(base / "draft-v1", base / "media-cuda")
    chunks = tuple(VideoChunk.model_validate(c) for c in load(base / "run-02/rag/chunks.json"))
    index = load(base / "run-02/rag/index.json")
    TrustedRepairScope.create(index["mediaId"], context, chunks)
    if len(chunks) != index["points"] or any(len(c.embedding) != index["dimension"] for c in chunks):
        raise ValueError("cached index dimensions mismatch")
    from dovideo.application.retrieval import vector_scope
    if index["scope"] != vector_scope(chunks):
        raise ValueError("index scope mismatch")
    legacy = [dict(c.model_dump(mode="json", by_alias=True), cohort="V1_LEGACY", review_status="DRAFT_NOT_HUMAN_APPROVED") for c in dataset.cases]
    # New candidate questions, with source links inherited from relevant legacy
    # questions. No newly generated answer is promoted to Gold.
    specs = [
        ("v2-01", "不考虑视频，请解释时间复杂度与运行秒数的区别。", None, "DIRECT_LLM"),
        ("v2-02", "仅从通用知识角度，请解释二分查找为什么是对数阶。", None, "DIRECT_LLM"),
        ("v2-03", "视频里基本运算次数与问题规模是什么关系？", "tc-04", "VIDEO_RAG"),
        ("v2-04", "视频中递增版本与翻倍版本的循环次数和复杂度分别如何？", "tc-18", "VIDEO_RAG"),
        ("v2-05", "视频怎样计算逐个查找的平均比较次数？", "tc-15", "VIDEO_RAG"),
        ("v2-06", "视频中 3n+3 与 3n+1003 分别出现在哪里，阶数为何相同？", "tc-17", "VIDEO_RAG"),
        ("v2-07", "视频里的匹配算法为何上线后出问题，后来怎样改进？", "tc-19", "VIDEO_RAG"),
        ("v2-08", "作者对时间复杂度忽略常数项的解释正确吗？", "tc-10", "VIDEO_PLUS_KNOWLEDGE"),
        ("v2-09", "视频是否讲解了红黑树删除旋转的每一步？", "tc-20", "VIDEO_RAG"),
        ("v2-10", "视频中翻倍循环每轮如何变化，为什么不是指数阶？", "tc-12", "VIDEO_RAG"),
        ("v2-11", "作者对平均情况与最坏情况的解释是否合理？", "tc-15", "VIDEO_PLUS_KNOWLEDGE"),
        ("v2-12", "脱离视频，请解释大 O 是上界还是精确等式。", None, "DIRECT_LLM"),
    ]
    by_id = {c["caseId"]: c for c in legacy}
    extra = []
    for number, (cid, question, ref, route) in enumerate(specs):
        inherited = by_id[ref] if ref else {}
        extra.append(dict(caseId=cid, query=question, cohort="V2_HOLDOUT" if number >= 9 else "V2_DEVELOPMENT",
            expected_route=route, review_status="NEEDS_REVIEW", requiredFacts=inherited.get("requiredFacts", []),
            expectedEvidenceRefs=inherited.get("expectedEvidenceRefs", []), source_case=ref,
            label_note="Inherited candidate source links require question-specific independent review; no semantic scoring"))
    return context, chunks, index, legacy + extra, validation, manifest


class CampaignHttp:
    """One durable ledger for ALL provider attempts, including failed calls."""
    def __init__(self, output, args):
        self.inner = StdlibAsyncJsonPostClient()
        self.output, self.args = output, args
        self.records, self.tokens, self.cost_usd = [], 0, 0.0
        if args.prior_followup_run:
            prior = load(args.prior_followup_run / "campaign-ledger.json")
            self.records, self.tokens, self.cost_usd = prior["records"], prior["charged_tokens"], prior["reported_cost_usd_subtotal"]

    def persist(self):
        save(self.output / "campaign-ledger.json", dict(attempts=len(self.records), charged_tokens=self.tokens,
            reported_cost_usd_subtotal=self.cost_usd, cost_complete=all(r.get("cost_usd") is not None for r in self.records),
            cost_state="NOT_MEASURED" if not any(r.get("cost_usd") is not None for r in self.records) else "PARTIAL_OR_REPORTED",
            records=self.records))

    async def post(self, url, *, headers, json, timeout):
        reserve = estimate_chat_input(json.get("messages", [{"content": str(json.get("input", json.get("documents", "")))}])) + json.get("max_tokens", 4096)
        if len(self.records) >= self.args.max_http_attempts or self.tokens + reserve > self.args.max_total_tokens or self.cost_usd >= self.args.max_reported_cost_usd:
            from dovideo.application.errors import BudgetExceededError
            raise BudgetExceededError("V2 campaign admission denied")
        record = dict(component="embedding" if "embeddings" in url else "reranker" if "rerank" in url else "chat",
            stage=_call_stage.get(),
            model=json.get("model"), status=None, usage=None, cost_usd=None, token_state="ESTIMATED", charged_tokens=reserve)
        self.records.append(record)
        self.tokens += reserve
        self.persist()  # charged before network; interruption cannot reset budget
        start = time.perf_counter()
        try:
            response = await self.inner.post(url, headers=headers, json=json, timeout=timeout)
            record["status"] = response.status_code
            if isinstance(response.body, dict):
                record["provider"] = response.body.get("provider")
                choices = response.body.get("choices") or []
                record["finish_reason"] = choices[0].get("finish_reason") if choices else None
                record["content_present"] = bool(choices and choices[0].get("message", {}).get("content"))
            usage = response.body.get("usage", {}) if isinstance(response.body, dict) else {}
            record["usage"] = usage
            total = usage.get("total_tokens")
            if type(total) is int and total >= 0:
                self.tokens += total - reserve
                record.update(charged_tokens=total, token_state="PROVIDER")
            cost = usage.get("cost")
            if type(cost) in (float, int) and cost >= 0:
                record["cost_usd"] = cost
                self.cost_usd += cost
            return response
        finally:
            record["latency_ms"] = (time.perf_counter() - start) * 1000
            self.persist()


class CapturingChat:
    def __init__(self, inner):
        self.inner, self.calls = inner, []
        self.config = inner.config
    async def complete(self, messages, *, stage, **kwargs):
        row = dict(stage=stage, messages=messages, response=None)
        self.calls.append(row)
        started = time.perf_counter()
        token = _call_stage.set(stage)
        try:
            row["response"] = await self.inner.complete(messages, stage=stage, **kwargs)
            return row["response"]
        finally:
            row["latency_ms"] = (time.perf_counter() - started) * 1000
            _call_stage.reset(token)


async def execute(case, arm, context, chunks, index, config, http, args):
    from dovideo.application.conversation_memory import ConversationIdentity, ConversationMemoryService
    from dovideo.infrastructure.conversation_memory import InMemoryConversationMemoryStore
    from dovideo.infrastructure.providers.conversation_memory import ConversationModelAdapter
    metrics = Metrics()
    metrics.counter_value = lambda key: metrics.counts[key]
    inner = OpenAICompatibleChatClient(config, client=http,
        request_settings=ModelRequestSettings(max_tokens=args.output_tokens, reasoning_effort=args.reasoning_effort))
    chat = CapturingChat(inner)
    planner = RetrievalPlannerModelAdapter(chat)
    embedding = ObservedEmbedding(OpenAICompatibleEmbeddingAdapter(replace(embedding_provider_config_from_environment(required=True), max_attempts=1), client=http))
    vector = QdrantVectorIndex(base_url=os.environ.get("DOVIDEO_QDRANT_URL", "http://127.0.0.1:6333"),
        api_key=os.environ.get("DOVIDEO_QDRANT_API_KEY"), collection=index["collection"])
    rc = RerankerConfig.from_environment()
    reranker = SiliconFlowRerankerAdapter(replace(rc, max_attempts=1), client=http) if rc.enabled else None
    hybrid = _StrictRetrievalService(planner, embedding, vector, telemetry=metrics, reranker=reranker)
    class NoChunking:
        async def build(self, *a, **kw):
            raise AssertionError("must reuse existing chunks")
    class Checkpoint:
        async def load_context(self, media_id): return context
        async def load_chunks(self, media_id): return chunks
        async def load_result(self, key): return None
    retrieval_calls = []
    class ObservedContext(LongVideoContextService):
        async def search_evidence(self, media_id, selected_context, **kw):
            observation = dict(phase=getattr(self, "phase", "INITIAL"), query=selected_context.user_goal,
                hits=[], error=None)
            retrieval_calls.append(observation)
            started = time.perf_counter()
            try:
                hits = await super().search_evidence(media_id, selected_context, **kw)
                observation["hits"] = [h.model_dump(mode="json", by_alias=True) for h in hits]
                return hits
            except Exception as error:
                observation["error"] = type(error).__name__
                raise
            finally:
                observation["latency_ms"] = (time.perf_counter() - started) * 1000
        async def search_evidence_baseline(self, *a, **kw):
            previous = getattr(self, "phase", "INITIAL")
            self.phase = "REPAIR"
            try:
                return await super().search_evidence_baseline(*a, **kw)
            finally:
                self.phase = previous
    need, reflection, feedback = ARMS[arm]
    memory = ConversationMemoryService(InMemoryConversationMemoryStore(write_sources=True), ConversationModelAdapter(chat))
    service = GroundedFollowUpService(Checkpoint(), ObservedContext(NoChunking(), AdaptiveRetrievalService(hybrid, planner, AdaptiveRetrievalSettings(enabled=False))),
        GroundedFollowUpModelAdapter(chat), retrieval_need_settings=RetrievalNeedSettings(need),
        direct_model=DirectFollowUpModelAdapter(chat), mixed_knowledge_model=MixedKnowledgeModelAdapter(chat), mixed_enabled=True,
        reflection_model=EvidenceReflectionModelAdapter(chat), reflection_mode=reflection, feedback_mode=feedback,
        budget_settings=FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED, args.request_tokens), memory=memory)
    kwargs = {}
    if case.get("history"):
        from dovideo.application.conversation_memory import successful_turn, ConversationState
        cid = str(uuid4())
        ident = ConversationIdentity.create(1, index["mediaId"], context.user_goal or "视频学习", AnalysisMode.GENERAL, cid, context)
        await memory.store.acquire(ident, "seed")
        turns = [successful_turn(str(uuid4()), h["question"], h["answer"], context.source_revision) for h in case["history"]]
        await memory.store.commit(ident, 0, ConversationState(version=1, turns=turns), "seed")
        await memory.store.release(ident, "seed")
        kwargs = dict(user_id=1, conversation_id=cid, request_id=str(uuid4()))
    start, offset = time.perf_counter(), len(http.records)
    row = dict(case_id=case["caseId"], arm=arm, input_hash=identity(case), error=None, answer=None)
    with ExitStack() as stack:
        from dovideo.application.follow_up import capture_literal_location
        captures = {name: stack.enter_context(cm()) for name, cm in dict(literal_location=capture_literal_location, need=capture_retrieval_need,
            reflection=capture_evidence_reflection, repair=capture_feedback_retrieval, retrieval_budget=capture_retrieval_budget,
            budget=capture_follow_up_budget, initial_hits=capture_retrieval).items()}
        try:
            row["answer"] = await service.answer(index["mediaId"], case["query"], context.user_goal or "视频学习", AnalysisMode.GENERAL, **kwargs)
        except Exception as error:
            row["error"] = getattr(error, "category", type(error).__name__)
        row["trace"] = {name: [asdict(r) if hasattr(r, "__dataclass_fields__") else r for r in values]
                        for name, values in captures.items() if name != "initial_hits"}
        hits = captures["initial_hits"][0] if captures["initial_hits"] else ()
        row["initial_hits"] = [h.model_dump(mode="json", by_alias=True) for h in hits]
    row.update(e2e_ms=(time.perf_counter()-start)*1000, provider_attempts=http.records[offset:], model_calls=chat.calls,
               retrieval_calls=retrieval_calls, metrics=dict(metrics.counts), semantic_quality="NOT_MEASURED_PENDING_INDEPENDENT_REVIEW")
    projections = []
    for call in chat.calls:
        if call["stage"] == "FOLLOW_UP":
            content = call["messages"][-1]["content"]
            projections.append(json.loads(content.split("Input as JSON:\n")[-1])["retrievedSourceCandidates"])
    row["final_projection"] = projections[-1] if projections else []
    if row["trace"]["literal_location"]:
        row["final_projection"] = row["trace"]["literal_location"][-1]["projection"]
        row["application_stage"] = "CACHED_LITERAL_LOCATION"
    row["actual_route"] = "DIRECT_LLM" if any(c["stage"] == "DIRECT_FOLLOW_UP" for c in chat.calls) else "VIDEO_PLUS_KNOWLEDGE" if any(c["stage"] == "MIXED_KNOWLEDGE" or c["stage"] == "FOLLOW_UP" and c["messages"][-1]["content"].startswith("FACT EXTRACTION ONLY") for c in chat.calls) else "VIDEO_RAG"
    initial_hits = next((r["hits"] for r in retrieval_calls if r["phase"] == "INITIAL"), [])
    from dovideo.domain import VideoEvidenceHit
    resolved_query = next((r["query"] for r in retrieval_calls if r["phase"] == "INITIAL"), case["query"])
    prepared = service._prepare_candidates(context, tuple(VideoEvidenceHit.model_validate(h) for h in initial_hits), question=resolved_query)
    row["initial_projection"] = [source_payload(i, c.prompt_hit, context.observations) for i, c in enumerate(prepared)]
    reflection_payload = dict(question=resolved_query, scope="BOUNDED_ANSWER_INPUT", route=row["actual_route"], retrievedSourceCandidates=row["initial_projection"])
    row["reflection_input_chars"] = len(json.dumps(reflection_payload, ensure_ascii=False, separators=(",", ":"))) if row["trace"]["reflection"] else None
    row["executed_model_stages"] = [c["stage"] for c in chat.calls]
    row["quality_layers"] = dict(initial_chunk_ids=sorted({h.chunk_id for h in hits if h.chunk_id}),
        initial_segment_ids=sorted({h.segment_id for h in hits if h.segment_id}),
        final_segment_windows=[(p["startMs"], p["endMs"]) for p in row["final_projection"]],
        required_fact_in_prompt="NOT_MEASURED_REQUIRES_REVIEWED_FACT_SPANS",
        fact_correctly_used_in_answer="NOT_MEASURED_REQUIRES_INDEPENDENT_REVIEW")
    goals = targets(case, context)
    row["linked_source_excerpt_visibility_proxy"] = visibility(goals, row["final_projection"], context)
    row["findings"] = []
    # Source identities are factual contracts; these proxies are not semantic labels.
    for repair in row["trace"]["repair"]:
        if repair["state"] in {"SKIPPED_TIME", "SKIPPED_BUDGET", "NO_NEW_EVIDENCE", "INVALID_EVIDENCE", "NO_VALID_REPAIR"}:
            row["findings"].append(dict(category="REPAIR_NO_PROGRESS" if repair["state"] == "NO_NEW_EVIDENCE" else "REPAIR_SKIPPED",
                stage="REPAIR", expected="bounded repair when admissible", observed=repair["state"]))
    await inner.aclose()
    return row


async def run(args):
    if not 1 <= args.max_http_attempts <= 80 or not 1 <= args.max_total_tokens <= 200000 or not 0 < args.max_reported_cost_usd <= 2:
        raise ValueError("campaign limits exceed this authorized envelope")
    base = ROOT / "work/evaluation/optimization-v1-20261009"
    context, chunks, index, cases, validation, manifest = prepare(base)
    output = fresh(args.output)
    writer = EvaluationArtifactWriter(output)
    writer.ensure_writable()
    memory_cases = load(base / "memory-cases-v1.json")
    by_id = {c["caseId"]: c for c in cases}
    for m in memory_cases:
        c = dict(by_id[m["target_case"]], caseId=m["id"], query=m["question"], history=m["history"], cohort="V1_LEGACY")
        cases.append(c)
    selected = args.followup_cases.split(",") if args.followup_cases else ["tc-04", "tc-17", "v2-01", "v2-08", "tc-20", "tc-18"]
    plan = followup_schedule(cases, selected, getattr(args, "followup_arms", ""), getattr(args, "max_followup_requests", None))
    metadata = dict(run_id=output.name, dataset_version="agentic-rag-v2-draft-1", source_revision=context.source_revision,
        source_code_commit=subprocess.check_output(["git", "-c", f"safe.directory={ROOT.as_posix()}", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        source_code_hashes={str(p.relative_to(ROOT)): __import__("hashlib").sha256(p.read_bytes()).hexdigest()
                            for p in [*sorted((ROOT / "src/dovideo/application").glob("*.py")), *sorted((ROOT / "src/dovideo/infrastructure/providers").glob("*.py")), Path(__file__), ROOT / "tools/badcase_registry.py", ROOT / "tools/run_optimization_benchmark.py"]},
        source_hashes={str(p.relative_to(ROOT)): __import__("hashlib").sha256(p.read_bytes()).hexdigest() for p in
            [base / "media-cuda/context.json", base / "run-02/rag/chunks.json", base / "draft-v1/dataset.json"]},
        validation=validation, extraction_calls=0, chunk_embedding_calls=0, m2_enabled=False,
        request_plan=[dict(case_id=c["caseId"], arm=arm) for c, arm in plan],
        limits=dict(http_attempts=args.max_http_attempts, total_tokens=args.max_total_tokens, reported_cost_usd=args.max_reported_cost_usd,
                    request_tokens=args.request_tokens, deadline_seconds=60, output_tokens=args.output_tokens))
    save(output / "dataset.json", dict(cases=cases, independent_gold=False))
    save(output / "run.json", metadata)
    if not args.live:
        save(output / "summary.json", dict(status="PREPARED_NOT_LIVE", executed=0, semantic_quality="NOT_MEASURED"))
        return
    if not args.paid_authorized:
        raise ValueError("live requires explicit fresh paid authorization")
    sources = environment(args.env_file)
    if args.eval_provider_tag:
        sources.append(dict(kind="process_only_provider_override", previous=os.environ.get("DOVIDEO_MODEL_PROVIDER_TAG"), value=args.eval_provider_tag))
        os.environ["DOVIDEO_MODEL_PROVIDER_TAG"] = args.eval_provider_tag
    config = replace(ProviderConfig.from_environment(required=True), max_attempts=1)
    metadata.update(model=dict(model=config.model, provider_only=config.provider_only, transport=config.transport,
        reasoning_effort=args.reasoning_effort), environment_sources=sources)
    metadata["model_and_config_digest"] = identity(dict(model=metadata["model"], limits=metadata["limits"], arms={k:[x.value for x in v] for k,v in ARMS.items()}))
    save(output / "run.json", metadata)
    http, registry = CampaignHttp(output, args), BadCaseRegistry(output / "registry")
    rows = []
    for case, arm in plan:
        cid = case["caseId"]
        row = await execute(case, arm, context, chunks, index, config, http, args)
        path = output / f"{cid}-{arm}.json"
        save(path, row)
        registry.collect(row, artifact=path, metadata=metadata)
        rows.append(row)
        print(cid, arm, row["error"] or "DELIVERED", flush=True)
        save(output / "summary.json", dict(status="LIVE_PARTIAL", executed=len(rows), delivered=sum(r["answer"] is not None for r in rows),
            arms=dict(Counter(r["arm"] for r in rows)), errors=dict(Counter(r["error"] for r in rows if r["error"])),
            reflection_outcomes=dict(Counter(o["outcome"] for r in rows for o in r["trace"]["reflection"])),
            repair_states=dict(Counter(o["state"] for r in rows for o in r["trace"]["repair"])),
            quality="NOT_MEASURED_PENDING_REVIEW"))
        # Provider/config failures stop the batch; never silently switch to fixtures.
        if any(r.get("status") is None or r["status"] >= 400 for r in row["provider_attempts"]) or row["error"] in {"BudgetExceededError", "budget_exceeded"}:
            return
