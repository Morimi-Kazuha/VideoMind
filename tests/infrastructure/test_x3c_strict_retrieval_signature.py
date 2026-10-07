import asyncio

import pytest

from dovideo.application.errors import BudgetExceededError

from dovideo.application.value_objects import VectorHit
from dovideo.infrastructure.r4_runtime import R4ProviderFallbackError, _StrictRetrievalService
from dovideo.infrastructure.vector.qdrant import QdrantVectorError
from dovideo.domain import VideoChunk, CHUNKING_CONTRACT_VERSION


class _Vectors:
    async def search(self, media_id, query, *, limit, source_revision=None, chunking_version=None):
        assert media_id == 17
        assert limit >= 2
        assert source_revision == "a" * 64
        assert chunking_version == CHUNKING_CONTRACT_VERSION
        return (
            VectorHit(0, 10, 0.9, source_revision="a" * 64, chunk_id="one", chunking_version=CHUNKING_CONTRACT_VERSION),
            VectorHit(10, 20, 0.8, source_revision="a" * 64, chunk_id="two", chunking_version=CHUNKING_CONTRACT_VERSION),
            VectorHit(20, 30, 1.0, source_revision="b" * 64, chunk_id="stale"),
        )


class _Telemetry:
    def __init__(self):
        self.counters = {}

    def counter_value(self, _name):
        return self.counters.get(_name, 0)

    def observe(self, _name, _value):
        pass


@pytest.mark.asyncio
async def test_strict_retrieval_forwards_prepared_chunk_scope_to_base() -> None:
    telemetry = _Telemetry()
    service = _StrictRetrievalService(object(), object(), _Vectors(), telemetry=telemetry)
    chunks = (
        VideoChunk(start_ms=0, end_ms=10, chunk_id="one", source_revision="a" * 64, chunking_version=CHUNKING_CONTRACT_VERSION),
        VideoChunk(start_ms=10, end_ms=20, chunk_id="two", source_revision="a" * 64, chunking_version=CHUNKING_CONTRACT_VERSION),
    )
    scores = await service._dense_candidates(17, (0.1, 0.2), chunks)
    assert len(scores) == 2
    assert {c.score for c in scores} == {0.9, 0.8}


def test_strict_retrieval_distinguishes_provider_and_vector_fallbacks() -> None:
    telemetry = _Telemetry()
    service = _StrictRetrievalService(object(), object(), _Vectors(), telemetry=telemetry)
    telemetry.counters["retrievalIntentFallbacks"] = 1
    with pytest.raises(R4ProviderFallbackError):
        service._raise_if_fallback((0, 0, 0, 0, 0))
    telemetry.counters = {"vectorStoreFallbacks": 1}
    with pytest.raises(QdrantVectorError):
        service._raise_if_fallback((0, 0, 0, 0, 0))


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [BudgetExceededError("budget"), asyncio.CancelledError()])
async def test_strict_index_verification_preserves_control_errors(error) -> None:
    class Vectors:
        async def upsert(self, media_id, chunks):
            pass

        async def search(self, *args, **kwargs):
            raise error

    class Telemetry(_Telemetry):
        def increment(self, name, amount=1):
            self.counters[name] = self.counters.get(name, 0) + amount

    telemetry = Telemetry()
    service = _StrictRetrievalService(object(), object(), Vectors(), telemetry=telemetry)
    chunks = (
        VideoChunk(start_ms=0, end_ms=300_000, embedding=(1.0,)),
        VideoChunk(start_ms=240_000, end_ms=540_000, embedding=(1.0,)),
    )
    with pytest.raises(type(error)) as caught:
        await service.index(17, chunks)
    assert caught.value is error
    assert telemetry.counter_value("vectorStoreFallbacks") == 0
