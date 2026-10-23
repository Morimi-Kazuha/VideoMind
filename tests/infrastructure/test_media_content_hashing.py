"""All media ingestion adapters persist the SHA-256 of the stored video bytes."""

import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from dovideo.application import UrlDownloadResult
from dovideo.infrastructure.media import (
    HASH_READ_BYTES,
    InMemoryMediaRecordStore,
    InMemoryObjectStorage,
    MediaIngestService,
)
from dovideo.presentation.api.r2_runtime import ProductionMediaStore
from dovideo.presentation.api.runtime import LocalR1Services


@pytest.mark.asyncio
async def test_sha256_helper_uses_bounded_reads_and_keeps_caller_stream_open():
    payload = bytes(range(256)) * 100

    class BoundedInput(io.BytesIO):
        def read(self, size=-1):
            assert size == HASH_READ_BYTES == 8 * 1024
            return super().read(size)

    stream = BoundedInput(payload)
    service = MediaIngestService(InMemoryObjectStorage(), InMemoryMediaRecordStore())

    assert await service.calculate_sha256(stream) == hashlib.sha256(payload).hexdigest()
    assert stream.tell() == len(payload)
    assert not stream.closed


@pytest.mark.asyncio
async def test_sha256_helper_reads_the_real_file(tmp_path: Path):
    payload = bytes(range(256)) * 100
    path = tmp_path / "input.mp4"
    path.write_bytes(payload)
    service = MediaIngestService(InMemoryObjectStorage(), InMemoryMediaRecordStore())

    assert await service.calculate_sha256(path) == hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_production_direct_upload_persists_sha256_of_stored_bytes():
    payload = bytes(range(256)) * 5000
    records = InMemoryMediaRecordStore()
    storage = InMemoryObjectStorage()
    service = ProductionMediaStore(
        SimpleNamespace(media_repository=records, object_storage=storage)
    )

    record = await service.ingest(7, "input.mp4", payload, "video/mp4")

    assert record.content_hash == hashlib.sha256(payload).hexdigest()
    assert await records.get(record.media_id) == record
    assert list(storage.objects.values()) == [payload]


@pytest.mark.asyncio
async def test_local_url_ingest_fingerprints_actual_downloaded_bytes(monkeypatch, tmp_path):
    payload = bytes(range(256)) * 100

    async def download(self, url, workspace):
        path = workspace.path / "download.mp4"
        path.write_bytes(payload)
        return UrlDownloadResult(path=path, filename="download.mp4")

    monkeypatch.setattr(
        "dovideo.infrastructure.media.url.DirectMp4Downloader.download", download
    )
    service = LocalR1Services(work_dir=tmp_path)

    record = await service.ingest_url(7, "https://example.test/download.mp4")

    assert record.content_hash == hashlib.sha256(payload).hexdigest()
    assert Path(record.source).read_bytes() == payload
    assert await service.media.get(record.media_id) == record
