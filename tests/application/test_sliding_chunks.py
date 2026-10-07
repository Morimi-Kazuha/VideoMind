import pytest

from dovideo.application.chunking import VideoChunkingService, chunks_compatible, chunk_windows
from dovideo.application.errors import BudgetExceededError
from dovideo.application.long_context import LongVideoContextService
from dovideo.domain import CHUNKING_CONTRACT_VERSION, ChunkSummary, VideoChunk, VideoSegment, chunk_id_for


def segment(start, end=None, **kwargs):
    return VideoSegment(start_ms=start, end_ms=end or start + 60_000, **kwargs)


class Summary:
    async def summarize_chunk(self, segments):
        return ChunkSummary(segment_summary="summary")


class Embedding:
    async def embed(self, text):
        return (1.0, 0.0)


@pytest.mark.parametrize("tail,starts", [(0, [0]), (240_000, [0]), (300_000, [0, 240_000]),
                                        (480_000, [0, 240_000]), (540_000, [0, 240_000, 480_000])])
def test_tail_stops_after_first_window_covering_end(tail, starts):
    segments = tuple(segment(t) for t in range(0, tail + 1, 60_000))
    windows = chunk_windows(segments)
    assert [start for start, _, _ in windows] == starts
    assert all(any(s in raw for _, _, raw in windows) for s in segments)
    assert all(end - start == 300_000 for start, end, _ in windows)


def test_overlap_gap_half_open_and_crossing_segment():
    before = segment(240_000)
    boundary = segment(300_000)
    crossing = segment(290_000, 320_000)
    late = segment(1_440_000)
    windows = chunk_windows((late, before, crossing, boundary))
    assert [s for s, _, _ in windows] == [0, 240_000, 1_200_000]
    assert before in windows[0][2] and before in windows[1][2]
    assert boundary not in windows[0][2] and boundary in windows[1][2]
    assert crossing in windows[0][2] and crossing in windows[1][2]
    assert chunk_windows(()) == ()


@pytest.mark.asyncio
async def test_checkpoint_versions_ids_universe_and_revision_are_checked():
    segments = (segment(0, source_revision="a" * 64, segment_id="s1"),
                segment(300_000, source_revision="a" * 64, segment_id="s2"))
    chunks = await VideoChunkingService(Summary(), Embedding()).build(segments)
    assert chunks_compatible(chunks, segments)
    assert all(c.chunking_version == CHUNKING_CONTRACT_VERSION for c in chunks)
    assert all(c.chunk_id == chunk_id_for(c.source_revision, c.start_ms, c.end_ms) for c in chunks)
    for field, value in [("chunking_version", "video-chunk-5m-v1"), ("chunk_id", "bad"),
                         ("source_revision", "b" * 64), ("raw_segments", ()), ("end_ms", 400_000)]:
        bad = (chunks[0].model_copy(update={field: value}), *chunks[1:])
        assert not chunks_compatible(bad, segments)
    assert not chunks_compatible(chunks[:1], segments)
    assert not chunks_compatible(chunks, (segments[0].model_copy(update={"transcript": "changed"}), segments[1]))


@pytest.mark.asyncio
async def test_invalid_checkpoint_rebuilds_saves_and_reindexes():
    segments = (segment(0), segment(300_000))
    events = []
    class Checkpoint:
        async def load_chunks(self, media):
            return (VideoChunk(start_ms=0, end_ms=300_000, raw_segments=segments),)
        async def save_chunks(self, media, chunks):
            events.append(("save", chunks))
    class Retrieval:
        async def index(self, media, chunks):
            events.append(("index", chunks))
    service = LongVideoContextService(VideoChunkingService(Summary(), Embedding()), Retrieval(), Checkpoint())
    chunks = await service._resolve_chunks(1, segments)
    assert [e[0] for e in events] == ["save", "index"]
    assert events[0][1] == events[1][1] == chunks


@pytest.mark.asyncio
async def test_chunk_embedding_budget_and_cancellation_escape():
    import asyncio
    for error in (BudgetExceededError("budget"), asyncio.CancelledError()):
        class Failing:
            async def embed(self, text):
                raise error
        with pytest.raises(type(error)):
            await VideoChunkingService(Summary(), Failing()).build((segment(0),))
