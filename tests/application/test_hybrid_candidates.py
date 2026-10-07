import asyncio
from collections import Counter

import pytest

import dovideo.application.retrieval as retrieval_module
from dovideo.application.retrieval import VideoEvidenceRetrievalService
from dovideo.application.errors import BudgetExceededError
from dovideo.application.value_objects import VectorHit
from dovideo.domain import CHUNKING_CONTRACT_VERSION, VideoChunk, VideoRetrievalIntent, VideoSegment
from dovideo.presentation.composition import InMemoryVectorIndex


class Planner:
    async def plan_retrieval(self, query):
        return VideoRetrievalIntent(semantic_query=query, keywords=(query,), visual_keywords=(query,))


class Embedding:
    async def embed(self, query):
        return (1.0, 0.0)


class Metrics:
    def __init__(self):
        self.counts = Counter()
        self.values = {}
    def increment(self, name, amount=1):
        self.counts[name] += amount
    def observe(self, name, value):
        self.values[name] = value
    def counter_value(self, name):
        return self.counts[name]


def chunk(start, transcript="", ocr=(), **updates):
    segment = VideoSegment(start_ms=start, end_ms=start + 60_000, transcript=transcript, ocr_texts=ocr)
    return VideoChunk(start_ms=start, end_ms=start + 300_000, raw_segments=(segment,),
                      embedding=(1.0, 0.0)).model_copy(update=updates)


@pytest.mark.asyncio
async def test_healthy_remote_gating_never_cosines_nonhits(monkeypatch):
    class Index:
        async def search(self, media, vector, *, limit, **scope):
            return (VectorHit(300_000, 600_000, .01),)
    def forbidden(*args):
        raise AssertionError("normal remote search must not call local cosine")
    monkeypatch.setattr(retrieval_module, "cosine_similarity", forbidden)
    service = VideoEvidenceRetrievalService(Planner(), Embedding(), Index())
    hits = await service.search(1, "unmatched", (chunk(0, "first"), chunk(300_000, "second")))
    assert [h.start_ms for h in hits] == [300_000]


@pytest.mark.asyncio
async def test_healthy_empty_is_not_outage_or_local_dense():
    service = VideoEvidenceRetrievalService(Planner(), Embedding(), InMemoryVectorIndex(), Metrics())
    assert await service.search(1, "no match", (chunk(0, "unrelated"),)) == ()
    assert service._telemetry.counts["vectorStoreFallbacks"] == 0


@pytest.mark.asyncio
async def test_remote_failure_local_fallback_and_sparse_only_embedding_failure():
    class FailingIndex:
        async def search(self, *args, **kwargs):
            raise RuntimeError("down")
    metrics = Metrics()
    service = VideoEvidenceRetrievalService(Planner(), Embedding(), FailingIndex(), metrics)
    assert await service.search(1, "no match", (chunk(0, "unrelated"),))
    assert metrics.counts["vectorStoreFallbacks"] == 1
    class FailingEmbedding:
        async def embed(self, query):
            raise RuntimeError("down")
    service = VideoEvidenceRetrievalService(Planner(), FailingEmbedding(), FailingIndex(), metrics)
    hits = await service.search(1, "QKV", (chunk(0, "irrelevant"), chunk(300_000, "QKV")))
    assert [h.start_ms for h in hits] == [300_000]
    assert metrics.counts["embeddingFallbacks"] == 1
    assert metrics.counts["vectorStoreFallbacks"] == 1


@pytest.mark.asyncio
async def test_sparse_failure_dense_only_and_strict_rejection():
    from dovideo.infrastructure.r4_runtime import _StrictRetrievalService, R4ProviderFallbackError
    class BrokenSparse:
        def rank(self, *args, **kwargs):
            raise RuntimeError("bug")
    for service_type in (VideoEvidenceRetrievalService, _StrictRetrievalService):
        metrics = Metrics()
        service = service_type(Planner(), Embedding(), InMemoryVectorIndex(), telemetry=metrics, sparse=BrokenSparse())
        if service_type is _StrictRetrievalService:
            # Bypass only the production 1024-dimensional embedding guard.
            service._embed = Embedding().embed
            with pytest.raises(R4ProviderFallbackError):
                await service.search(None, "q", (chunk(0, "text"),))
        else:
            assert await service.search(None, "q", (chunk(0, "text"),))
        assert metrics.counts["sparseFallbacks"] == 1


@pytest.mark.asyncio
async def test_overlap_dedup_keeps_best_parent_and_provenance():
    s = VideoSegment(start_ms=240_000, end_ms=300_000, transcript="QKV", segment_id="stable", source_revision="a" * 64)
    chunks = (chunk(0, raw_segments=(s,), chunk_id="best", source_revision=s.source_revision),
              chunk(240_000, raw_segments=(s,), chunk_id="second", source_revision=s.source_revision))
    service = VideoEvidenceRetrievalService(Planner(), Embedding(), InMemoryVectorIndex())
    hits = await service.search(None, "QKV", chunks)
    assert len(hits) == 1
    assert hits[0].segment_id == "stable" and hits[0].chunk_id == "best"
    assert hits[0].source_revision == s.source_revision
    assert len(await service.retrieve(None, "QKV", chunks)) == 1


@pytest.mark.asyncio
async def test_inmemory_scope_filters_stale_before_candidate_cap():
    index = InMemoryVectorIndex()
    stale = chunk(0, "stale", source_revision="a" * 64, chunking_version="old", chunk_id="old")
    current = chunk(240_000, "QKV", source_revision="a" * 64, chunking_version=CHUNKING_CONTRACT_VERSION, chunk_id="new")
    await index.upsert(1, (stale, current))
    hits = await index.search(1, (1.0, 0.0), limit=1, source_revision="a" * 64, chunking_version=CHUNKING_CONTRACT_VERSION)
    assert hits[0].chunk_id == "new"
    assert (await index.search(1, (1.0, 0.0), limit=1))[0].chunk_id == "old"


@pytest.mark.asyncio
async def test_semantic_candidate_without_lexical_match_and_legacy_dedup():
    class SemanticIndex:
        async def search(self, media, vector, **kwargs):
            return (VectorHit(300_000, 600_000, .7),)
    service = VideoEvidenceRetrievalService(Planner(), Embedding(), SemanticIndex())
    hits = await service.search(1, "meaning-only paraphrase", (chunk(0, "distractor"), chunk(300_000, "independent wording")))
    assert [h.start_ms for h in hits] == [300_000]
    original = VideoSegment(start_ms=240_000, end_ms=300_000, transcript="same")
    repeat = original.model_copy(update={"start_ms": 300_000, "end_ms": 360_000})
    chunks = (chunk(0, raw_segments=(original,)), chunk(240_000, raw_segments=(original.model_copy(), repeat)))
    hits = await service.search(None, "same", chunks)
    assert sorted(h.start_ms for h in hits) == [240_000, 300_000]


@pytest.mark.asyncio
async def test_rank_fusion_and_segment_order_do_not_use_remote_absolute_scale():
    chunks = (chunk(0, "one"), chunk(300_000, "two"))
    outputs = []
    for scores in ((.9, .8), (1000.0, -1000.0)):
        class Index:
            async def search(self, media, vector, **kwargs):
                return tuple(VectorHit(c.start_ms, c.end_ms, score) for c, score in zip(chunks, scores))
        outputs.append(await VideoEvidenceRetrievalService(Planner(), Embedding(), Index()).search(1, "unmatched", chunks))
    assert outputs[0] == outputs[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["embed", "search", "rerank", "sparse"])
@pytest.mark.parametrize("error", [BudgetExceededError("budget"), asyncio.CancelledError()])
async def test_budget_and_cancel_propagate_through_every_arm(arm, error):
    class Failing:
        async def embed(self, *args):
            raise error
        async def search(self, *args, **kwargs):
            raise error
        async def rerank(self, *args):
            raise error
        def rank(self, *args, **kwargs):
            raise error
    bad = Failing()
    service = VideoEvidenceRetrievalService(Planner(), bad if arm == "embed" else Embedding(),
              bad if arm == "search" else InMemoryVectorIndex(),
              sparse=bad if arm == "sparse" else None, reranker=bad if arm == "rerank" else None)
    with pytest.raises(type(error)):
        await service.search(1 if arm == "search" else None, "QKV", (chunk(0, "QKV"),))
