from dovideo.application import (
    CHUNK_MS,
    CHUNK_MILLISECONDS,
    MAX_CONTEXT_CHARS,
    MAX_SUMMARY_FALLBACK_CHARS,
    LongVideoContextService,
    VideoChunkingService,
    VideoEvidenceRetrievalService,
    critique_query,
    java_string_length,
    near_segment,
)
from dovideo.infrastructure import (
    AsyncJsonHttpClient,
    JsonHttpResponse,
    QdrantError,
    QdrantInfrastructureError,
    QdrantVectorError,
    QdrantVectorIndex,
    QdrantVectorStore,
    StdlibAsyncJsonHttpClient,
    java_name_uuid_from_bytes,
    point_id,
)


def test_phase5_public_exports_are_available_from_package_boundaries() -> None:
    assert CHUNK_MS == CHUNK_MILLISECONDS == 300_000
    assert MAX_CONTEXT_CHARS == 24_000
    assert MAX_SUMMARY_FALLBACK_CHARS == 500
    assert VideoChunkingService.__name__ == "VideoChunkingService"
    assert VideoEvidenceRetrievalService.__name__ == "VideoEvidenceRetrievalService"
    assert LongVideoContextService.__name__ == "LongVideoContextService"
    assert callable(critique_query)
    assert callable(java_string_length)
    assert callable(near_segment)
    assert QdrantVectorStore is QdrantVectorIndex
    assert QdrantError is QdrantVectorError
    assert QdrantInfrastructureError is QdrantVectorError
    assert AsyncJsonHttpClient is not None
    assert JsonHttpResponse is not None
    assert StdlibAsyncJsonHttpClient is not None
    assert callable(java_name_uuid_from_bytes)
    assert callable(point_id)
