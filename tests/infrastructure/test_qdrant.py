from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest

from dovideo.domain import VideoChunk
from dovideo.application.value_objects import VectorHit
from dovideo.infrastructure import (
    JsonHttpResponse,
    QdrantVectorError,
    QdrantVectorIndex,
    java_name_uuid_from_bytes,
    point_id,
)


ResponseFactory = Callable[[], object | Awaitable[object]]


class FakeJsonClient:
    def __init__(self, responses: Sequence[object | ResponseFactory] = ()) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: Any,
        timeout: float,
    ) -> object:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json": json,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("fake HTTP response queue is empty")
        response = self.responses.pop(0)
        if callable(response):
            response = response()
            if hasattr(response, "__await__"):
                response = await response  # type: ignore[assignment]
        if isinstance(response, BaseException):
            raise response
        return response


def _chunk(
    start_ms: int = 0,
    *,
    embedding: tuple[float, ...] = (),
    end_ms: int | None = None,
) -> VideoChunk:
    return VideoChunk(
        start_ms=start_ms,
        end_ms=end_ms if end_ms is not None else start_ms + 300_000,
        embedding=embedding,
    )


def _existing_collection(dimension: int = 2) -> JsonHttpResponse:
    return JsonHttpResponse(
        200,
        {
            "result": {
                "config": {
                    "params": {"vectors": {"size": dimension, "distance": "Cosine"}}
                }
            }
        },
    )


@pytest.mark.asyncio
async def test_disabled_and_empty_vector_operations_make_no_requests() -> None:
    client = FakeJsonClient()
    index = QdrantVectorIndex(enabled=False, client=client)

    await index.upsert(7, (_chunk(embedding=(1.0, 2.0)),))
    await index.upsert(7, (_chunk(),))
    assert await index.search(7, (1.0, 2.0), limit=3) == ()
    await index.delete_media(7)

    enabled_client = FakeJsonClient()
    enabled = QdrantVectorIndex(client=enabled_client)
    await enabled.upsert(7, (_chunk(),))
    assert await enabled.search(7, (), limit=3) == ()
    assert client.calls == []
    assert enabled_client.calls == []


@pytest.mark.asyncio
async def test_upsert_existing_collection_filters_vectors_and_emits_java_point_id() -> None:
    client = FakeJsonClient([_existing_collection(), JsonHttpResponse(200, {})])
    index = QdrantVectorIndex(
        base_url="http://qdrant.invalid///",
        api_key="  secret-token  ",
        collection="video_chunks",
        client=client,
        timeout_seconds=3.5,
    )
    chunks = (
        _chunk(0, embedding=(1.0, 0.0)),
        _chunk(300_000),
        _chunk(600_000, embedding=(0.25, 0.75)),
    )

    await index.upsert(42, chunks)

    assert index.collection_ready is True
    assert [call["method"] for call in client.calls] == ["GET", "PUT"]
    assert client.calls[0]["url"] == "http://qdrant.invalid/collections/video_chunks"
    point_call = client.calls[1]
    assert point_call["url"] == (
        "http://qdrant.invalid/collections/video_chunks/points?wait=true"
    )
    assert point_call["headers"] == {
        "Content-Type": "application/json",
        "api-key": "secret-token",
    }
    assert point_call["timeout"] == 3.5
    points = point_call["json"]["points"]
    assert len(points) == 2
    assert points[0] == {
        "id": "a9cb7c14-b7c6-3e00-8011-6e2725be29e8",
        "vector": [1.0, 0.0],
        "payload": {"mediaId": 42, "startMs": 0, "endMs": 300_000},
    }
    assert points[1]["payload"] == {"mediaId": 42, "startMs": 600_000, "endMs": 900_000}


@pytest.mark.asyncio
async def test_upsert_creates_missing_collection_with_first_vector_dimension() -> None:
    client = FakeJsonClient(
        [
            JsonHttpResponse(404, {"status": "error"}),
            JsonHttpResponse(200, {}),
            JsonHttpResponse(200, {}),
        ]
    )
    index = QdrantVectorIndex(client=client)

    await index.upsert(1, (_chunk(embedding=(1.0, 2.0, 3.0)),))

    assert client.calls[1]["method"] == "PUT"
    assert client.calls[1]["url"] == "http://localhost:6333/collections/video_chunks"
    assert client.calls[1]["json"] == {
        "vectors": {"size": 3, "distance": "Cosine"}
    }
    assert client.calls[2]["url"].endswith("/points?wait=true")


@pytest.mark.asyncio
async def test_search_builds_media_filter_and_preserves_valid_hit_order() -> None:
    client = FakeJsonClient(
        [
            _existing_collection(3),
            JsonHttpResponse(
                200,
                {
                    "result": {
                        "points": [
                            {"payload": {"startMs": 600_000, "endMs": 900_000}, "score": 0.8},
                            {"id": "missing-payload", "score": 0.7},
                            {"payload": {"startMs": 0, "endMs": 300_000}, "score": 0.9},
                            "not-a-point",
                        ]
                    }
                },
            ),
        ]
    )
    index = QdrantVectorIndex(api_key="key", client=client)

    hits = await index.search(99, (0.0, 1.0, 0.0), limit=6)

    assert hits == (
        VectorHit(start_ms=600_000, end_ms=900_000, score=0.8),
        VectorHit(start_ms=0, end_ms=300_000, score=0.9),
    )
    search_call = client.calls[1]
    assert search_call["method"] == "POST"
    assert search_call["url"].endswith("/collections/video_chunks/points/query")
    assert search_call["headers"]["api-key"] == "key"
    assert search_call["json"] == {
        "query": [0.0, 1.0, 0.0],
        "filter": {
            "must": [
                {"key": "mediaId", "match": {"value": 99}},
            ]
        },
        "limit": 6,
        "with_payload": True,
    }


def test_java_name_uuid_helper_and_point_id_are_stable() -> None:
    chunk = _chunk(0, embedding=(1.0,))

    assert java_name_uuid_from_bytes(b"42:0:300000") == (
        "a9cb7c14-b7c6-3e00-8011-6e2725be29e8"
    )
    assert point_id(42, chunk) == "a9cb7c14-b7c6-3e00-8011-6e2725be29e8"
    assert point_id(None, chunk) != point_id(42, chunk)


@pytest.mark.asyncio
async def test_upsert_status_failure_resets_ready_and_can_retry() -> None:
    client = FakeJsonClient(
        [_existing_collection(), JsonHttpResponse(500, {"secret": "must not leak"})]
    )
    index = QdrantVectorIndex(client=client)
    chunk = _chunk(embedding=(1.0, 0.0))

    with pytest.raises(QdrantVectorError) as caught:
        await index.upsert(1, (chunk,))
    assert "500" in str(caught.value)
    assert "must not leak" not in str(caught.value)
    assert index.collection_ready is False

    client.responses.extend([_existing_collection(), JsonHttpResponse(200, {})])
    await index.upsert(1, (chunk,))
    assert index.collection_ready is True
    assert [call["method"] for call in client.calls] == ["GET", "PUT", "GET", "PUT"]


@pytest.mark.asyncio
async def test_transport_and_malformed_json_fail_as_typed_errors_and_reset_ready() -> None:
    transport_client = FakeJsonClient([OSError("connection refused")])
    transport_index = QdrantVectorIndex(client=transport_client)
    with pytest.raises(QdrantVectorError):
        await transport_index.upsert(1, (_chunk(embedding=(1.0,)),))
    assert transport_index.collection_ready is False

    malformed_client = FakeJsonClient(
        [JsonHttpResponse(200, "{not valid json")]
    )
    malformed_index = QdrantVectorIndex(client=malformed_client)
    with pytest.raises(QdrantVectorError):
        await malformed_index.upsert(1, (_chunk(embedding=(1.0,)),))
    assert malformed_index.collection_ready is False


@pytest.mark.asyncio
async def test_search_missing_result_is_empty_but_non_mapping_shape_is_error() -> None:
    empty_client = FakeJsonClient(
        [_existing_collection(1), JsonHttpResponse(200, {})]
    )
    empty_index = QdrantVectorIndex(client=empty_client)
    assert await empty_index.search(1, (1.0,), limit=1) == ()

    malformed_client = FakeJsonClient(
        [_existing_collection(1), JsonHttpResponse(200, [])]
    )
    malformed_index = QdrantVectorIndex(client=malformed_client)
    with pytest.raises(QdrantVectorError):
        await malformed_index.search(1, (1.0,), limit=1)
    assert malformed_index.collection_ready is False


@pytest.mark.asyncio
async def test_delete_is_best_effort_but_cancellation_propagates_and_ready_survives() -> None:
    client = FakeJsonClient([_existing_collection(1), JsonHttpResponse(200, {})])
    index = QdrantVectorIndex(client=client)
    await index.upsert(1, (_chunk(embedding=(1.0,)),))
    assert index.collection_ready

    client.responses.extend([JsonHttpResponse(503, {}), OSError("gone")])
    await index.delete_media(1)
    await index.delete_media(1)
    assert index.collection_ready is True

    client.responses.append(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await index.delete_media(1)
    assert index.collection_ready is True


@pytest.mark.parametrize(
    "collection",
    ("", "has.dot", "has/slash", "a" * 129, "中文"),
)
def test_collection_name_must_match_qdrant_safe_pattern(collection: str) -> None:
    with pytest.raises(ValueError):
        QdrantVectorIndex(collection=collection)


@pytest.mark.asyncio
async def test_dimension_mismatch_is_typed_and_invalidates_collection_ready() -> None:
    client = FakeJsonClient([_existing_collection(2)])
    index = QdrantVectorIndex(client=client)

    with pytest.raises(QdrantVectorError):
        await index.upsert(1, (_chunk(embedding=(1.0, 2.0, 3.0)),))
    assert index.collection_ready is False
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_r4_search_rejects_stale_collection_dimension_before_query() -> None:
    client = FakeJsonClient([_existing_collection(3)])
    index = QdrantVectorIndex(
        collection="video_chunks_r4_bge_m3",
        client=client,
    )

    with pytest.raises(QdrantVectorError, match="dimension does not match"):
        await index.search(11, tuple(0.0 for _ in range(1024)), limit=2)

    assert index.collection_ready is False
    assert len(client.calls) == 1
    assert client.calls[0]["method"] == "GET"


@pytest.mark.asyncio
async def test_concurrent_initialization_performs_collection_lookup_once() -> None:
    async def delayed_collection() -> JsonHttpResponse:
        await asyncio.sleep(0.01)
        return _existing_collection(2)

    client = FakeJsonClient([delayed_collection, JsonHttpResponse(200, {}), JsonHttpResponse(200, {})])
    index = QdrantVectorIndex(client=client)
    chunk = _chunk(embedding=(1.0, 0.0))

    await asyncio.gather(index.upsert(1, (chunk,)), index.upsert(1, (chunk,)))

    assert sum(call["method"] == "GET" for call in client.calls) == 1
    assert sum(call["url"].endswith("/points?wait=true") for call in client.calls) == 2


@pytest.mark.asyncio
async def test_current_scope_filters_stale_before_candidate_limit():
    from dovideo.domain import CHUNKING_CONTRACT_VERSION
    class ScopedClient(FakeJsonClient):
        async def request(self, method, url, **kwargs):
            if method == "GET":
                return _existing_collection(2)
            self.calls.append({"json": kwargs["json"]})
            points = [
                {"payload": {"mediaId": 9, "startMs": 0, "endMs": 300_000,
                             "sourceRevision": "a" * 64, "chunkingVersion": "old", "chunkId": "stale"}, "score": 1.0},
                {"payload": {"mediaId": 9, "startMs": 240_000, "endMs": 540_000,
                             "sourceRevision": "a" * 64, "chunkingVersion": CHUNKING_CONTRACT_VERSION, "chunkId": "current"}, "score": .2},
            ]
            conditions = kwargs["json"]["filter"]["must"]
            eligible = [p for p in points if all(p["payload"].get(c["key"]) == c["match"]["value"] for c in conditions)]
            return JsonHttpResponse(200, {"result": {"points": eligible[:kwargs["json"]["limit"]]}})
    client = ScopedClient()
    index = QdrantVectorIndex(client=client)
    hits = await index.search(9, (1.0, 0.0), limit=1, source_revision="a" * 64, chunking_version=CHUNKING_CONTRACT_VERSION)
    assert hits[0].chunk_id == "current"
    assert hits[0].chunking_version == CHUNKING_CONTRACT_VERSION
    assert client.calls[0]["json"]["filter"]["must"] == [
        {"key": "mediaId", "match": {"value": 9}},
        {"key": "sourceRevision", "match": {"value": "a" * 64}},
        {"key": "chunkingVersion", "match": {"value": CHUNKING_CONTRACT_VERSION}},
    ]
    assert (await index.search(9, (1.0, 0.0), limit=1))[0].chunk_id == "stale"


@pytest.mark.asyncio
async def test_budget_error_is_not_wrapped_as_vector_failure():
    from dovideo.application.errors import BudgetExceededError
    for operation in ("upsert", "search", "delete_media"):
        index = QdrantVectorIndex(client=FakeJsonClient([BudgetExceededError("budget")]))
        with pytest.raises(BudgetExceededError):
            if operation == "upsert":
                await index.upsert(1, (_chunk(embedding=(1.0, 0.0)),))
            elif operation == "search":
                await index.search(1, (1.0, 0.0), limit=1)
            else:
                await index.delete_media(1)
