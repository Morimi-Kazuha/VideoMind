"""Deterministic FFmpeg audio segmentation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from os import PathLike
from pathlib import Path

from .runner import AsyncSubprocessRunner
from .workspace import MediaWorkspace, ScopedArtifact

AUDIO_SEGMENT_SECONDS = 60
AUDIO_SEGMENT_MILLISECONDS = AUDIO_SEGMENT_SECONDS * 1000
_AUDIO_FILE_RE = re.compile(r"^audio_(?P<index>[0-9]+)\.mp3$")


@dataclass(frozen=True, slots=True)
class AudioSegment:
    """An FFmpeg-produced segment valid within its :class:`MediaWorkspace`."""

    index: int
    start_ms: int
    artifact: ScopedArtifact
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("segment index cannot be negative")
        if self.start_ms < 0:
            raise ValueError("segment start cannot be negative")
        if not isinstance(self.artifact, ScopedArtifact):
            raise TypeError("artifact must be a ScopedArtifact")
        if self.duration_ms is not None and self.duration_ms <= 0:
            raise ValueError("segment duration must be positive when present")

    @property
    def path(self) -> Path:
        """Resolve the live path; raises once the workspace is closed."""

        return self.artifact.path

    @property
    def filename(self) -> str:
        return self.artifact.name

    @property
    def offset_ms(self) -> int:
        """Java-compatible 60-second offset for this output index."""

        return self.start_ms

    @property
    def end_ms(self) -> int:
        """Nominal end of the fixed-size window (the final file may be shorter)."""

        return self.start_ms + (
            AUDIO_SEGMENT_MILLISECONDS if self.duration_ms is None else self.duration_ms
        )

    @property
    def relative_path(self) -> str:
        return self.artifact.relative_path


class AudioSegmenter:
    """Transcode a media source and discover the produced 60-second files."""

    def __init__(
        self,
        runner: AsyncSubprocessRunner,
        *,
        executable: str | PathLike[str] = "ffmpeg",
        timeout: float = 15 * 60,
    ) -> None:
        self._runner = runner
        self._executable = _tool_name(executable)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout

    async def segment(
        self,
        source: str | PathLike[str],
        workspace: MediaWorkspace,
    ) -> tuple[AudioSegment, ...]:
        """Run FFmpeg and return only files actually discovered in the scope.

        The count is never derived from a duration estimate.  Missing indexes
        are retained as named by FFmpeg so their offsets remain deterministic.
        """

        source_arg = _source_arg(source)
        output_dir = await workspace.directory("audio")
        _remove_previous_outputs(output_dir)
        output_pattern = output_dir / "audio_%03d.mp3"
        command = (
            self._executable,
            "-y",
            "-i",
            source_arg,
            "-vn",
            "-acodec",
            "libmp3lame",
            "-f",
            "segment",
            "-segment_time",
            str(AUDIO_SEGMENT_SECONDS),
            "-reset_timestamps",
            "1",
            os.fspath(output_pattern),
        )
        await self._runner.run(command, cwd=workspace.path, timeout=self._timeout)

        artifacts: list[AudioSegment] = []
        for index, path in _discover_outputs(output_dir):
            artifacts.append(
                AudioSegment(
                    index=index,
                    start_ms=index * AUDIO_SEGMENT_MILLISECONDS,
                    artifact=workspace.artifact(path),
                )
            )
        return tuple(artifacts)

    extract = segment
    segment_audio = segment


def _discover_outputs(directory: Path) -> list[tuple[int, Path]]:
    discovered: list[tuple[int, Path]] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = _AUDIO_FILE_RE.fullmatch(path.name)
        if match is not None:
            discovered.append((int(match.group("index")), path))
    discovered.sort(key=lambda item: (item[0], item[1].name))
    return discovered


def _remove_previous_outputs(directory: Path) -> None:
    """Clear only this adapter's directly generated names in its own scope."""

    for path in directory.iterdir():
        if path.is_file() and _AUDIO_FILE_RE.fullmatch(path.name) is not None:
            path.unlink()


def _tool_name(value: str | PathLike[str]) -> str:
    try:
        result = os.fspath(value)
    except TypeError as exc:
        raise TypeError("ffmpeg executable must be text or path-like") from exc
    if isinstance(result, bytes):
        result = os.fsdecode(result)
    if not isinstance(result, str) or not result or "\x00" in result:
        raise ValueError("ffmpeg executable must be non-empty text")
    return result


def _source_arg(value: str | PathLike[str]) -> str:
    try:
        result = os.fspath(value)
    except TypeError as exc:
        raise TypeError("media source must be text or path-like") from exc
    if isinstance(result, bytes):
        result = os.fsdecode(result)
    if not isinstance(result, str) or not result or "\x00" in result:
        raise ValueError("media source must be non-empty text")
    return result


FFmpegAudioSegmenter = AudioSegmenter


__all__ = [
    "AUDIO_SEGMENT_MILLISECONDS",
    "AUDIO_SEGMENT_SECONDS",
    "AudioSegment",
    "AudioSegmenter",
    "FFmpegAudioSegmenter",
]
