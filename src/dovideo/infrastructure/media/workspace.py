"""Scoped temporary directories for media-tool artifacts."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path, PurePath
from types import TracebackType
from typing import Self

from .errors import WorkspaceCleanupError, WorkspaceClosedError, WorkspaceError


class MediaWorkspace:
    """An owned temporary directory with explicit asynchronous cleanup.

    A workspace owns only the directory created by ``mkdtemp``.  It never
    recursively deletes a caller-provided parent.  Artifact descriptors keep a
    reference to this scope and refuse to expose a path after the scope closes,
    so consumers cannot accidentally use a deleted path.
    """

    __slots__ = ("_parent", "_prefix", "_path", "_closed")

    def __init__(
        self,
        *,
        parent: str | os.PathLike[str] | None = None,
        prefix: str = "dovideo-media-",
    ) -> None:
        if not isinstance(prefix, str) or not prefix or "\x00" in prefix:
            raise ValueError("workspace prefix must be non-empty text without NUL")
        self._parent = None if parent is None else Path(os.fspath(parent)).resolve()
        self._prefix = prefix
        self._path: Path | None = None
        self._closed = False

    async def __aenter__(self) -> Self:
        if self._path is not None or self._closed:
            raise WorkspaceError("a media workspace cannot be entered twice")
        if self._parent is not None:
            await asyncio.to_thread(self._parent.mkdir, parents=True, exist_ok=True)
        created = await asyncio.to_thread(
            tempfile.mkdtemp,
            prefix=self._prefix,
            dir=None if self._parent is None else str(self._parent),
        )
        self._path = Path(created).resolve()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def path(self) -> Path:
        """Return the live workspace root, or raise after cleanup."""

        if self._path is None or self._closed:
            raise WorkspaceClosedError("media workspace is not active")
        return self._path

    @property
    def root(self) -> Path:
        """Readable alias for :attr:`path`."""

        return self.path

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        """Remove the owned directory exactly once."""

        if self._closed:
            return
        path = self._path
        self._closed = True
        if path is None:
            return
        try:
            await asyncio.to_thread(shutil.rmtree, path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise WorkspaceCleanupError(
                f"could not clean media workspace {path}"
            ) from exc

    cleanup = close

    async def directory(self, name: str) -> Path:
        """Create and return a safe direct child directory."""

        root = self.path
        component = _safe_component(name)
        directory = root / component
        await asyncio.to_thread(directory.mkdir, parents=False, exist_ok=True)
        return directory

    def artifact(self, path: str | os.PathLike[str]) -> "ScopedArtifact":
        """Describe an existing file beneath this workspace."""

        relative = self._relative_path(path, require_file=True)
        return ScopedArtifact(self, relative.relative_to(self.path).as_posix())

    def resolve(self, relative: str | os.PathLike[str], *, require_file: bool = False) -> Path:
        """Resolve a relative artifact path without allowing traversal."""

        return self._relative_path(relative, require_file=require_file)

    def _relative_path(
        self,
        value: str | os.PathLike[str],
        *,
        require_file: bool,
    ) -> Path:
        root = self.path
        try:
            candidate = Path(os.fspath(value))
        except TypeError as exc:
            raise TypeError("workspace paths must be text or path-like") from exc
        if any(part in (".", "..") for part in candidate.parts):
            raise WorkspaceError("artifact path cannot contain traversal components")
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        else:
            resolved = (root / candidate).resolve(strict=False)
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise WorkspaceError("artifact path escapes the workspace") from exc
        if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
            raise WorkspaceError("artifact path must be a non-empty safe relative path")
        if require_file and (not resolved.is_file()):
            raise WorkspaceError(f"artifact does not exist: {relative}")
        return resolved


class ScopedArtifact:
    """Immutable reference whose concrete path is valid only in its scope."""

    __slots__ = ("_workspace", "relative_path")

    def __init__(self, workspace: MediaWorkspace, relative_path: str) -> None:
        if not isinstance(workspace, MediaWorkspace):
            raise TypeError("workspace must be a MediaWorkspace")
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("relative_path is required")
        self._workspace = workspace
        self.relative_path = relative_path

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise AttributeError("ScopedArtifact is immutable")
        object.__setattr__(self, name, value)

    @property
    def path(self) -> Path:
        return self._workspace.resolve(self.relative_path, require_file=True)

    @property
    def name(self) -> str:
        return Path(self.relative_path).name

    @property
    def workspace(self) -> MediaWorkspace:
        return self._workspace

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def __repr__(self) -> str:
        return f"ScopedArtifact(relative_path={self.relative_path!r})"

    def __hash__(self) -> int:
        return hash((id(self._workspace), self.relative_path))

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, ScopedArtifact)
            and self._workspace is other._workspace
            and self.relative_path == other.relative_path
        )


def _safe_component(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("workspace directory name is invalid")
    path = PurePath(value)
    if len(path.parts) != 1 or path.parts[0] in (".", "..") or path.is_absolute():
        raise WorkspaceError("workspace directory name must be one safe component")
    return path.parts[0]


__all__ = ["MediaWorkspace", "ScopedArtifact"]
