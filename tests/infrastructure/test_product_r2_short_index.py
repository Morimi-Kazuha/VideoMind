import pytest
from dovideo.domain import VideoChunk
from dovideo.application.value_objects import VectorHit
from dovideo.infrastructure.r4_runtime import _StrictRetrievalService
from dovideo.infrastructure.vector.qdrant import QdrantVectorError


class Telemetry:
    def counter_value(self,name): return 0
    def increment(self,name,amount=1): pass
    def observe(self,name,value): pass


class Vectors:
    async def upsert(self,media_id,chunks): pass
    async def search(self,media_id,vector,*,limit,**scope):
        return (VectorHit(0,1000,.9),)


@pytest.mark.asyncio
async def test_single_chunk_product_media_still_requires_real_vector_verification():
    service=_StrictRetrievalService(object(),object(),Vectors(),telemetry=Telemetry())
    await service.index(17,(VideoChunk(start_ms=0,end_ms=1000,embedding=(1.,)),))


@pytest.mark.asyncio
async def test_long_media_retains_two_hit_verification_requirement():
    service=_StrictRetrievalService(object(),object(),Vectors(),telemetry=Telemetry())
    with pytest.raises(QdrantVectorError):
        await service.index(17,(VideoChunk(start_ms=0,end_ms=1000,embedding=(1.,)),VideoChunk(start_ms=1000,end_ms=2000,embedding=(1.,))))
