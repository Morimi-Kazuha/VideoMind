import json
import asyncio
import os
import shutil
from pathlib import Path
import pytest
from dovideo.infrastructure.media.audio_export import export_mp3, NoAudioTrack
from dovideo.infrastructure.media.runner import AsyncSubprocessRunner
from dovideo.infrastructure.media.response_files import ResponseFiles
from dovideo.presentation.api.r2_runtime import ProductionR2Services, R1ServiceError
from dovideo.application.media import MediaRecord
from types import SimpleNamespace
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_real_mp3_and_no_audio(tmp_path):
    ffmpeg = os.environ.get("DOVIDEO_FFMPEG", shutil.which("ffmpeg"))
    ffprobe = os.environ.get("DOVIDEO_FFPROBE", shutil.which("ffprobe"))
    if not ffmpeg or not ffprobe: pytest.skip("FFmpeg tools unavailable")
    runner = AsyncSubprocessRunner()
    source, target = tmp_path / "source.mp4", tmp_path / "audio.mp3"
    await runner.run((ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        "color=c=black:s=64x64:r=1", "-f", "lavfi", "-i", "sine=frequency=440",
        "-t", "1", "-c:v", "mpeg4", "-c:a", "aac", str(source)))
    await export_mp3(source, target, ffmpeg=ffmpeg, ffprobe=ffprobe)
    probe = await runner.run((ffprobe, "-v", "error", "-show_streams", "-of", "json", str(target)))
    streams = json.loads(probe.stdout)["streams"]
    assert len(streams) == 1 and streams[0]["codec_name"] == "mp3" and streams[0]["codec_type"] == "audio"
    silent = tmp_path / "silent.mp4"
    await runner.run((ffmpeg, "-nostdin", "-v", "error", "-i", str(source), "-an", "-c:v", "copy", str(silent)))
    with pytest.raises(NoAudioTrack):
        await export_mp3(silent, tmp_path / "empty.mp3", ffmpeg=ffmpeg, ffprobe=ffprobe)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, status", [
    (NoAudioTrack("private input path"), 422),
    (OSError("private converter details"), 503),
    (TimeoutError("private timeout details"), 503),
    (asyncio.CancelledError(), None),
])
async def test_audio_service_releases_both_files_on_failure(monkeypatch, tmp_path, failure, status):
    service = object.__new__(ProductionR2Services)
    service.response_files = ResponseFiles(tmp_path)
    source = service.response_files.allocate(".mp4")
    source.write_bytes(b"source")
    record = MediaRecord(media_id=1, user_id=2, filename="a.mp4", source="minio://media/a")
    service.media = SimpleNamespace(require_owned=AsyncMock(return_value=record))
    service._download_object = AsyncMock(return_value=source)
    monkeypatch.setattr("dovideo.presentation.composition.AnalysisSettings.from_environment",
        lambda: SimpleNamespace(ffmpeg_executable="ffmpeg", ffprobe_executable="ffprobe"))
    async def convert(source, output, **kwargs):
        output.write_bytes(b"partial")
        raise failure
    monkeypatch.setattr("dovideo.infrastructure.media.audio_export.export_mp3", convert)
    with pytest.raises(asyncio.CancelledError if status is None else R1ServiceError) as caught:
        await service.download_path(1, 2)
    if status is not None:
        assert caught.value.status_code == status
        assert "private" not in caught.value.message
    assert list(service.response_files.root.iterdir()) == []
