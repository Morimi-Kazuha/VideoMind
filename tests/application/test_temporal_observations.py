from __future__ import annotations

import pytest

from dovideo.application import AgentCheckpointService, VideoContextBuilder
from dovideo.application.temporal_read import temporal_observation_page
from dovideo.application.value_objects import (
    AsrBranchOutcome,
    MediaObservationBundle,
    OcrBranchOutcome,
    OcrObservation,
    TranscriptSpan,
)
from dovideo.domain.video import TemporalObservation, VideoContext
from dovideo.infrastructure.persistence import (
    CheckpointRepository,
    InMemoryHotCheckpointCache,
    SqliteCheckpointStore,
)


def _context(asr=(), ocr=(), *, identity="sha256:one") -> VideoContext:
    return VideoContextBuilder().build(
        "memory://source",
        "goal",
        MediaObservationBundle(
            asr=AsrBranchOutcome(observations=asr, attempted=len(asr)),
            ocr=OcrBranchOutcome(observations=ocr, attempted=len(ocr)),
        ),
        media_content_identity=identity,
    )


@pytest.mark.parametrize(
    ("asr", "ocr", "expected_kinds"),
    [
        ((TranscriptSpan(1000, 3000, "speech"),), (), ["ASR"]),
        ((), (OcrObservation(2500, "slide", "frame-1"),), ["OCR"]),
        (
            (TranscriptSpan(4000, 5000, "later"), TranscriptSpan(0, 1000, "first")),
            (OcrObservation(2000, "slide", "frame-2"),),
            ["ASR", "OCR", "ASR"],
        ),
    ],
)
def test_original_observations_keep_exact_time_text_and_identity(
    asr, ocr, expected_kinds
) -> None:
    context = _context(asr, ocr)
    page = temporal_observation_page(context, limit=20, offset=0)
    assert page["granularity"] == "source-observation"
    assert page["sourceRevision"] == context.source_revision
    assert [item["kind"] for item in page["items"]] == expected_kinds
    assert [item["startMs"] for item in page["items"]] == sorted(
        item["startMs"] for item in page["items"]
    )
    source_ids = {
        item.source_item_id for segment in context.segments for item in segment.source_items
    }
    assert {item["id"] for item in page["items"]} == source_ids
    assert all(item["sourceRevision"] == context.source_revision for item in page["items"])
    assert all("frameRef" not in item for item in page["items"])
    assert [item["text"] for item in page["items"]] == [
        item.text for item in sorted(context.observations, key=lambda value: value.source_item.timestamp_ms)
    ]


def test_observation_paging_legacy_fallback_and_validation() -> None:
    context = _context(
        (TranscriptSpan(0, 1000, "first"), TranscriptSpan(5000, 6000, "second"))
    )
    assert temporal_observation_page(context, limit=1, offset=1)["items"][0]["text"] == "second"
    assert temporal_observation_page(context, limit=1, offset=2)["items"] == []
    legacy = VideoContext(source=context.source, segments=context.segments)
    assert temporal_observation_page(legacy, limit=10, offset=0)["available"] is False
    assert temporal_observation_page(None, limit=10, offset=0)["available"] is False
    with pytest.raises(ValueError, match="text does not match"):
        TemporalObservation(source_item=context.observations[0].source_item, text="tampered")
    with pytest.raises(ValueError, match="not part of this context"):
        VideoContext(
            source=context.source,
            source_revision="other-revision",
            segments=context.segments,
            observations=context.observations,
        )


@pytest.mark.asyncio
async def test_observations_survive_restart_and_replace_previous_revision(tmp_path) -> None:
    path = tmp_path / "context.sqlite3"
    first_store = SqliteCheckpointStore(path)
    first = AgentCheckpointService(
        CheckpointRepository(first_store, InMemoryHotCheckpointCache())
    )
    old = _context((TranscriptSpan(0, 1000, "old"),), identity="sha256:old")
    await first.save_context(7, old)
    first_store.close()

    second_store = SqliteCheckpointStore(path)
    second = AgentCheckpointService(
        CheckpointRepository(second_store, InMemoryHotCheckpointCache())
    )
    loaded = await second.load_context(7)
    assert loaded is not None
    assert loaded.observations == old.observations
    assert loaded.user_goal == ""

    new = _context((TranscriptSpan(2000, 3000, "new"),), identity="sha256:new")
    await second.save_context(7, new)
    latest = await second.load_context(7)
    assert latest is not None
    page = temporal_observation_page(latest, limit=10, offset=0)
    assert page["sourceRevision"] == new.source_revision
    assert [item["text"] for item in page["items"]] == ["new"]
    assert old.observations[0].source_item.source_item_id not in {
        item["id"] for item in page["items"]
    }
    second_store.close()
