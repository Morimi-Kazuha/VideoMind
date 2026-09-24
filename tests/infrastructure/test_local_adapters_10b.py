from __future__ import annotations

import math

import pytest

from dovideo.application import ReadableSource, TranscriptSpan
from dovideo.infrastructure.media import LocalWhisperTranscriptionAdapter
from dovideo.infrastructure.providers import LocalTfidfEmbeddingAdapter


class FakeWhisperModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def transcribe(self, source: str, **options: object) -> dict[str, object]:
        self.calls.append((source, options))
        return {
            "segments": [
                {"start": 0.125, "end": 1.5, "text": " first span "},
                {"start": 2, "end": 3.25, "text": "second span"},
            ]
        }


@pytest.mark.asyncio
async def test_local_whisper_maps_realistic_timestamped_segments() -> None:
    model = FakeWhisperModel()
    adapter = LocalWhisperTranscriptionAdapter(model, language="en")

    spans = await adapter.transcribe(
        ReadableSource(uri="D:/media/audio.wav"), trace_id="trace-10b"
    )

    assert spans == (
        TranscriptSpan(125, 1500, "first span"),
        TranscriptSpan(2000, 3250, "second span"),
    )
    assert model.calls == [
        (
            "D:/media/audio.wav",
            {
                "fp16": False,
                "verbose": False,
                "temperature": 0,
                "condition_on_previous_text": False,
                "language": "en",
            },
        )
    ]


@pytest.mark.asyncio
async def test_local_tfidf_embedding_is_fitted_bounded_and_finite() -> None:
    adapter = LocalTfidfEmbeddingAdapter(max_features=3)
    with pytest.raises(RuntimeError):
        await adapter.embed("video")

    adapter.fit(("Video speech", "screen text", "video evidence"))
    vector = await adapter.embed("video evidence")

    assert adapter.dimension == 3
    assert len(vector) == 3
    assert all(math.isfinite(value) for value in vector)
    assert math.isclose(sum(value * value for value in vector), 1.0)
    assert await adapter.embed(" ") == ()
    assert await adapter.embed("unseen-token") == (0.0, 0.0, 0.0)
