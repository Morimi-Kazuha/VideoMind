import json
import os
import shutil
from pathlib import Path
import pytest
from dovideo.infrastructure.media.audio_export import export_mp3, NoAudioTrack
from dovideo.infrastructure.media.runner import AsyncSubprocessRunner


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
