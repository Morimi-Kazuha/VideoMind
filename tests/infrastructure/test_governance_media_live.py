"""Real MinIO, Redis, MySQL, HTTP streaming and FFmpeg; no model calls."""
import asyncio
import json
import os
import shutil
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from test_upload_redis_minio_live import live  # isolated upload fixture and teardown
from dovideo.infrastructure.media.runner import AsyncSubprocessRunner
from dovideo.presentation.api.app import create_app
from dovideo.presentation.api.r2_runtime import ProductionR2Services

pytestmark = pytest.mark.skipif(os.environ.get("DOVIDEO_UPLOAD_LIVE") != "1",
    reason="explicit live infrastructure opt-in required")


def test_real_cross_instance_playback_audio_and_expiry(live, tmp_path, monkeypatch):
    first, second, owner, service, init, credentials = live
    ffmpeg = os.environ.get("DOVIDEO_FFMPEG") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("DOVIDEO_FFPROBE") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg tools unavailable")
    monkeypatch.setenv("DOVIDEO_FFMPEG", ffmpeg)
    monkeypatch.setenv("DOVIDEO_FFPROBE", ffprobe)
    runner = AsyncSubprocessRunner()
    source = tmp_path / "fixture.mp4"
    asyncio.run(runner.run((ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        "color=c=black:s=64x64:r=1", "-f", "lavfi", "-i", "sine=frequency=440",
        "-t", "1", "-c:v", "mpeg4", "-c:a", "aac", str(source))))
    async def upload(path):
        session = await init(1)
        await service().upload_chunk(session.upload_id, 0, 1, path.read_bytes(), owner)
        return await service().complete(session.upload_id, owner)
    record = asyncio.run(upload(source))
    a, b = ProductionR2Services(first), ProductionR2Services(second)
    token = a.auth.login(*credentials).token
    headers = {"Authorization": f"Bearer {token}"}
    grants = []
    try:
        with TestClient(create_app(services=a)) as client_a, TestClient(create_app(services=b)) as client_b:
            grant = client_a.get("/media/playback", params={"id":record.media_id}, headers=headers)
            assert grant.status_code == 200
            url = grant.json()["data"]
            access = parse_qs(urlsplit(url).query)["access_token"][0]
            grants.append(a.playback_grants.key(access))
            playback = client_b.get(url, headers={"Range":"bytes=0-15"})
            assert playback.status_code == 206 and playback.content == source.read_bytes()[:16]
            assert list(b.response_files.root.iterdir()) == []
            audio = client_b.get("/analysis/download", params={"id":record.media_id}, headers=headers)
            assert audio.status_code == 200 and audio.headers["content-type"] == "audio/mpeg"
            output = tmp_path / "download.mp3"
            output.write_bytes(audio.content)
            probe = asyncio.run(runner.run((ffprobe, "-v", "error", "-show_streams", "-of", "json", str(output))))
            streams = json.loads(probe.stdout)["streams"]
            assert len(streams) == 1 and streams[0]["codec_name"] == "mp3"
            assert list(b.response_files.root.iterdir()) == []
            first.redis_client.delete(grants[0])
            assert client_b.get(url).status_code == 401
            silent = tmp_path / "silent.mp4"
            asyncio.run(runner.run((ffmpeg, "-nostdin", "-v", "error", "-i", str(source),
                "-an", "-c:v", "copy", str(silent))))
            no_audio = asyncio.run(upload(silent))
            assert client_b.get("/analysis/download", params={"id":no_audio.media_id}, headers=headers).status_code == 422
            assert list(b.response_files.root.iterdir()) == []
    finally:
        a.auth.logout(f"Bearer {token}")
        for key in grants:
            first.redis_client.delete(key)
