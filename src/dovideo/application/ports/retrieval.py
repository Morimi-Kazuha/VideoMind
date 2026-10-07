"""Vector index boundary."""

from __future__ import annotations

from typing import Protocol
from dataclasses import dataclass

from dovideo.domain import VideoChunk

from ..value_objects import VectorHit


class VectorIndexPort(Protocol):
    """Upsert/search/delete vectors without exposing a vector SDK."""

    async def upsert(self, media_id: int, chunks: tuple[VideoChunk, ...]) -> None:
        ...

    async def search(
        self,
        media_id: int,
        query_embedding: tuple[float, ...],
        *,
        limit: int,
        source_revision: str | None = None,
        chunking_version: str | None = None,
    ) -> tuple[VectorHit, ...]:
        ...

    async def delete_media(self, media_id: int) -> None:
        ...


@dataclass(frozen=True, slots=True)
class RerankerDocument:
    candidate_id: str
    text: str


@dataclass(frozen=True, slots=True)
class RerankerResult:
    candidate_id: str
    score: float


class RerankerPort(Protocol):
    async def rerank(self, query: str, documents: tuple[RerankerDocument, ...]) -> tuple[RerankerResult, ...]:
        """Score every supplied candidate; no domain DTO or provider HTTP shape."""
        ...


__all__ = ["VectorIndexPort", "RerankerPort", "RerankerDocument", "RerankerResult"]
