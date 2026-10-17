"""Bounded audio-only MP3 export using the existing subprocess owner."""
import json
from .runner import AsyncSubprocessRunner


class NoAudioTrack(ValueError):
    pass


async def export_mp3(source, target, *, ffmpeg="ffmpeg", ffprobe="ffprobe", runner=None):
    runner = runner or AsyncSubprocessRunner()
    probe = await runner.run((ffprobe, "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "json", str(source)), timeout=60)
    if not json.loads(probe.stdout).get("streams"):
        raise NoAudioTrack("media has no audio track")
    await runner.run((ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(source),
        "-map", "0:a:0", "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(target)), timeout=900)
