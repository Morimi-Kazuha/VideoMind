"""MinIO object and chunk stores for the R2 upload boundary."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dovideo.application import MAX_CHUNK_BYTES
from dovideo.application.ports.ingest import ChunkObjectPort
from dovideo.application.ports.media import ObjectStoragePort


_SAFE_OBJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}$")
_CHUNK_NAME = re.compile(r"^chunk-uploads/[0-9a-f-]{36}/part-([0-9]+)$")


class MinioStorageError(RuntimeError):
    """Safe MinIO boundary error without endpoint or credential details."""


class MinioObjectStorage(ObjectStoragePort):
    """Provider-neutral object port backed by a synchronous MinIO client."""

    def __init__(
        self,
        client: Any,
        *,
        bucket: str = "media",
        workspace_parent: str | os.PathLike[str] | None = None,
    ) -> None:
        if not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,62}", bucket):
            raise ValueError("MinIO bucket name is invalid")
        self.client = client
        self.bucket = bucket
        self.workspace_parent = None if workspace_parent is None else Path(workspace_parent)

    async def ensure_bucket(self) -> None:
        try:
            exists = await asyncio.to_thread(self.client.bucket_exists, self.bucket)
            if not exists:
                await asyncio.to_thread(self.client.make_bucket, self.bucket)
        except Exception as exc:
            raise MinioStorageError("MinIO bucket initialization failed") from exc

    async def put_object(
        self,
        chunks: AsyncIterable[bytes],
        *,
        object_name: str,
        content_type: str | None = None,
    ) -> str:
        name = _validate_object_name(object_name)
        path: Path | None = None
        try:
            path = await self._materialize(chunks)
            size = path.stat().st_size
            await self._put_path(path, name, size, content_type)
            return f"minio://{self.bucket}/{name}"
        except asyncio.CancelledError:
            raise
        except MinioStorageError:
            raise
        except Exception as exc:
            raise MinioStorageError("MinIO object upload failed") from exc
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    async def delete_object(self, source: str) -> None:
        bucket, name = _parse_source(source, self.bucket)
        try:
            await asyncio.to_thread(self.client.remove_object, bucket, name)
        except Exception as exc:
            raise MinioStorageError("MinIO object deletion failed") from exc

    async def _materialize(self, chunks: AsyncIterable[bytes]) -> Path:
        directory = self.workspace_parent
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix="dovideo-r2-",
            suffix=".object",
            dir=None if directory is None else str(directory),
            delete=False,
        )
        path = Path(handle.name)
        try:
            handle.close()
            async for piece in chunks:
                if not isinstance(piece, (bytes, bytearray, memoryview)):
                    raise TypeError("object chunks must be bytes")
                await asyncio.to_thread(_append_bytes, path, bytes(piece))
            return path
        except BaseException:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    async def _put_path(
        self,
        path: Path,
        object_name: str,
        size: int,
        content_type: str | None,
    ) -> None:
        def put() -> None:
            with path.open("rb") as stream:
                kwargs: dict[str, Any] = {
                    "bucket_name": self.bucket,
                    "object_name": object_name,
                    "data": stream,
                    "length": size,
                    "part_size": 10 * 1024 * 1024,
                }
                if content_type:
                    kwargs["content_type"] = content_type
                self.client.put_object(**kwargs)

        try:
            await asyncio.to_thread(put)
        except Exception as exc:
            raise MinioStorageError("MinIO object upload failed") from exc


class MinioChunkObjectStore(ChunkObjectPort):
    """ChunkObjectPort using the same MinIO bucket and bounded object names."""

    def __init__(self, storage: MinioObjectStorage) -> None:
        self.storage = storage
        self.client = storage.client
        self.bucket = storage.bucket

    async def put_chunk(
        self,
        object_name: str,
        chunks: AsyncIterable[bytes],
        *,
        size: int,
    ) -> None:
        if size <= 0 or size > MAX_CHUNK_BYTES:
            raise ValueError("chunk size is outside the R2 limit")
        name = _validate_object_name(object_name)
        path = await self.storage._materialize(chunks)
        try:
            actual = path.stat().st_size
            if actual != size or actual > MAX_CHUNK_BYTES:
                raise ValueError("chunk byte count does not match declared size")
            await self.storage._put_path(path, name, actual, "application/octet-stream")
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def read_chunk(self, object_name: str) -> AsyncIterator[bytes]:
        name = _validate_object_name(object_name)

        async def stream() -> AsyncIterator[bytes]:
            response = None
            try:
                response = await asyncio.to_thread(self.client.get_object, self.bucket, name)
                while True:
                    piece = await asyncio.to_thread(response.read, 1024 * 1024)
                    if not piece:
                        return
                    yield bytes(piece)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise MinioStorageError("MinIO chunk read failed") from exc
            finally:
                if response is not None:
                    close = getattr(response, "close", None)
                    release = getattr(response, "release_conn", None)
                    if callable(close):
                        await asyncio.to_thread(close)
                    if callable(release):
                        await asyncio.to_thread(release)

        return stream()

    async def delete_chunk(self, object_name: str) -> None:
        name = _validate_object_name(object_name)
        try:
            await asyncio.to_thread(self.client.remove_object, self.bucket, name)
        except Exception as exc:
            raise MinioStorageError("MinIO chunk deletion failed") from exc

    async def list_chunks(self, upload_id: str) -> tuple[int, ...]:
        prefix = f"chunk-uploads/{upload_id}/"

        def list_names() -> tuple[int, ...]:
            values: list[int] = []
            for item in self.client.list_objects(self.bucket, prefix=prefix, recursive=True):
                name = str(getattr(item, "object_name", ""))
                match = _CHUNK_NAME.fullmatch(name)
                if match:
                    values.append(int(match.group(1)))
            return tuple(sorted(set(values)))

        try:
            return await asyncio.to_thread(list_names)
        except Exception as exc:
            raise MinioStorageError("MinIO chunk listing failed") from exc


def _validate_object_name(value: str) -> str:
    if not isinstance(value, str) or not value or ".." in value or not _SAFE_OBJECT.fullmatch(value):
        raise ValueError("object name is invalid")
    return value


def _append_bytes(path: Path, value: bytes) -> None:
    with path.open("ab") as stream:
        stream.write(value)


def _parse_source(source: str, default_bucket: str) -> tuple[str, str]:
    if not isinstance(source, str) or not source.strip():
        raise ValueError("object source is required")
    text = source.strip()
    if text.startswith("minio://"):
        parsed = urlsplit(text)
        bucket = parsed.netloc or default_bucket
        name = parsed.path.lstrip("/")
    else:
        bucket = default_bucket
        name = text.lstrip("/")
    if bucket != default_bucket:
        raise ValueError("object source bucket is not owned by this adapter")
    return bucket, _validate_object_name(name)


MinIOObjectStorage = MinioObjectStorage
MinIOChunkObjectStore = MinioChunkObjectStore


__all__ = [
    "MinIOChunkObjectStore",
    "MinIOObjectStorage",
    "MinioChunkObjectStore",
    "MinioObjectStorage",
    "MinioStorageError",
]
