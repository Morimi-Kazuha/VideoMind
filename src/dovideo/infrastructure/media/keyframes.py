"""Scene-change keyframe extraction with deterministic sampling fallback."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from os import PathLike
from pathlib import Path

from .runner import AsyncSubprocessRunner, SubprocessResult
from .workspace import MediaWorkspace, ScopedArtifact

KEYFRAME_FALLBACK_INTERVAL_SECONDS = 30
KEYFRAME_FALLBACK_INTERVAL_MILLISECONDS = KEYFRAME_FALLBACK_INTERVAL_SECONDS * 1000
DEFAULT_SCENE_THRESHOLD = 0.35
_FRAME_FILE_RE = re.compile(r"^frame_(?P<index>[0-9]+)\.jpg$")
_PTS_TIME_RE = re.compile(
    r"pts_time:(?P<seconds>[+-]?(?:\d+(?:\.\d*)?|\.\d+))"
)


class KeyframeSelection(str, Enum):
    SCENE_CHANGE = "scene_change"
    FIXED_INTERVAL = "fixed_interval"


@dataclass(frozen=True, slots=True)
class Keyframe:
    """An extracted JPEG and its source timestamp within a workspace."""

    index: int
    timestamp_ms: int
    artifact: ScopedArtifact
    selection: KeyframeSelection

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("keyframe index cannot be negative")
        if self.timestamp_ms < 0:
            raise ValueError("keyframe timestamp cannot be negative")
        if not isinstance(self.artifact, ScopedArtifact):
            raise TypeError("artifact must be a ScopedArtifact")
        if not isinstance(self.selection, KeyframeSelection):
            raise TypeError("selection must be a KeyframeSelection")

    @property
    def path(self) -> Path:
        return self.artifact.path

    @property
    def filename(self) -> str:
        return self.artifact.name

    @property
    def frame_ref(self) -> str:
        """Stable workspace-relative frame reference for later OCR stages."""

        return self.artifact.relative_path

    @property
    def relative_path(self) -> str:
        return self.artifact.relative_path


class KeyframeExtractor:
    """Run Java-compatible scene selection, then 30-second fallback sampling."""

    def __init__(
        self,
        runner: AsyncSubprocessRunner,
        *,
        executable: str | PathLike[str] = "ffmpeg",
        timeout: float = 15 * 60,
        scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
        fallback_interval_seconds: int = KEYFRAME_FALLBACK_INTERVAL_SECONDS,
    ) -> None:
        if not math.isfinite(scene_threshold) or not 0 <= scene_threshold <= 1:
            raise ValueError("scene_threshold must be finite and between 0 and 1")
        if fallback_interval_seconds <= 0:
            raise ValueError("fallback_interval_seconds must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._runner = runner
        self._executable = _tool_name(executable)
        self._timeout = timeout
        self._scene_threshold = scene_threshold
        self._fallback_interval_seconds = fallback_interval_seconds

    async def extract(
        self,
        source: str | PathLike[str],
        workspace: MediaWorkspace,
    ) -> tuple[Keyframe, ...]:
        """Extract sorted keyframes while keeping all artifacts in ``workspace``."""

        source_arg = _source_arg(source)
        output_dir = await workspace.directory("frames")
        _remove_previous_outputs(output_dir)
        output_pattern = output_dir / "frame_%06d.jpg"

        scene_result = await self._runner.run(
            self._command(source_arg, output_pattern, self._scene_filter()),
            cwd=workspace.path,
            timeout=self._timeout,
        )
        files = _discover_outputs(output_dir)
        selection = KeyframeSelection.SCENE_CHANGE
        result = scene_result
        if not files:
            # A successful scene pass with no output is the only condition for
            # fallback.  Tool failures remain visible as subprocess exceptions.
            _remove_previous_outputs(output_dir)
            result = await self._runner.run(
                self._command(source_arg, output_pattern, self._fallback_filter()),
                cwd=workspace.path,
                timeout=self._timeout,
            )
            files = _discover_outputs(output_dir)
            selection = KeyframeSelection.FIXED_INTERVAL

        timestamps = _parse_timestamps(result)
        frames: list[Keyframe] = []
        for position, (index, path) in enumerate(files):
            timestamp = (
                timestamps[position]
                if position < len(timestamps)
                else position * self._fallback_interval_seconds * 1000
            )
            frames.append(
                Keyframe(
                    index=index,
                    timestamp_ms=timestamp,
                    artifact=workspace.artifact(path),
                    selection=selection,
                )
            )
        return tuple(frames)

    extract_keyframes = extract

    def _command(self, source: str, output_pattern: Path, video_filter: str) -> tuple[str, ...]:
        return (
            self._executable,
            "-y",
            "-i",
            source,
            "-vf",
            video_filter,
            "-fps_mode",
            "vfr",
            os.fspath(output_pattern),
        )

    def _scene_filter(self) -> str:
        # This is one argument, not shell syntax.  The escaped comma matches
        # Java's ProcessBuilder invocation while remaining safe in exec mode.
        return (
            f"select=eq(n\\,0)+gt(scene\\,{self._scene_threshold:g})"
            "+gte(t-prev_selected_t\\,30),showinfo"
        )

    def _fallback_filter(self) -> str:
        return f"fps=1/{self._fallback_interval_seconds},showinfo"


def _discover_outputs(directory: Path) -> list[tuple[int, Path]]:
    discovered: list[tuple[int, Path]] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = _FRAME_FILE_RE.fullmatch(path.name)
        if match is not None:
            discovered.append((int(match.group("index")), path))
    discovered.sort(key=lambda item: (item[0], item[1].name))
    return discovered


def _remove_previous_outputs(directory: Path) -> None:
    for path in directory.iterdir():
        if path.is_file() and _FRAME_FILE_RE.fullmatch(path.name) is not None:
            path.unlink()


def _parse_timestamps(result: SubprocessResult) -> list[int]:
    values: list[int] = []
    # Java's appendTimestamp first filters each log line by the literal
    # ``showinfo`` marker, then applies PTS_TIME to that line.  Keeping the
    # same line boundary avoids treating unrelated tool diagnostics as frames.
    for line in result.combined_output.splitlines():
        if "showinfo" not in line:
            continue
        for match in _PTS_TIME_RE.finditer(line):
            try:
                seconds = Decimal(match.group("seconds"))
            except (InvalidOperation, ValueError):
                continue
            if not seconds.is_finite() or seconds < 0:
                continue
            milliseconds = int(seconds * 1000)
            values.append(milliseconds)
    return values


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


FFmpegKeyframeExtractor = KeyframeExtractor


__all__ = [
    "DEFAULT_SCENE_THRESHOLD",
    "FFmpegKeyframeExtractor",
    "KEYFRAME_FALLBACK_INTERVAL_MILLISECONDS",
    "KEYFRAME_FALLBACK_INTERVAL_SECONDS",
    "Keyframe",
    "KeyframeExtractor",
    "KeyframeSelection",
]
