"""Safe asynchronous execution of local media tools.

Only ``create_subprocess_exec`` is used here.  Commands are represented as a
sequence of arguments and are never passed through a shell, which keeps media
paths and filter expressions from becoming shell syntax.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from os import PathLike

from .errors import (
    SubprocessExecutionError,
    SubprocessLaunchError,
    SubprocessTimeoutError,
)

DEFAULT_PROCESS_TIMEOUT_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class SubprocessResult:
    """Captured result of one completed process invocation."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def args(self) -> tuple[str, ...]:
        """Alias matching :mod:`subprocess` terminology."""

        return self.command

    @property
    def combined_output(self) -> str:
        """The two captured streams in a deterministic parse-friendly form."""

        if not self.stdout:
            return self.stderr
        if not self.stderr:
            return self.stdout
        return f"{self.stdout}\n{self.stderr}"


class AsyncSubprocessRunner:
    """Run an argument vector asynchronously with bounded process lifetime."""

    def __init__(self, *, default_timeout: float | None = DEFAULT_PROCESS_TIMEOUT_SECONDS) -> None:
        if default_timeout is not None and default_timeout <= 0:
            raise ValueError("default_timeout must be positive or None")
        self.default_timeout = default_timeout

    async def run(
        self,
        args: Sequence[str | PathLike[str]],
        *,
        cwd: str | PathLike[str] | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> SubprocessResult:
        """Execute ``args`` without a shell and capture stdout/stderr.

        ``timeout`` applies to process completion, not merely process startup.
        On timeout or cancellation the child is killed and reaped before the
        exception is raised, preventing orphaned ffmpeg/ffprobe processes.
        """

        command = _normalize_args(args)
        effective_timeout = self.default_timeout if timeout is None else timeout
        if effective_timeout is not None and effective_timeout <= 0:
            raise ValueError("timeout must be positive or None")

        process_env = None if env is None else dict(env)
        process_cwd = None if cwd is None else os.fspath(cwd)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=process_cwd,
                env=process_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise SubprocessLaunchError(
                f"could not start process: {command[0]}",
                command=command,
                stderr=str(exc),
            ) from exc

        communication = asyncio.create_task(process.communicate())
        try:
            if effective_timeout is None:
                stdout_bytes, stderr_bytes = await communication
            else:
                # Shield keeps the reader task alive while the child is being
                # terminated, allowing us to drain both pipes deterministically.
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    asyncio.shield(communication), effective_timeout
                )
        except asyncio.TimeoutError as exc:
            await _terminate_and_reap(process)
            stdout_bytes, stderr_bytes = await communication
            stdout = _decode(stdout_bytes)
            stderr = _decode(stderr_bytes)
            raise SubprocessTimeoutError(
                f"process timed out after {effective_timeout:g}s: {command[0]}",
                command=command,
                timeout_seconds=effective_timeout,
                stdout=stdout,
                stderr=stderr,
            ) from exc
        except asyncio.CancelledError:
            await _terminate_and_reap(process)
            await communication
            raise

        result = SubprocessResult(
            command=command,
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=_decode(stdout_bytes),
            stderr=_decode(stderr_bytes),
        )
        if result.returncode != 0:
            raise SubprocessExecutionError(
                f"process exited with code {result.returncode}: {command[0]}",
                command=command,
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode,
            )
        return result

    # ``execute`` is a descriptive alias for adapters that avoid the generic
    # word ``run``; both names share exactly the same safety guarantees.
    execute = run


async def _terminate_and_reap(process: asyncio.subprocess.Process) -> None:
    """Kill a child if needed and wait for its OS handle to be reaped."""

    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await process.wait()
    except ProcessLookupError:
        # The child exited between the return-code check and ``wait``.
        return


def _normalize_args(args: Sequence[str | PathLike[str]]) -> tuple[str, ...]:
    if isinstance(args, (str, bytes)):
        raise TypeError("subprocess command must be an argument sequence")
    if not args:
        raise ValueError("a subprocess command cannot be empty")
    normalized: list[str] = []
    for value in args:
        try:
            item = os.fspath(value)
        except TypeError as exc:
            raise TypeError("subprocess arguments must be text or path-like") from exc
        if isinstance(item, bytes):
            item = os.fsdecode(item)
        if not isinstance(item, str) or not item:
            raise ValueError("subprocess arguments must be non-empty text")
        if "\x00" in item:
            raise ValueError("subprocess arguments cannot contain NUL")
        normalized.append(item)
    return tuple(normalized)


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


__all__ = [
    "AsyncSubprocessRunner",
    "DEFAULT_PROCESS_TIMEOUT_SECONDS",
    "SubprocessResult",
]
