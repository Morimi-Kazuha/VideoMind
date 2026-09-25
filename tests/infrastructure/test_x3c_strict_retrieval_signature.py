from types import SimpleNamespace

import pytest

from dovideo.application.value_objects import VectorHit
from dovideo.infrastructure.r4_runtime import R4ProviderFallbackError, _StrictRetrievalService
from dovideo.infrastructure.vector.qdrant import QdrantVectorError


class _Vectors:
    async def search(self, media_id, query, *, limit):
        assert media_id == 17
        assert limit >= 2
        return (
            VectorHit(0, 10, 0.9, source_revision="a" * 64, chunk_id="one"),
            VectorHit(10, 20, 0.8, source_revision="a" * 64, chunk_id="two"),
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
        SimpleNamespace(chunk_id="one", source_revision="a" * 64),
        SimpleNamespace(chunk_id="two", source_revision="a" * 64),
    )
    scores = await service._vector_scores(17, (0.1, 0.2), chunks)
    assert len(scores) == 2
    assert set(scores.values()) == {0.9, 0.8}


def test_strict_retrieval_distinguishes_provider_and_vector_fallbacks() -> None:
    telemetry = _Telemetry()
    service = _StrictRetrievalService(object(), object(), _Vectors(), telemetry=telemetry)
    telemetry.counters["retrievalIntentFallbacks"] = 1
    with pytest.raises(R4ProviderFallbackError):
        service._raise_if_fallback((0, 0, 0))
    telemetry.counters = {"vectorStoreFallbacks": 1}
    with pytest.raises(QdrantVectorError):
        service._raise_if_fallback((0, 0, 0))
