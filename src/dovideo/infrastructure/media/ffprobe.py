"""ffprobe duration adapter."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from os import PathLike

from .errors import MediaProbeError
from .runner import AsyncSubprocessRunner


@dataclass(frozen=True, slots=True)
class MediaDuration:
    """A finite non-negative media duration."""

    seconds: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.seconds) or self.seconds < 0:
            raise ValueError("duration must be finite and non-negative")

    @property
    def milliseconds(self) -> int:
        return int(self.seconds * 1000)

    @property
    def duration_ms(self) -> int:
        return self.milliseconds


class FfprobeDurationAdapter:
    """Read ``format.duration`` using an argument-vector ffprobe call."""

    def __init__(
        self,
        runner: AsyncSubprocessRunner,
        *,
        executable: str | PathLike[str] = "ffprobe",
        timeout: float = 60.0,
    ) -> None:
        self._runner = runner
        self._executable = _tool_name(executable, "ffprobe")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout

    async def probe(self, source: str | PathLike[str]) -> MediaDuration:
        source_arg = _source_arg(source)
        command = (
            self._executable,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            source_arg,
        )
        try:
            result = await self._runner.run(command, timeout=self._timeout)
        except Exception as exc:
            if isinstance(exc, MediaProbeError):
                raise
            raise MediaProbeError(f"ffprobe failed for {source_arg}") from exc
        value = _parse_duration(result.stdout)
        if value is None:
            detail = result.stderr.strip() or "no duration in ffprobe output"
            raise MediaProbeError(f"invalid ffprobe duration for {source_arg}: {detail}")
        return MediaDuration(value)

    async def duration(self, source: str | PathLike[str]) -> float:
        """Return duration in seconds for callers that need a scalar."""

        return (await self.probe(source)).seconds

    # Names used by the application boundary during migration.
    get_duration = duration
    probe_duration = duration


def _parse_duration(output: str) -> float | None:
    for line in output.splitlines():
        candidate = line.strip()
        if not candidate or candidate.upper() in {"N/A", "NAN", "INF", "+INF", "-INF"}:
            continue
        try:
            decimal_value = Decimal(candidate)
        except (InvalidOperation, ValueError):
            continue
        if decimal_value.is_finite() and decimal_value >= 0:
            value = float(decimal_value)
            if math.isfinite(value):
                return value
    return None


def _tool_name(value: str | PathLike[str], label: str) -> str:
    try:
        result = os.fspath(value)
    except TypeError as exc:
        raise TypeError(f"{label} executable must be text or path-like") from exc
    if isinstance(result, bytes):
        result = os.fsdecode(result)
    if not isinstance(result, str) or not result or "\x00" in result:
        raise ValueError(f"{label} executable must be non-empty text")
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


# Compatibility aliases are intentionally class aliases, not wrappers.
FfprobeDurationProbe = FfprobeDurationAdapter
FFProbeDurationAdapter = FfprobeDurationAdapter


__all__ = [
    "FFProbeDurationAdapter",
    "FfprobeDurationAdapter",
    "FfprobeDurationProbe",
    "MediaDuration",
]
