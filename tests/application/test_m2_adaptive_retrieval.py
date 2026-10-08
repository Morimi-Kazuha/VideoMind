"""Harness, scope, ordering and M1 integration checks (deterministic ports)."""
import asyncio

import pytest

from dovideo.application.adaptive_retrieval import (
    AdaptiveRetrievalService, AdaptiveRetrievalSettings, RoutingSuggestion,
    RetrievalRoute, capture_adaptive_retrieval, baseline_retrieval,
)
from dovideo.application.errors import BudgetExceededError, DeadlineExceededError
from dovideo.application.execution_budget import AgentExecutionBudget
from dovideo.application.long_context import LongVideoContextService
from dovideo.application.retrieval import VideoEvidenceRetrievalService
from dovideo.application.retrieval_observation import capture_retrieval
from dovideo.domain import VideoSegment, VideoChunk, VideoEvidenceHit, VideoContext, CriticResult

QUERY = "作者最初选择 MySQL 的原因；后来改成 Redis 的依据"
PARTS = ["最初选择 MySQL 的原因", "后来改成 Redis 的依据"]


def suggestion(parts=None):
    return RoutingSuggestion(retrieval_route="BOUNDED_MULTI_QUERY", reason_code="TEMPORAL_CHANGE",
                             sub_queries=PARTS if parts is None else parts)


class Planner:
    def __init__(self, result=None, error=None):
        self.result = result or suggestion()
        self.error = error
        self.calls = []
    async def suggest_retrieval(self, query):
        self.calls.append(query)
        if self.error: raise self.error
        return self.result


def corpus():
    segments = tuple(VideoSegment(start_ms=i * 600_000, end_ms=i * 600_000 + 60_000,
                                  transcript=f"source {i}", segment_id=f"s{i}", source_revision="r1")
                     for i in range(12))
    chunks = tuple(VideoChunk(start_ms=s.start_ms, end_ms=s.end_ms, raw_segments=(s,),
                              source_revision="r1", chunking_version="v1", chunk_id=f"c{i}")
                   for i, s in enumerate(segments))
    hits = tuple(VideoEvidenceHit(start_ms=s.start_ms, end_ms=s.end_ms, transcript=s.transcript,
                                  snippet=s.transcript, source="ASR", segment_id=s.segment_id,
                                  source_revision="r1", chunk_id=f"c{i}") for i, s in enumerate(segments))
    return segments, chunks, hits


class Baseline:
    def __init__(self, batches=None):
        self.segments, self.chunks, self.hits = corpus()
        self.batches = batches or {PARTS[0]: self.hits[:4], PARTS[1]: self.hits[3:7]}
        self.calls = []
        self.after = None
    async def search(self, media, query, chunks):
        self.calls.append((media, query, tuple(chunks)))
        if self.after: await self.after()
        return self.batches.get(query, self.hits[:8])
    async def retrieve(self, media, query, chunks):
        self.calls.append((media, query, tuple(chunks)))
        return self.segments
    async def index(self, media, chunks):
        self.calls.append((media, "INDEX", tuple(chunks)))


def harness(settings=None, planner=None, baseline=None):
    baseline = baseline or Baseline()
    planner = planner or Planner()
    return AdaptiveRetrievalService(baseline, planner, settings or AdaptiveRetrievalSettings(enabled=True)), baseline, planner


@pytest.mark.asyncio
async def test_simple_and_disabled_keep_original_search_and_retrieve_contract():
    for enabled, query in ((False, QUERY), (True, "MySQL 是什么？")):
        service, base, plan = harness(AdaptiveRetrievalSettings(enabled=enabled))
        assert await service.search(42, query, base.chunks) == base.hits[:8]
        assert await service.retrieve(42, query, base.chunks) == base.segments  # no new eight-hit cap
        assert plan.calls == []
        assert [c[1] for c in base.calls] == [query, query]


@pytest.mark.asyncio
async def test_multi_round_robin_dedup_cap_scope_and_post_cap_coverage():
    service, base, plan = harness(AdaptiveRetrievalSettings(enabled=True, max_candidates=4))
    with capture_adaptive_retrieval() as trace:
        hits = await service.search(42, QUERY, iter(base.chunks))
    assert [h.segment_id for h in hits] == ["s0", "s3", "s1", "s4"]
    assert [c[1] for c in base.calls] == PARTS
    assert all(c[0] == 42 and c[2] == base.chunks for c in base.calls)
    assert trace[0].retrieval_call_count == 2 and trace[0].candidate_count == 8
    assert trace[0].coverage_status == "ALL_SUBQUERIES_HAVE_CANDIDATES"
    assert trace[0].subquery_candidate_counts == (3, 2)
    assert plan.calls == [QUERY]


@pytest.mark.asyncio
async def test_retrieve_maps_to_canonical_segments_and_index_is_delegated():
    service, base, _ = harness()
    result = await service.retrieve(42, QUERY, base.chunks)
    assert {s.segment_id for s in result} == {f"s{i}" for i in range(7)}
    assert all(s in base.segments for s in result)
    await service.index(42, base.chunks)
    assert base.calls[-1][1] == "INDEX"


@pytest.mark.asyncio
@pytest.mark.parametrize("parts", [[], ["a"], ["x" * 501, PARTS[1]], [PARTS[0], PARTS[0]],
                                     [PARTS[0], PARTS[1], "第三目标", "第四目标"],
                                     ["MySQL 导致故障", PARTS[1]], ["other_media_id=2", PARTS[1]]])
async def test_invalid_model_construct_cannot_bypass_policy(parts):
    value = RoutingSuggestion.model_construct(retrieval_route=RetrievalRoute.BOUNDED_MULTI_QUERY,
                                               reason_code="TEMPORAL_CHANGE", sub_queries=parts)
    service, base, _ = harness(planner=Planner(value))
    with capture_adaptive_retrieval() as trace:
        assert await service.search(42, QUERY, base.chunks) == base.hits[:8]
    assert len(base.calls) == 1 and base.calls[0][1] == QUERY
    assert trace[0].decision.fallback == "INVALID_PLAN"


@pytest.mark.asyncio
async def test_provider_failure_and_reduced_capacity_fallback():
    for settings, planner, reason in (
        (AdaptiveRetrievalSettings(enabled=True), Planner(error=RuntimeError("private")), "PROVIDER_FAILURE"),
        (AdaptiveRetrievalSettings(enabled=True, max_queries=1), Planner(), "CAPACITY"),
        (AdaptiveRetrievalSettings(enabled=True, max_candidates=1), Planner(), "INVALID_PLAN"),
    ):
        service, base, _ = harness(settings, planner)
        with capture_adaptive_retrieval() as trace:
            await service.search(42, QUERY, base.chunks)
        assert len(base.calls) == 1 and trace[0].decision.fallback == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [BudgetExceededError("stop"), DeadlineExceededError("stop"), asyncio.CancelledError()])
async def test_planner_budget_deadline_cancellation_propagate(error):
    service, base, _ = harness(planner=Planner(error=error))
    with pytest.raises(type(error)):
        await service.search(42, QUERY, base.chunks)
    assert base.calls == []


@pytest.mark.asyncio
async def test_planning_timeout_falls_back_but_outer_deadline_does_not():
    class Slow(Planner):
        async def suggest_retrieval(self, query):
            await asyncio.sleep(1)
    service, base, _ = harness(AdaptiveRetrievalSettings(enabled=True, planning_timeout_seconds=.01), Slow())
    with capture_adaptive_retrieval() as trace:
        await service.search(42, QUERY, base.chunks)
    assert trace[0].decision.fallback == "PLANNING_TIMEOUT" and len(base.calls) == 1
    clock = [0.0]
    budget = AgentExecutionBudget(monotonic=lambda: clock[0])
    with budget.open(10000):
        clock[0] = 11
        with pytest.raises(DeadlineExceededError):
            await service.search(42, QUERY, base.chunks)


@pytest.mark.asyncio
async def test_deadline_or_cancellation_between_subqueries_stops_execution():
    service, base, _ = harness()
    async def cancel(): raise asyncio.CancelledError()
    base.after = cancel
    with pytest.raises(asyncio.CancelledError): await service.search(42, QUERY, base.chunks)
    assert len(base.calls) == 1
    base.calls.clear()
    clock = [0.0]
    async def expire(): clock[0] = 11
    base.after = expire
    with AgentExecutionBudget(monotonic=lambda: clock[0]).open(10000):
        with pytest.raises(DeadlineExceededError): await service.search(42, QUERY, base.chunks)
    assert len(base.calls) == 1


@pytest.mark.asyncio
async def test_partial_no_evidence_stale_revision_and_forged_excerpt_filtered():
    for second in ((), (corpus()[2][5].model_copy(update={"source_revision": "stale"}),),
                   (corpus()[2][5].model_copy(update={"transcript": "fabricated"}),)):
        base = Baseline({PARTS[0]: corpus()[2][:1], PARTS[1]: second})
        service, _, _ = harness(baseline=base)
        with capture_adaptive_retrieval() as trace:
            hits = await service.search(42, QUERY, base.chunks)
        assert len(hits) == 1 and trace[0].coverage_status == "PARTIAL_CANDIDATES"
        assert len(base.calls) == 2  # no speculative retry
    base = Baseline({PARTS[0]: (), PARTS[1]: ()})
    service, _, _ = harness(baseline=base)
    with capture_adaptive_retrieval() as trace:
        assert await service.search(42, QUERY, base.chunks) == ()
    assert trace[0].coverage_status == "NO_EVIDENCE"


@pytest.mark.asyncio
async def test_mixed_source_or_chunking_scope_fails_before_provider():
    for field in ("source_revision", "chunking_version"):
        service, base, planner = harness()
        chunks = (base.chunks[0], base.chunks[1].model_copy(update={field: "stale"}))
        with pytest.raises(ValueError): await service.search(42, QUERY, chunks)
        assert not base.calls and not planner.calls


@pytest.mark.asyncio
async def test_existing_retrieval_observer_receives_merged_batch():
    class Observed(Baseline):
        async def search(self, *args):
            from dovideo.application.retrieval_observation import observe_retrieval
            hits = await super().search(*args)
            observe_retrieval(lambda: hits)
            return hits
    service, base, _ = harness(baseline=Observed())
    with capture_retrieval() as captured:
        hits = await service.search(42, QUERY, base.chunks)
    assert captured == [hits]


@pytest.mark.asyncio
async def test_baseline_scope_is_task_local_and_resets():
    service, base, planner = harness()
    with baseline_retrieval(): await service.search(42, QUERY, base.chunks)
    assert not planner.calls
    await service.search(42, QUERY, base.chunks)
    assert planner.calls == [QUERY]


@pytest.mark.asyncio
async def test_long_context_initial_multi_short_bypass_and_critic_single():
    service, base, planner = harness()
    class Chunking:
        async def build(self, segments): return base.chunks
    long = LongVideoContextService(Chunking(), service)
    context = VideoContext(source="fixture", user_goal=QUERY, segments=base.segments)
    selected = await long.select_relevant(context)
    assert len(base.calls) == 2 and len(selected.segments) == 7
    base.calls.clear()
    await long.select_relevant(context.model_copy(update={"segments": base.segments[:1]}))
    assert base.calls == []
    await long.refine_for_critique(42, context, selected, CriticResult())
    assert [c[1] for c in base.calls if c[1] != "INDEX"] == [QUERY]
    assert planner.calls == [QUERY]


@pytest.mark.asyncio
async def test_m1_rewrite_precedes_router_and_guarded_memory_write():
    from test_m1_conversation_memory import harness as m1_harness, ask
    h = m1_harness()
    service, cp, _, chat, memory, identity = h
    from dovideo.application.conversation_memory import QueryRewrite
    rewritten = "视频中 Redis 和 MySQL 相比有什么不同？"
    chat.rewrite_result = QueryRewrite(standalone_query=rewritten, needs_clarification=False,
                                       clarification_question="")
    class Routed(Planner):
        async def suggest_retrieval(self, query):
            assert chat.calls[-1][0] == "QUERY_REWRITE"
            self.calls.append(query)
            return RoutingSuggestion(retrieval_route="BOUNDED_MULTI_QUERY", reason_code="COMPARISON",
                                     sub_queries=["Redis", "MySQL"])
    planner = Routed()
    class RetrievalIntent:
        async def plan_retrieval(self, query):
            from dovideo.domain import VideoRetrievalIntent
            return VideoRetrievalIntent(semantic_query=query, keywords=(query,))
    class Embedding:
        async def embed(self, text): return ()
    class Vector: pass
    hybrid = VideoEvidenceRetrievalService(RetrievalIntent(), Embedding(), Vector())
    adaptive = AdaptiveRetrievalService(hybrid, planner, AdaptiveRetrievalSettings(enabled=True))
    class NoBuild: pass
    service._retrieval = LongVideoContextService(NoBuild(), adaptive)
    await ask(h)
    answer = await ask(h, "它和 MySQL 相比有什么不同？")
    assert planner.calls == [rewritten]
    assert "视频证据" in answer
    assert len((await memory.store.load(identity)).turns) == 2
    assert chat.calls[-1][1]["question"] == "它和 MySQL 相比有什么不同？"
    chat.bad_evidence = True
    from dovideo.application.follow_up import FollowUpFailure
    with pytest.raises(FollowUpFailure) as error:
        await ask(h, "它和 MySQL 相比有什么不同？")
    assert error.value.category == "evidence_rejected"
    assert len((await memory.store.load(identity)).turns) == 2


@pytest.mark.parametrize("query,reason", [("为什么选择 MySQL？", "SINGLE_FACT"),
    (QUERY, "TEMPORAL_CHANGE"), ("解释缓存命中率并且说明失效策略", "MULTI_CONDITION"),
    ("比较 Redis 与 MySQL", "COMPARISON")])
def test_complexity_gate(query, reason):
    from dovideo.application.adaptive_retrieval import complexity_reason
    assert complexity_reason(query) == reason


@pytest.mark.asyncio
async def test_real_hybrid_ports_are_called_per_query_with_exact_media_revision_and_version():
    from dovideo.application.value_objects import VectorHit
    class Intent:
        calls = []
        async def plan_retrieval(self, query):
            from dovideo.domain import VideoRetrievalIntent
            self.calls.append(query)
            return VideoRetrievalIntent(semantic_query=query)
    class Embedding:
        async def embed(self, text): return (1., 0.)
    class Vector:
        calls = []
        async def search(self, media, embedding, **scope):
            self.calls.append((media, scope))
            return (VectorHit(start_ms=0, end_ms=60_000, score=1., chunk_id="c0",
                              source_revision="r1", chunking_version="v1"),)
    intent, vector = Intent(), Vector()
    hybrid = VideoEvidenceRetrievalService(intent, Embedding(), vector)
    adaptive = AdaptiveRetrievalService(hybrid, Planner(), AdaptiveRetrievalSettings(enabled=True))
    hits = await adaptive.search(42, QUERY, corpus()[1])
    assert intent.calls == PARTS
    assert len(hits) == 1  # actual RRF and dedup, same canonical source twice
    assert vector.calls == [(42, {"limit": 8, "source_revision": "r1", "chunking_version": "v1"})] * 2


@pytest.mark.asyncio
async def test_multicondition_executes_and_model_can_decline_decomposition():
    query = "解释缓存命中率并且说明失效策略"
    value = RoutingSuggestion(retrieval_route="BOUNDED_MULTI_QUERY", reason_code="MULTI_CONDITION",
                              sub_queries=["解释缓存命中率", "说明失效策略"])
    service, base, _ = harness(planner=Planner(value))
    await service.search(42, query, base.chunks)
    assert [c[1] for c in base.calls] == list(value.sub_queries)
    single = RoutingSuggestion(retrieval_route="SINGLE_HYBRID", reason_code="SINGLE_FACT", sub_queries=[])
    service, base, _ = harness(planner=Planner(single))
    await service.search(42, QUERY, base.chunks)
    assert [c[1] for c in base.calls] == [QUERY]


@pytest.mark.asyncio
async def test_policy_authorized_tool_search_remains_one_call_with_m2_enabled():
    from test_video_tools_x1c import _execution_context
    from dovideo.application.video_tools import VideoReadOnlyToolExecutor
    from dovideo.application.tool_contracts import ToolCall, ToolName, SearchEvidenceArguments
    service, base, planner = harness()
    class Chunking:
        async def build(self, segments): return base.chunks
    long = LongVideoContextService(Chunking(), service)
    tool = VideoReadOnlyToolExecutor(long)
    context = VideoContext(source="fixture", user_goal="goal", segments=base.segments)
    call = ToolCall(call_id="m2-test", tool_name=ToolName.SEARCH_EVIDENCE,
                    validated_arguments=SearchEvidenceArguments(query=QUERY, limit=8))
    await tool.execute(call, _execution_context(context))
    assert not planner.calls
    assert [c[1] for c in base.calls if c[1] != "INDEX"] == [QUERY]


@pytest.mark.asyncio
async def test_metrics_do_not_include_query_source_or_provider_failure_text(caplog):
    service, base, _ = harness(planner=Planner(error=RuntimeError("private-key-here")))
    with caplog.at_level("INFO", logger="dovideo.adaptive_retrieval"):
        await service.search(42, QUERY, base.chunks)
    assert "routing_fallback=PROVIDER_FAILURE" in caplog.text
    assert QUERY not in caplog.text and "private-key-here" not in caplog.text
    assert all(s.transcript not in caplog.text for s in base.segments)


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_share_observations_or_candidates():
    class Scoped(Baseline):
        async def search(self, media, query, chunks):
            await asyncio.sleep(0)
            return self.hits[:1] if media == 42 else self.hits[4:5]
    service, base, _ = harness(baseline=Scoped())
    async def request(media):
        with capture_adaptive_retrieval() as trace:
            hits = await service.search(media, QUERY, base.chunks)
        return hits, trace
    (left, left_trace), (right, right_trace) = await asyncio.gather(request(42), request(43))
    assert [h.segment_id for h in left] == ["s0"]
    assert [h.segment_id for h in right] == ["s4"]
    assert len(left_trace) == len(right_trace) == 1
    assert left_trace[0].retrieval_call_count == right_trace[0].retrieval_call_count == 2
