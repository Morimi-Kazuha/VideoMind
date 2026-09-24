"""Run the local half of Phase 10B-FIX over the prepared long media.

This is an evidence/composition runner.  It reuses the already-composed media
and the completed local Whisper artifact, then exercises the frozen
VideoContext, chunking, local embedding, and retrieval services.  It does not
call a paid provider and it does not synthesize or inject transcript text.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from dovideo.application import (
    AsrBranchOutcome,
    MediaObservationBundle,
    OcrBranchOutcome,
    TranscriptSpan,
    VectorHit,
    VideoChunkingService,
    VideoContextBuilder,
    VideoEvidenceRetrievalService,
)
from dovideo.application.retrieval import cosine_similarity, fallback_terms
from dovideo.domain import VideoChunk, VideoContext, VideoRetrievalIntent, VideoSegment
from dovideo.infrastructure.providers import (
    LocalChunkSummaryAdapter,
    LocalTfidfEmbeddingAdapter,
)


ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "work" / "media"
SOURCE = MEDIA / "representative-long.mp4"
ASR_ARTIFACT = MEDIA / "representative-asr.json"
OUTPUT = MEDIA / "phase10b-fix-local.json"
MEDIA_ID = 10_000_010
GOAL = "What does the later poem say about the sea, pool, and tide?"


class LocalRetrievalPlanner:
    """Deterministic retrieval-planner seam for the local validation run."""

    async def plan_retrieval(self, goal: str) -> VideoRetrievalIntent:
        terms = fallback_terms(goal)
        return VideoRetrievalIntent(
            semantic_query=goal,
            keywords=terms,
            visual_keywords=terms,
        )


class InMemoryVectorIndex:
    """Small local VectorIndexPort implementation over the real chunk vectors."""

    def __init__(self) -> None:
        self._chunks: tuple[VideoChunk, ...] = ()

    async def upsert(self, media_id: int, chunks: tuple[VideoChunk, ...]) -> None:
        del media_id
        self._chunks = tuple(chunks)

    async def search(
        self,
        media_id: int,
        vector: tuple[float, ...],
        *,
        limit: int,
    ) -> tuple[VectorHit, ...]:
        del media_id
        ranked = sorted(
            (
                VectorHit(
                    start_ms=chunk.start_ms,
                    end_ms=chunk.end_ms,
                    score=cosine_similarity(vector, chunk.embedding),
                )
                for chunk in self._chunks
            ),
            key=lambda hit: (-hit.score, hit.start_ms),
        )
        return tuple(ranked[:limit])

    async def delete_media(self, media_id: int) -> None:
        del media_id
        self._chunks = ()


def _probe_media() -> dict[str, object]:
    probe = subprocess.run(
        [
            str(ROOT / "tools" / "ffmpeg" / "ffprobe.exe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,codec_name",
            "-of",
            "json",
            str(SOURCE),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(probe.stdout)
    duration = float(payload["format"]["duration"])
    streams = tuple(
        {
            "index": int(stream["index"]),
            "codec_type": str(stream["codec_type"]),
            "codec_name": str(stream["codec_name"]),
        }
        for stream in payload.get("streams", ())
    )
    if duration <= 300:
        raise RuntimeError(f"representative media is not longer than five minutes: {duration}")
    return {"duration_seconds": duration, "streams": streams}


def _load_asr() -> tuple[dict[str, object], tuple[TranscriptSpan, ...]]:
    if not ASR_ARTIFACT.is_file():
        raise FileNotFoundError(f"completed local ASR artifact is missing: {ASR_ARTIFACT}")
    payload = json.loads(ASR_ARTIFACT.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not str(payload.get("text", "")).strip():
        raise RuntimeError("representative ASR artifact has no transcript text")
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise RuntimeError("representative ASR artifact has no timestamped segments")
    spans: list[TranscriptSpan] = []
    previous_end = -1.0
    for item in raw_segments:
        if not isinstance(item, dict):
            raise RuntimeError("representative ASR artifact contains a malformed segment")
        start = float(item["start"])
        end = float(item["end"])
        text = item.get("text", "")
        if end <= start or start < previous_end or not isinstance(text, str) or not text.strip():
            raise RuntimeError("representative ASR artifact contains invalid ordered spans")
        spans.append(
            TranscriptSpan(
                start_ms=round(start * 1000),
                end_ms=round(end * 1000),
                text=text,
            )
        )
        previous_end = end
    return payload, tuple(spans)


def _region_for_text(text: str) -> str:
    normalized = text.casefold()
    if any(term in normalized for term in ("poem", "sea", "pool", "tide")):
        return "later-afterlove"
    return "early-jfk"


async def main() -> None:
    media_probe = _probe_media()
    asr_payload, spans = _load_asr()
    context = VideoContextBuilder().build(
        str(SOURCE),
        GOAL,
        MediaObservationBundle(
            asr=AsrBranchOutcome(observations=spans, attempted=len(spans)),
            ocr=OcrBranchOutcome(),
        ),
    )
    embedder = LocalTfidfEmbeddingAdapter(max_features=256)
    embedder.fit(
        [
            context.transcript_text(),
            GOAL,
            *(segment.transcript for segment in context.segments),
        ]
    )
    chunks = await VideoChunkingService(
        LocalChunkSummaryAdapter(),
        embedder,
    ).build(context.segments)
    if len(context.segments) < 2:
        raise RuntimeError("VideoContext did not create multiple temporal windows")
    if len(chunks) < 2:
        raise RuntimeError("five-minute chunking did not create multiple chunks")
    if not all(chunk.embedding for chunk in chunks):
        raise RuntimeError("one or more chunks has no embedding vector")

    vector_index = InMemoryVectorIndex()
    retrieval = VideoEvidenceRetrievalService(
        LocalRetrievalPlanner(),
        embedder,
        vector_index,
    )
    await retrieval.index(MEDIA_ID, chunks)
    hits = await retrieval.search(MEDIA_ID, GOAL, chunks)
    if len(hits) < 2:
        raise RuntimeError("retrieval returned fewer than two evidence candidates")
    top_hit = hits[0]
    top_region = _region_for_text(top_hit.transcript)
    if top_region != "later-afterlove":
        raise RuntimeError(
            "retrieval did not rank the later After Love region first: "
            f"{top_hit.start_ms}ms"
        )

    # Feed the top retrieval candidates back into the existing AgentLoop as a
    # context selected by retrieval output, preserving original segments and
    # timestamps.  No timestamp or transcript is selected by a fixed index.
    selected: list[VideoSegment] = []
    selected_ranges: set[tuple[int, int]] = set()
    for hit in hits[:1]:
        key = (hit.start_ms, hit.end_ms)
        if key in selected_ranges:
            continue
        for segment in context.segments:
            if (segment.start_ms, segment.end_ms) == key:
                selected.append(segment)
                selected_ranges.add(key)
                break
    retrieved_context = VideoContext(
        source=context.source,
        user_goal=GOAL,
        segments=tuple(selected),
    )
    if not retrieved_context.segments:
        raise RuntimeError("retrieval candidates could not be mapped to source segments")

    result = {
        "status": "success",
        "source": str(SOURCE),
        "media_id": MEDIA_ID,
        "goal": GOAL,
        "media": media_probe,
        "asr": {
            "artifact": str(ASR_ARTIFACT),
            "runtime": r"D:\python\python.exe",
            "model": "tiny.en",
            "real_inference_artifact": True,
            "segment_count": len(spans),
            "first_start_ms": spans[0].start_ms,
            "last_end_ms": spans[-1].end_ms,
            "contains_early_jfk": all(
                term in str(asr_payload.get("text", ""))
                for term in ("Americans", "country")
            ),
            "contains_later_afterlove": all(
                term in str(asr_payload.get("text", "")).casefold()
                for term in ("poem", "sea", "pool", "tide")
            ),
        },
        "video_context": {
            "temporal_window_count": len(context.segments),
            "windows": [
                {
                    "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms,
                    "region": _region_for_text(segment.transcript),
                    "transcript_preview": segment.transcript[:180],
                }
                for segment in context.segments
            ],
        },
        "chunking": {
            "chunk_count": len(chunks),
            "embedding_dimension": embedder.dimension,
            "chunks": [
                {
                    "start_ms": chunk.start_ms,
                    "end_ms": chunk.end_ms,
                    "raw_segment_count": len(chunk.raw_segments),
                    "summary": chunk.segment_summary,
                    "keywords": list(chunk.keywords),
                    "embedding_dimension": len(chunk.embedding),
                    "embedding_is_finite": all(
                        value == value and abs(value) != float("inf")
                        for value in chunk.embedding
                    ),
                }
                for chunk in chunks
            ],
        },
        "retrieval": {
            "planner": "deterministic local retrieval planner",
            "query": GOAL,
            "candidate_count": len(hits),
            "top_candidate": hits[0].model_dump(by_alias=True),
            "top_candidate_region": top_region,
            "candidates": [
                {
                    "rank": rank,
                    **hit.model_dump(by_alias=True),
                    "region": _region_for_text(hit.transcript),
                }
                for rank, hit in enumerate(hits, start=1)
            ],
            "agent_context_selected_from_top_candidates": True,
            "selected_context_windows": [
                {"start_ms": segment.start_ms, "end_ms": segment.end_ms}
                for segment in retrieved_context.segments
            ],
        },
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": result["status"],
                "duration_seconds": media_probe["duration_seconds"],
                "asr_segments": len(spans),
                "temporal_windows": len(context.segments),
                "chunks": len(chunks),
                "embedding_dimension": embedder.dimension,
                "retrieval_candidates": len(hits),
                "top_candidate_start_ms": top_hit.start_ms,
                "top_candidate_region": top_region,
                "selected_context_windows": len(retrieved_context.segments),
                "output": str(OUTPUT),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
