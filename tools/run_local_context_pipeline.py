"""Compose the existing application services over the real smoke artifacts.

This is an evidence script, not a production entry point.  It reads the
Whisper/Tesseract outputs generated from ``jfk-smoke.mp4`` and exercises the
frozen VideoContext, chunking, local embedding, and retrieval seams.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from dovideo.application import (
    AsrBranchOutcome,
    MediaObservationBundle,
    OcrBranchOutcome,
    OcrObservation,
    TranscriptSpan,
    VideoContextBuilder,
    VideoChunkingService,
    VideoEvidenceRetrievalService,
)
from dovideo.domain import VideoRetrievalIntent
from dovideo.infrastructure.providers import (
    LocalChunkSummaryAdapter,
    LocalTfidfEmbeddingAdapter,
)


ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "work" / "media"


class LocalRetrievalPlanner:
    async def plan_retrieval(self, goal: str) -> VideoRetrievalIntent:
        terms = tuple(value for value in goal.split() if len(value) > 1)
        return VideoRetrievalIntent(
            semantic_query=goal,
            keywords=terms,
            visual_keywords=terms,
        )


class EmptyVectorIndex:
    async def upsert(self, media_id: int | None, chunks: tuple[object, ...]) -> None:
        del media_id, chunks

    async def search(
        self,
        media_id: int | None,
        vector: tuple[float, ...],
        *,
        limit: int,
    ) -> tuple[object, ...]:
        del media_id, vector, limit
        return ()


async def main() -> None:
    asr_payload = json.loads((MEDIA / "jfk-asr-tiny-en.json").read_text(encoding="utf-8"))
    ocr_payload = json.loads((MEDIA / "jfk-ocr.json").read_text(encoding="utf-8"))
    spans = tuple(
        TranscriptSpan(
            start_ms=round(float(item["start"]) * 1000),
            end_ms=round(float(item["end"]) * 1000),
            text=item["text"],
        )
        for item in asr_payload["segments"]
    )
    ocr = tuple(
        OcrObservation(
            timestamp_ms=index * 3000,
            text=item["Text"],
            frame_ref=f"jfk-frames/{item['Frame']}",
        )
        for index, item in enumerate(ocr_payload)
    )
    bundle = MediaObservationBundle(
        asr=AsrBranchOutcome(observations=spans, attempted=len(spans)),
        ocr=OcrBranchOutcome(observations=ocr, attempted=len(ocr)),
    )
    goal = "What did President Kennedy ask Americans to do?"
    context = VideoContextBuilder().build("jfk-smoke.mp4", goal, bundle)

    embedder = LocalTfidfEmbeddingAdapter(max_features=256)
    embedder.fit(
        [
            context.transcript_text(),
            *(value for segment in context.segments for value in segment.ocr_texts),
            goal,
        ]
    )
    chunks = await VideoChunkingService(LocalChunkSummaryAdapter(), embedder).build(
        context.segments
    )
    retrieval = VideoEvidenceRetrievalService(
        LocalRetrievalPlanner(), embedder, EmptyVectorIndex()
    )
    hits = await retrieval.search(1, goal, chunks)
    print(
        json.dumps(
            {
                "source": str(MEDIA / "jfk-smoke.mp4"),
                "goal": goal,
                "context_segments": len(context.segments),
                "transcript_spans": len(spans),
                "ocr_observations": len(ocr),
                "chunks": len(chunks),
                "embedding_dimension": embedder.dimension,
                "chunk_vectors": [len(chunk.embedding) for chunk in chunks],
                "retrieval_hits": [hit.model_dump(by_alias=True) for hit in hits],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
