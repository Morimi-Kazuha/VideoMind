"""Run the one bounded Phase 11 live embedding verification.

This script is intentionally separate from pytest and from the DeepSeek
AgentLoop run.  It first performs one harmless adapter smoke request, then
reuses the completed Phase 10B ASR/context/chunk path for remote vectors.  No
credential, prompt, or provider response body is written to the evidence
artifact.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

from dovideo.application import (
    AsrBranchOutcome,
    MediaObservationBundle,
    OcrBranchOutcome,
    VideoChunkingService,
    VideoContextBuilder,
    VideoEvidenceRetrievalService,
)
from dovideo.domain import VideoContext
from dovideo.infrastructure.providers import (
    EmbeddingResponseError,
    OpenAICompatibleEmbeddingAdapter,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderError,
    ProviderRequestError,
    ProviderTransportError,
    ProviderTransientError,
    LocalChunkSummaryAdapter,
)
from dovideo.presentation import (
    InMemoryVectorIndex,
    LocalRetrievalPlanner,
    embedding_provider_config_from_environment,
    load_timestamped_asr_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "work" / "media"
SOURCE = MEDIA / "representative-long.mp4"
ASR_ARTIFACT = MEDIA / "representative-asr.json"
LOCAL_ARTIFACT = MEDIA / "phase10b-fix-local.json"
OUTPUT = MEDIA / "phase11-embedding-live.json"
MEDIA_ID = 10_000_010
SMOKE_INPUT = "long video evidence retrieval"


class CountingEmbedding:
    """Count calls without retaining text, vectors, headers, or responses."""

    def __init__(self, inner: OpenAICompatibleEmbeddingAdapter) -> None:
        self._inner = inner
        self.request_count = 0

    async def embed(self, text: str) -> tuple[float, ...]:
        self.request_count += 1
        return await self._inner.embed(text)


def _load_context() -> tuple[VideoContext, dict[str, Any]]:
    local = json.loads(LOCAL_ARTIFACT.read_text(encoding="utf-8"))
    if not isinstance(local, dict) or local.get("status") != "success":
        raise RuntimeError("completed Phase 10B local artifact is unavailable")
    if int(local.get("chunking", {}).get("chunk_count", 0)) < 2:
        raise RuntimeError("Phase 10B local artifact does not contain two chunks")
    asr_payload, spans = load_timestamped_asr_artifact(ASR_ARTIFACT)
    context = VideoContextBuilder().build(
        str(SOURCE),
        str(local["goal"]),
        MediaObservationBundle(
            asr=AsrBranchOutcome(observations=spans, attempted=len(spans)),
            ocr=OcrBranchOutcome(),
        ),
    )
    if not asr_payload.get("segments"):
        raise RuntimeError("ASR artifact contains no source spans")
    return context, local


def _region_for_text(text: str) -> str:
    normalized = text.casefold()
    if any(term in normalized for term in ("poem", "sea", "pool", "tide")):
        return "later-afterlove"
    return "other"


def _validate_vector(vector: tuple[float, ...], *, name: str) -> int:
    if not vector or not all(math.isfinite(value) for value in vector):
        raise RuntimeError(f"{name} vector is empty or non-finite")
    return len(vector)


async def _run() -> dict[str, Any]:
    config = embedding_provider_config_from_environment(required=True)
    adapter = CountingEmbedding(OpenAICompatibleEmbeddingAdapter(config))

    smoke = await adapter.embed(SMOKE_INPUT)
    dimension = _validate_vector(smoke, name="embedding smoke")

    context, local = _load_context()
    chunks = await VideoChunkingService(
        LocalChunkSummaryAdapter(),
        adapter,
    ).build(context.segments)
    if len(chunks) < 2:
        raise RuntimeError("remote embedding path produced fewer than two chunks")
    dimensions = {_validate_vector(chunk.embedding, name="chunk") for chunk in chunks}
    if dimensions != {dimension}:
        raise RuntimeError("remote embedding dimensions are not stable")

    index = InMemoryVectorIndex()
    retrieval = VideoEvidenceRetrievalService(
        LocalRetrievalPlanner(),
        adapter,
        index,
    )
    await retrieval.index(MEDIA_ID, chunks)
    hits = await retrieval.search(MEDIA_ID, context.user_goal, chunks)
    if len(hits) < 2:
        raise RuntimeError("remote retrieval returned fewer than two candidates")
    top_region = _region_for_text(hits[0].transcript)
    if top_region != "later-afterlove":
        raise RuntimeError("remote retrieval did not select the expected semantic region")
    expected_requests = 1 + len(chunks) + 1
    if adapter.request_count != expected_requests:
        raise RuntimeError(
            "unexpected remote embedding request count; local substitution may have occurred"
        )
    return {
        "status": "LIVE VERIFIED",
        "provider": "OpenAI-compatible embeddings",
        "model": config.embedding_model or config.model,
        "dimension": dimension,
        "success": True,
        "request_count": adapter.request_count,
        "vector_count": 1 + len(chunks) + 1,
        "source": str(SOURCE),
        "goal": context.user_goal,
        "reused_phase10b_artifact": str(LOCAL_ARTIFACT),
        "chunk_count": len(chunks),
        "candidate_count": len(hits),
        "top_candidate_range": {
            "start_ms": hits[0].start_ms,
            "end_ms": hits[0].end_ms,
        },
        "top_candidate_region": top_region,
        "local_artifact_chunk_count": int(local["chunking"]["chunk_count"]),
    }


def _classification(error: BaseException) -> str:
    if isinstance(error, ProviderConfigurationError):
        return "CONFIGURATION BLOCKED"
    if isinstance(
        error,
        (
            ProviderAuthenticationError,
            ProviderRequestError,
            ProviderTransientError,
            ProviderTransportError,
        ),
    ):
        return "LIVE PROVIDER BLOCKED"
    if isinstance(error, (EmbeddingResponseError, ProviderError)):
        return "ADAPTER DEFECT"
    return "ADAPTER DEFECT"


async def main() -> int:
    try:
        result = await _run()
    except Exception as error:
        result = {
            "status": _classification(error),
            "success": False,
            "provider": "OpenAI-compatible embeddings",
        }
        print(json.dumps(result, ensure_ascii=False))
        return 2
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({**result, "output": str(OUTPUT)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
