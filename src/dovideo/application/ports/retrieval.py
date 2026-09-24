"""Vector index boundary."""

from __future__ import annotations

from typing import Protocol

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
    ) -> tuple[VectorHit, ...]:
        ...

    async def delete_media(self, media_id: int) -> None:
        ...


__all__ = ["VectorIndexPort"]
