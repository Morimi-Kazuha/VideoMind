import asyncio
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from dovideo.infrastructure.media.response_files import ResponseFiles, RedisPlaybackGrants, joined_thread
from dovideo.presentation.api.file_response import OwnedFileResponse
from dovideo.presentation.api.r2_runtime import ProductionR2Services
from dovideo.application.media import MediaRecord


def test_response_range_and_cleanup(tmp_path):
    files = ResponseFiles(tmp_path)
    path = files.allocate(".mp4")
    path.write_bytes(b"0123456789")
    app = FastAPI()
    async def release(path): files.release(Path(path))
    @app.get("/media")
    def media(): return OwnedFileResponse(path, release=release, media_type="video/mp4")
    with TestClient(app) as client:
        response = client.get("/media", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206 and response.content == b"2345"
    assert not path.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError, asyncio.CancelledError])
async def test_response_cleanup_when_sender_fails(tmp_path, error):
    files = ResponseFiles(tmp_path)
    path = files.allocate(".mp4")
    path.write_bytes(b"video")
    async def release(path): files.release(Path(path))
    async def send(message): raise error()
    response = OwnedFileResponse(path, release=release)
    with pytest.raises(error):
        await response({"type":"http", "asgi":{"spec_version":"2.4"}, "method":"GET", "headers":[], "extensions":{}}, None, send)
    assert not path.parent.exists()


@pytest.mark.asyncio
async def test_cancelled_download_joins_writer_before_cleanup(tmp_path):
    started, finish = threading.Event(), threading.Event()
    files = ResponseFiles(tmp_path)
    path = files.allocate(".mp4")
    def writer():
        started.set()
        finish.wait(5)
        path.write_bytes(b"finished")
    task = asyncio.create_task(joined_thread(writer))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(.02)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert path.read_bytes() == b"finished"
    files.release(path)
    assert not path.parent.exists()


def test_scavenger_preserves_active_owner_and_removes_abandoned_directory(tmp_path):
    first = ResponseFiles(tmp_path)
    active = first.allocate(".mp4")
    old = time.time() - 90000
    os.utime(active.parent, (old, old))
    second = ResponseFiles(tmp_path)
    assert active.parent.exists()
    # Simulate OS releasing the advisory lock after process exit.
    first._handles.pop(active).close()
    second.scavenge()
    assert not active.parent.exists()


@pytest.mark.asyncio
async def test_failed_object_read_removes_partial_file(tmp_path):
    class Response:
        closed = released = False
        def read(self, size): raise OSError("failed midway")
        def close(self): self.closed = True
        def release_conn(self): self.released = True
    response = Response()
    service = object.__new__(ProductionR2Services)
    service.response_files = ResponseFiles(tmp_path)
    service.infrastructure = SimpleNamespace(settings=SimpleNamespace(minio_bucket="media"),
        minio_client=SimpleNamespace(get_object=lambda *args: response))
    with pytest.raises(Exception):
        await service._download_object(MediaRecord(user_id=1, filename="a.mp4", source="minio://media/a.mp4"))
    assert response.closed and response.released
    assert list(service.response_files.root.iterdir()) == []


@pytest.mark.asyncio
async def test_real_redis_cross_instance_grants_expire():
    url = os.environ.get("DOVIDEO_GOVERNANCE_REDIS")
    if not url: pytest.skip("isolated Redis opt-in")
    from redis import Redis
    client = Redis.from_url(url)
    a, b = RedisPlaybackGrants(client), RedisPlaybackGrants(client)
    token = await a.issue(12, 7)
    try:
        assert await b.owner(12, token) == 7
        assert await b.owner(13, token) is None
        assert 0 < client.ttl(a.key(token)) <= 900
        client.pexpire(a.key(token), 1)
        await asyncio.sleep(.03)
        assert await b.owner(12, token) is None
    finally:
        client.delete(a.key(token))
        client.close()
