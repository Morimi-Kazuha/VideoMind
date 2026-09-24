from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from dovideo.application import InvalidMediaInput, MediaRecord, MediaRecordFailure
from dovideo.application.errors import UrlDownloadFailure
from dovideo.infrastructure.media import (
    InMemoryMediaRecordStore,
    InMemoryObjectStorage,
    MediaIngestService,
    MutableClock,
    normalize_video_filename,
)


class BoundedStream:
    def __init__(self, payload: bytes, name: str = "input.mp4") -> None:
        self.payload = payload
        self.position = 0
        self.name = name
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            size = len(self.payload) - self.position
        result = self.payload[self.position : self.position + size]
        self.position += len(result)
        return result


class InvalidRecordStore(InMemoryMediaRecordStore):
    async def save(self, record: MediaRecord) -> object:
        self.save_calls += 1
        return object()


class UnpersistedRecordStore(InMemoryMediaRecordStore):
    async def save(self, record: MediaRecord) -> MediaRecord:
        self.save_calls += 1
        return record


class UrlFakeDownloader:
    def __init__(self, payload: bytes = b"downloaded") -> None:
        self.payload = payload
        self.calls: list[str] = []

    async def download(self, url: str, workspace: object):
        self.calls.append(url)
        directory = await workspace.directory("fake-download")
        path = directory / "remote.m4v"
        path.write_bytes(self.payload)
        from dovideo.application import UrlDownloadResult

        return UrlDownloadResult(path=path, filename="remote.m4v")


def test_java_filename_normalization_and_object_key_safety() -> None:
    assert normalize_video_filename(r" C:\uploads\nested\clip.MP4 ") == "clip.MP4"
    assert normalize_video_filename("movie.WEBM") == "movie.WEBM"
    with pytest.raises(InvalidMediaInput):
        normalize_video_filename(" ")
    with pytest.raises(InvalidMediaInput):
        normalize_video_filename("clip.exe")
    with pytest.raises(InvalidMediaInput):
        normalize_video_filename("a" * 256 + ".mp4")


@pytest.mark.asyncio
async def test_direct_ingest_streams_md5_and_normalized_suffix() -> None:
    payload = b"0123456789" * 20
    stream = BoundedStream(payload, r"C:\unsafe\take.MOV")
    objects = InMemoryObjectStorage()
    records = InMemoryMediaRecordStore()
    result = await MediaIngestService(
        objects,
        records,
        clock=MutableClock(),
        stream_chunk_bytes=17,
    ).ingest_file(stream, 7)

    assert result.filename == "take.MOV"
    assert result.content_hash == hashlib.md5(payload).hexdigest()
    assert result.status.value == "COMPLETED"
    assert result.content_type == "video/quicktime"
    assert len(objects.objects) == 1
    assert next(iter(objects.objects.values())) == payload
    assert objects.object_name_from_source(result.source) == next(iter(objects.objects))
    assert max(stream.read_sizes) <= 17
    assert all("\\" not in key and ".." not in key for key in objects.objects)


@pytest.mark.asyncio
async def test_empty_file_is_rejected_before_upload() -> None:
    objects = InMemoryObjectStorage()
    records = InMemoryMediaRecordStore()
    service = MediaIngestService(objects, records)
    with pytest.raises(InvalidMediaInput):
        await service.ingest_file(b"", 1, filename="empty.mp4")
    assert objects.put_calls == []
    assert records.save_calls == 0


@pytest.mark.asyncio
async def test_invalid_record_result_rolls_back_uploaded_object() -> None:
    objects = InMemoryObjectStorage()
    records = InvalidRecordStore()
    service = MediaIngestService(objects, records)
    with pytest.raises(MediaRecordFailure):
        await service.ingest_file(b"payload", 1, filename="video.mp4")
    assert objects.objects == {}
    assert len(objects.delete_calls) == 1


@pytest.mark.asyncio
async def test_unpersisted_record_result_rolls_back_uploaded_object() -> None:
    objects = InMemoryObjectStorage()
    records = UnpersistedRecordStore()
    service = MediaIngestService(objects, records)

    with pytest.raises(MediaRecordFailure, match="persisted media id"):
        await service.ingest_file(b"payload", 1, filename="video.mp4")

    assert objects.objects == {}
    assert len(objects.delete_calls) == 1


@pytest.mark.asyncio
async def test_record_failure_preserves_original_and_cleanup_failure_note() -> None:
    objects = InMemoryObjectStorage()
    records = InMemoryMediaRecordStore()
    records.save_error = RuntimeError("record unavailable")
    objects.delete_error = RuntimeError("delete unavailable")
    service = MediaIngestService(objects, records)
    with pytest.raises(RuntimeError, match="record unavailable") as caught:
        await service.ingest_file(b"payload", 1, filename="video.mp4")
    assert hasattr(caught.value, "cleanup_error")
    assert len(objects.objects) == 1


@pytest.mark.asyncio
async def test_url_ingest_owns_and_cleans_download_workspace(tmp_path: Path) -> None:
    downloader = UrlFakeDownloader(b"url-content")
    objects = InMemoryObjectStorage()
    records = InMemoryMediaRecordStore()
    result = await MediaIngestService(
        objects,
        records,
        downloader,
        workspace_parent=tmp_path,
    ).ingest_url("https://example.test/video", 2)

    assert result.filename == "remote.m4v"
    assert result.content_hash == hashlib.md5(b"url-content").hexdigest()
    assert downloader.calls == ["https://example.test/video"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_url_ingest_rejects_path_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")

    class BadDownloader:
        async def download(self, url: str, workspace: object):
            from dovideo.application import UrlDownloadResult

            return UrlDownloadResult(path=outside, filename="outside.mp4")

    service = MediaIngestService(
        InMemoryObjectStorage(),
        InMemoryMediaRecordStore(),
        BadDownloader(),
        workspace_parent=tmp_path,
    )
    with pytest.raises(UrlDownloadFailure):
        await service.ingest_url("https://example.test/video", 2)
    assert outside.exists()
    assert [p for p in tmp_path.iterdir() if p.is_dir()] == []
