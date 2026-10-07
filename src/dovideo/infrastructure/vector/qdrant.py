"""Small asynchronous Qdrant vector-index adapter.

The adapter intentionally uses an injectable JSON HTTP boundary rather than a
Qdrant SDK.  The default client wraps the standard-library urllib call in
``asyncio.to_thread``; importing or constructing this module never performs a
network request.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json as jsonlib
import math
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dovideo.application.value_objects import VectorHit
from dovideo.application.errors import BudgetExceededError
from dovideo.domain import VideoChunk


DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_QDRANT_COLLECTION = "video_chunks"
DEFAULT_QDRANT_TIMEOUT_SECONDS = 10.0
COLLECTION_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")


class QdrantVectorError(RuntimeError):
    """A bounded, provider-neutral Qdrant infrastructure failure."""


QdrantInfrastructureError = QdrantVectorError
QdrantError = QdrantVectorError


@dataclass(frozen=True, slots=True)
class JsonHttpResponse:
    """Minimal response shape shared by the stdlib client and test fakes."""

    status_code: int
    body: Any = None


class AsyncJsonHttpClient(Protocol):
    """The only HTTP surface required by :class:`QdrantVectorIndex`."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Any,
        timeout: float,
    ) -> JsonHttpResponse:
        ...


class StdlibAsyncJsonHttpClient:
    """Async facade over ``urllib`` with a bounded request timeout."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Any,
        timeout: float,
    ) -> JsonHttpResponse:
        return await asyncio.to_thread(
            self._request_sync,
            method,
            url,
            headers,
            json,
            timeout,
        )

    @staticmethod
    def _request_sync(
        method: str,
        url: str,
        headers: Mapping[str, str],
        json: Any,
        timeout: float,
    ) -> JsonHttpResponse:
        payload = None
        request_headers = dict(headers)
        if json is not None:
            payload = jsonlib.dumps(json, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(
            url,
            data=payload,
            headers=request_headers,
            method=method.upper(),
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                status = int(response.status)
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            status = int(exc.code)
        body = _decode_body_bytes(raw)
        return JsonHttpResponse(status_code=status, body=body)


class QdrantVectorIndex:
    """Implement ``VectorIndexPort`` against Qdrant's JSON API."""

    def __init__(
        self,
        enabled: bool = True,
        base_url: str = DEFAULT_QDRANT_URL,
        api_key: str | None = None,
        collection: str = DEFAULT_QDRANT_COLLECTION,
        client: AsyncJsonHttpClient | None = None,
        *,
        http_client: AsyncJsonHttpClient | None = None,
        timeout_seconds: float = DEFAULT_QDRANT_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(collection, str) or COLLECTION_NAME_PATTERN.fullmatch(collection) is None:
            raise ValueError("Qdrant collection name is invalid")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("Qdrant base URL is required")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise TypeError("timeout_seconds must be a number")
        if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if api_key is not None and not isinstance(api_key, str):
            raise TypeError("api_key must be text or None")
        self.enabled = bool(enabled)
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key or "").strip()
        self.collection = collection
        self.timeout_seconds = float(timeout_seconds)
        self._client = client if client is not None else http_client
        if self._client is None:
            self._client = StdlibAsyncJsonHttpClient()
        self._collection_ready = False
        self._collection_dimension: int | None = None
        self._collection_lock = asyncio.Lock()

    @property
    def collection_ready(self) -> bool:
        return self._collection_ready

    @property
    def ready(self) -> bool:
        """Short compatibility spelling for health/status adapters."""

        return self._collection_ready

    async def upsert(
        self,
        media_id: int,
        chunks: tuple[VideoChunk, ...],
    ) -> None:
        """Upsert only chunks that have a non-empty embedding."""

        if not self.enabled:
            return
        vectorized = tuple(chunk for chunk in chunks if chunk.embedding)
        if not vectorized:
            return
        try:
            dimension = len(vectorized[0].embedding)
            await self._ensure_collection(dimension)
            for chunk in vectorized:
                if len(chunk.embedding) != dimension:
                    raise QdrantVectorError("Qdrant vector dimensions do not match")
            points = [
                {
                    "id": point_id(media_id, chunk),
                    "vector": list(chunk.embedding),
                    "payload": {
                        "mediaId": media_id,
                        "startMs": chunk.start_ms,
                        "endMs": chunk.end_ms,
                        **(
                            {"sourceRevision": chunk.source_revision}
                            if chunk.source_revision
                            else {}
                        ),
                        **(
                            {"chunkId": chunk.chunk_id}
                            if chunk.chunk_id
                            else {}
                        ),
                        **(
                            {"chunkingVersion": chunk.chunking_version}
                            if chunk.chunking_version else {}
                        ),
                    },
                }
                for chunk in vectorized
            ]
            response = await self._send(
                "PUT",
                f"/collections/{self.collection}/points?wait=true",
                {"points": points},
            )
            _require_success(response, "upsert")
        except (asyncio.CancelledError, BudgetExceededError):
            raise
        except Exception as exc:
            self._reset_ready()
            if isinstance(exc, QdrantVectorError):
                raise
            raise QdrantVectorError("Qdrant vector upsert failed") from exc

    async def search(
        self,
        media_id: int,
        query_embedding: tuple[float, ...],
        *,
        limit: int,
        source_revision: str | None = None,
        chunking_version: str | None = None,
    ) -> tuple[VectorHit, ...]:
        """Search one media ID and parse payload ranges into ``VectorHit``."""

        if not self.enabled or not query_embedding:
            return ()
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("Qdrant search limit must be an integer")
        if limit <= 0:
            return ()
        try:
            await self._ensure_collection(len(query_embedding))
            body = {
                "query": list(query_embedding),
                "filter": {
                    "must": [
                        {
                            "key": "mediaId",
                            "match": {"value": media_id},
                        }
                    ]
                },
                "limit": limit,
                "with_payload": True,
            }
            for key, value in (("sourceRevision", source_revision), ("chunkingVersion", chunking_version)):
                if value is not None:
                    body["filter"]["must"].append({"key": key, "match": {"value": value}})
            response = await self._send(
                "POST",
                f"/collections/{self.collection}/points/query",
                body,
            )
            _require_success(response, "search")
            body_value = response.body
            if not isinstance(body_value, Mapping):
                raise QdrantVectorError("Qdrant search response shape is invalid")
            result = body_value.get("result")
            if isinstance(result, Mapping):
                points = result.get("points")
            elif isinstance(result, list):
                # Accept the already-unwrapped shape used by a few local
                # fakes, while retaining Java's empty/missing-result behavior.
                points = result
            elif result is None:
                points = None
            else:
                raise QdrantVectorError("Qdrant search result shape is invalid")
            if points is None:
                return ()
            if not isinstance(points, list):
                raise QdrantVectorError("Qdrant search points shape is invalid")
            hits: list[VectorHit] = []
            for point in points:
                if not isinstance(point, dict):
                    continue
                payload = point.get("payload")
                if not isinstance(payload, dict):
                    continue
                try:
                    start_ms = _payload_int(payload, "startMs", "start_ms")
                    end_ms = _payload_int(payload, "endMs", "end_ms")
                    score = float(point.get("score", 0.0))
                except (TypeError, ValueError, OverflowError):
                    continue
                source_revision = _payload_text(
                    payload,
                    "sourceRevision",
                    "source_revision",
                )
                chunk_id = _payload_text(payload, "chunkId", "chunk_id")
                hits.append(
                    VectorHit(
                        start_ms=start_ms,
                        end_ms=end_ms,
                        score=score,
                        source_revision=source_revision,
                        chunk_id=chunk_id,
                        chunking_version=_payload_text(payload, "chunkingVersion", "chunking_version"),
                    )
                )
            return tuple(hits)
        except (asyncio.CancelledError, BudgetExceededError):
            raise
        except Exception as exc:
            self._reset_ready()
            if isinstance(exc, QdrantVectorError):
                raise
            raise QdrantVectorError("Qdrant vector search failed") from exc

    async def delete_media(self, media_id: int) -> None:
        """Best-effort media cleanup, matching Java's delete semantics."""

        if not self.enabled:
            return
        body = {
            "filter": {
                "must": [
                    {
                        "key": "mediaId",
                        "match": {"value": media_id},
                    }
                ]
            }
        }
        try:
            response = await self._send(
                "POST",
                f"/collections/{self.collection}/points/delete?wait=true",
                body,
            )
            _require_success(response, "delete")
        except (asyncio.CancelledError, BudgetExceededError):
            raise
        except Exception:
            # Java deliberately does not make cleanup failure fail the media
            # operation.  In particular, do not invalidate collection_ready.
            return

    async def _ensure_collection(self, dimension: int) -> None:
        if dimension <= 0:
            raise QdrantVectorError("Qdrant vector dimension must be positive")
        async with self._collection_lock:
            if self._collection_ready:
                self._check_dimension(dimension)
                return
            try:
                response = await self._send(
                    "GET",
                    f"/collections/{self.collection}",
                    None,
                )
                if 200 <= response.status_code < 300:
                    existing_dimension = _extract_dimension(response.body)
                    if existing_dimension is not None and existing_dimension != dimension:
                        raise QdrantVectorError(
                            "Qdrant collection vector dimension does not match"
                        )
                    self._collection_dimension = existing_dimension or dimension
                    self._collection_ready = True
                    return
                if response.status_code != 404:
                    raise _status_error("collection lookup", response.status_code)

                created = await self._send(
                    "PUT",
                    f"/collections/{self.collection}",
                    {"vectors": {"size": dimension, "distance": "Cosine"}},
                )
                _require_success(created, "collection creation")
                self._collection_dimension = dimension
                self._collection_ready = True
            except (asyncio.CancelledError, BudgetExceededError):
                raise
            except Exception:
                self._reset_ready()
                raise

    def _check_dimension(self, dimension: int) -> None:
        if (
            self._collection_dimension is not None
            and self._collection_dimension != dimension
        ):
            raise QdrantVectorError(
                "Qdrant collection vector dimension does not match"
            )

    async def _send(
        self,
        method: str,
        path: str,
        body: Any,
    ) -> JsonHttpResponse:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        try:
            request_method = self._client.request
            # ``json=`` follows the common httpx-style boundary.  Keep
            # compatibility with fakes written against an earlier local
            # spelling (``json_body``) without adding a provider dependency.
            try:
                parameter_names = inspect.signature(request_method).parameters
            except (TypeError, ValueError):
                parameter_names = {}
            body_keyword = (
                "json_body"
                if "json_body" in parameter_names and "json" not in parameter_names
                else "json"
            )
            raw_response = await request_method(
                method,
                self.base_url + path,
                headers=headers,
                **{body_keyword: body},
                timeout=self.timeout_seconds,
            )
        except (asyncio.CancelledError, BudgetExceededError):
            raise
        except Exception as exc:
            raise QdrantVectorError(
                f"Qdrant {method} request failed: {type(exc).__name__}"
            ) from exc
        response = await _coerce_response(raw_response)
        if not isinstance(response.status_code, int):
            raise QdrantVectorError("Qdrant response has an invalid status")
        return response

    def _reset_ready(self) -> None:
        self._collection_ready = False
        self._collection_dimension = None


def point_id(media_id: int | None, chunk: VideoChunk) -> str:
    """Return a deterministic point ID, namespaced for new source revisions."""

    media_text = "null" if media_id is None else str(media_id)
    if chunk.source_revision:
        source = (
            f"{media_text}:{chunk.source_revision}:{chunk.start_ms}:"
            f"{chunk.end_ms}:{chunk.chunk_id}"
        )
    else:
        # Preserve the frozen R0-R4 point identity for legacy chunks that do
        # not yet carry provenance metadata.
        source = f"{media_text}:{chunk.start_ms}:{chunk.end_ms}"
    return java_name_uuid_from_bytes(source.encode("utf-8"))


def java_name_uuid_from_bytes(value: bytes) -> str:
    """Reproduce Java's MD5/version-3 name UUID exactly."""

    digest = bytearray(hashlib.md5(value).digest())
    digest[6] &= 0x0F
    digest[6] |= 0x30
    digest[8] &= 0x3F
    digest[8] |= 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


async def _coerce_response(value: Any) -> JsonHttpResponse:
    missing = object()
    if isinstance(value, JsonHttpResponse):
        status = value.status_code
        body = value.body
    elif isinstance(value, Mapping):
        status = value.get("status_code", value.get("status"))
        body = value.get("body", missing)
        if body is missing:
            body = value.get("json", missing)
        if body is missing:
            body = value.get("payload", {})
    else:
        status = getattr(value, "status_code", getattr(value, "status", None))
        body = getattr(value, "body", missing)
        if body is missing or body is None:
            json_method = getattr(value, "json", None)
            if callable(json_method):
                try:
                    body = json_method()
                    if inspect.isawaitable(body):
                        body = await body
                except (asyncio.CancelledError, BudgetExceededError):
                    raise
                except Exception as exc:
                    raise QdrantVectorError(
                        "Qdrant response JSON is malformed"
                    ) from exc
            elif hasattr(value, "text"):
                body = getattr(value, "text")
            elif body is missing:
                body = {}
    if isinstance(status, bool) or not isinstance(status, int):
        raise QdrantVectorError("Qdrant response has an invalid status")
    if body is missing or body is None:
        body = {}
    if isinstance(body, bytes):
        body = _decode_body_bytes(body)
    elif isinstance(body, str):
        body = _decode_body_text(body)
    return JsonHttpResponse(status_code=status, body=body)


def _decode_body_bytes(raw: bytes) -> Any:
    if not raw:
        return {}
    return _decode_body_text(raw.decode("utf-8", errors="replace"))


def _decode_body_text(text: str) -> Any:
    if not text.strip():
        return {}
    try:
        return jsonlib.loads(text)
    except (TypeError, ValueError, jsonlib.JSONDecodeError) as exc:
        raise QdrantVectorError("Qdrant response JSON is malformed") from exc


def _status_error(operation: str, status_code: int) -> QdrantVectorError:
    # Keep response diagnostics bounded and secret-free: no response body or
    # request headers (which may contain the API key) is included.
    return QdrantVectorError(f"Qdrant {operation} returned HTTP {status_code}")


def _require_success(response: JsonHttpResponse, operation: str) -> None:
    if not 200 <= response.status_code < 300:
        raise _status_error(operation, response.status_code)


def _extract_dimension(body: Any) -> int | None:
    if not isinstance(body, dict):
        return None
    result = body.get("result")
    if not isinstance(result, dict):
        return None
    config = result.get("config")
    if not isinstance(config, dict):
        return None
    params = config.get("params")
    if not isinstance(params, dict):
        return None
    vectors = params.get("vectors")
    if isinstance(vectors, Mapping):
        value = vectors.get("size")
        if value is not None:
            return _dimension_value(value)
        # Qdrant may return named vectors as a mapping.  A single configured
        # vector is sufficient for this adapter's unnamed vector payload.
        if len(vectors) == 1:
            value = next(iter(vectors.values()))
            if isinstance(value, Mapping) and value.get("size") is not None:
                return _dimension_value(value["size"])
    return None


def _dimension_value(value: Any) -> int:
    if isinstance(value, bool):
        raise QdrantVectorError("Qdrant collection dimension is invalid")
    try:
        dimension = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise QdrantVectorError("Qdrant collection dimension is invalid") from exc
    if dimension <= 0:
        raise QdrantVectorError("Qdrant collection dimension is invalid")
    return dimension


def _payload_int(payload: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        if key in payload:
            value = payload[key]
            if isinstance(value, bool):
                raise TypeError("payload timestamp must be an integer")
            return int(value)
    return 0


def _payload_text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


# Java migration spelling; the application port remains ``VectorIndexPort``.
QdrantVectorStore = QdrantVectorIndex


__all__ = [
    "AsyncJsonHttpClient",
    "COLLECTION_NAME_PATTERN",
    "DEFAULT_QDRANT_COLLECTION",
    "DEFAULT_QDRANT_TIMEOUT_SECONDS",
    "DEFAULT_QDRANT_URL",
    "JsonHttpResponse",
    "QdrantError",
    "QdrantInfrastructureError",
    "QdrantVectorError",
    "QdrantVectorIndex",
    "QdrantVectorStore",
    "StdlibAsyncJsonHttpClient",
    "java_name_uuid_from_bytes",
    "point_id",
]
