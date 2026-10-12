"""Fixture A/B through real Follow-up, M1, Chat transport, LongContext and Hybrid.

Provider answers/usage, vector index and reranker are fixtures. No remote calls.
This measures execution contracts, not semantic quality or real model savings.
"""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests/application")]
from test_m1_conversation_memory import harness, ask, TEXT
from test_r2b_direct_follow_up import enabled, ANSWER
from test_r2a_follow_up_integration import FixtureHttp
from dovideo.application.follow_up_budget import capture_follow_up_budget
from dovideo.application.retrieval_need import RetrievalNeedSettings, RetrievalNeedMode, capture_retrieval_need
from dovideo.application.long_context import LongVideoContextService
from dovideo.application.retrieval import VideoEvidenceRetrievalService
from dovideo.application.adaptive_retrieval import AdaptiveRetrievalService, AdaptiveRetrievalSettings, RoutingSuggestion
from dovideo.application.ports.retrieval import RerankerResult
from dovideo.application.sparse_retrieval import BM25Retriever
from dovideo.infrastructure.providers.local_embedding import LocalTfidfEmbeddingAdapter
from dovideo.presentation.composition import LocalRetrievalPlanner, InMemoryVectorIndex
from dovideo.infrastructure.providers import OpenAICompatibleChatClient, ProviderConfig
from dovideo.infrastructure.providers.follow_up import GroundedFollowUpModelAdapter, DirectFollowUpModelAdapter
from dovideo.infrastructure.providers.conversation_memory import ConversationModelAdapter


async def run_case(case, mode, *, configure=None, transport_factory=None, capture_failure=False):
    h, _ = enabled()
    # Establish genuine M1 discussion; preparation is excluded from measurements.
    h[0]._retrieval_need_settings = RetrievalNeedSettings()
    await ask(h, "作者讲了什么？")
    h[0]._retrieval_need_settings = RetrievalNeedSettings(mode)
    h[3].calls.clear()
    calls = Counter({k: 0 for k in ["chunk_read", "search_evidence", "hybrid_search", "retrieve", "index",
        "planner", "embedding", "vector_query_spy", "bm25", "reranker", "m2_planner", "chunking"]})
    def spy(obj, method, name, sync=False):
        original = getattr(obj, method)
        if sync:
            def wrapped(*a, **kw):
                calls[name] += 1
                return original(*a, **kw)
        else:
            async def wrapped(*a, **kw):
                calls[name] += 1
                return await original(*a, **kw)
        setattr(obj, method, wrapped)
    embedding = LocalTfidfEmbeddingAdapter().fit([TEXT])
    vector = await embedding.embed(TEXT)
    h[1].chunks = (h[1].chunks[0].model_copy(update={"embedding": vector}),)
    planner, index, sparse = LocalRetrievalPlanner(), InMemoryVectorIndex(), BM25Retriever()
    await index.upsert(42, h[1].chunks)
    class Reranker:
        async def rerank(self, query, documents):
            calls["reranker"] += 1
            return tuple(RerankerResult(d.candidate_id, 1.0) for d in documents)
    class M2:
        async def suggest_retrieval(self, query):
            calls["m2_planner"] += 1
            return RoutingSuggestion(retrieval_route="SINGLE_QUERY", reason_code="SINGLE_FACT", sub_queries=[])
    class Chunking:
        async def process(self, segments):
            calls["chunking"] += 1
            raise AssertionError("durable chunks must be reused")
    spy(planner, "plan_retrieval", "planner")
    spy(embedding, "embed", "embedding")
    spy(index, "search", "vector_query_spy")
    spy(sparse, "rank", "bm25", sync=True)
    hybrid = VideoEvidenceRetrievalService(planner, embedding, index, sparse=sparse, reranker=Reranker())
    for method in ["search", "retrieve", "index"]: spy(hybrid, method, "hybrid_search" if method == "search" else method)
    adaptive = AdaptiveRetrievalService(hybrid, M2(), AdaptiveRetrievalSettings(enabled=True))
    long = LongVideoContextService(chunking_service=Chunking(), retrieval_service=adaptive)
    spy(long, "search_evidence", "search_evidence")
    spy(h[1], "load_chunks", "chunk_read")
    h[0]._retrieval = long
    class Transport(FixtureHttp):
        async def post(self, url, **kw):
            if "You have not viewed" in kw["json"]["messages"][0]["content"]:
                self.calls.append(kw["json"])
                from dovideo.infrastructure.providers import ProviderHttpResponse
                return ProviderHttpResponse(200, {"choices": [{"message": {"content": json.dumps({"answer": ANSWER}, ensure_ascii=False)}}], "usage": {"total_tokens": 20}})
            return await super().post(url, **kw)
    http = (transport_factory or Transport)(h[3])
    chat = OpenAICompatibleChatClient(ProviderConfig(base_url="https://example.test/v1", model="fixture", max_attempts=1), client=http)
    h[0]._model = GroundedFollowUpModelAdapter(chat)
    h[0]._direct_model = DirectFollowUpModelAdapter(chat)
    h[4].model = ConversationModelAdapter(chat)
    if configure is not None:
        configure(h, http)
    started = time.perf_counter()
    error = None
    with capture_follow_up_budget() as budget, capture_retrieval_need() as decisions:
        try:
            answer = await ask(h, case["input"]["original_question"])
        except Exception as exc:
            from dovideo.application.follow_up import FollowUpFailure
            if not capture_failure or not isinstance(exc, FollowUpFailure):
                raise
            error, answer = exc.category, ""
    state = await h[4].store.load(h[5])
    expected_kind = "MODEL_KNOWLEDGE" if answer == ANSWER else ("MIXED" if answer.startswith("## 视频事实") else "VIDEO_GROUNDED")
    return {"kind": state.turns[-1].answer_kind if not error else None, "error": error,
        "memory_committed": state.version > 1, "calls": dict(calls), "chat_attempts": len(http.calls),
        "fixture_chat_tokens": budget[0]["charged_chat_tokens"], "fixture_e2e_ms": (time.perf_counter()-started)*1000,
        "direct_no_citations": answer == ANSWER if state.turns[-1].answer_kind == "MODEL_KNOWLEDGE" else None,
        "memory_source_correct": state.turns[-1].answer_kind == expected_kind if not error else state.version == 1,
        "stages": budget[0]["stages"], "rendered_source_separation": answer.startswith("## 视频事实") and "## 通用知识与推理" in answer,
        "fixture_reported_usage": getattr(http, "usage_records", None),
        "decision": decisions[0].decision.model_dump(mode="json") if decisions else None}


async def evaluate(output):
    if output.exists(): raise ValueError("output exists; preserve earlier evaluation")
    dataset = ROOT / "datasets/agentic-rag/retrieval-need-synthetic-v1.json"
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    assert digest == dataset.with_suffix(".sha256").read_text().strip()
    cases = json.loads(dataset.read_text(encoding="utf-8"))["cases"]
    # Self-contained inputs only; dependent/rewrite safety is covered by dedicated tests.
    from dovideo.application.conversation_memory import needs_rewrite
    cases = [c for c in cases if c["cohort"] == "synthetic_contract" and not needs_rewrite(c["input"]["original_question"])]
    rows = []
    for case in cases:
        a, b = await run_case(case, RetrievalNeedMode.OFF), await run_case(case, RetrievalNeedMode.ENABLED)
        assert b["memory_source_correct"]
        if case["expected"] == "DIRECT_LLM":
            assert b["kind"] == "MODEL_KNOWLEDGE" and not any(b["calls"].values()) and b["direct_no_citations"]
        else:
            assert b["kind"] == "VIDEO_GROUNDED" and b["calls"] == a["calls"]
        rows.append({"id": case["id"], "expected": case["expected"], "A": a, "B": b})
    summary = {"status": "PASS", "fixture_only": True, "real_provider_run": False, "dataset_sha256": digest,
        "cases": len(rows), "cohorts": dict(Counter(r["expected"] for r in rows)),
        "correct_direct": sum(r["B"]["kind"] == "MODEL_KNOWLEDGE" for r in rows), "video_mixed_false_direct": 0,
        "direct_retrieval_calls": 0, "direct_no_citations_rate": 1.0, "memory_source_correct_rate": 1.0,
        "errors": 0, "timeouts": 0, "budget_denials": 0,
        "arms": {arm: {"chat_attempts": sum(r[arm]["chat_attempts"] for r in rows),
            "fixture_chat_tokens": sum(r[arm]["fixture_chat_tokens"] for r in rows),
            "fixture_e2e_median_ms": statistics.median(r[arm]["fixture_e2e_ms"] for r in rows)} for arm in ["A", "B"]}}
    output.mkdir(parents=True)
    (output / "comparison.json").write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(evaluate(parser.parse_args().output))
