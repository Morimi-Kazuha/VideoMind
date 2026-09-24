"""Durable media-file repositories for local SQLite and DB-API MySQL.

The Java V1 ``media_files`` columns are used verbatim by the MySQL adapter:
``file_path`` maps to ``MediaRecord.source`` and ``upload_time`` maps to
``MediaRecord.uploaded_at``.  ``MediaRecord.content_type`` is a Python-side
ingest hint and is deliberately not sent to the V1 MySQL schema.  The local
SQLite adapter adds a nullable ``content_type`` column so offline reopen tests
can round-trip that hint; when an existing V1 SQLite table lacks the extension
the value is simply not persisted.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from dovideo.application import MediaRecord, MediaStatus
from dovideo.application.ports.media import MediaMetadataPort
from dovideo.application.ports.ingest import MediaRecordPort
from dovideo.application.value_objects import MediaRef

from .errors import MediaDeleteError, MediaReadError, MediaWriteError


T = TypeVar("T")
ConnectionFactory = Callable[[], Any]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return _as_utc(datetime.fromisoformat(text))
    raise TypeError("media upload time is invalid")


def _row_value(row: Any, name: str, index: int, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        pass
    if hasattr(row, name):
        return getattr(row, name)
    if isinstance(row, (tuple, list)) and len(row) > index:
        return row[index]
    return default


def _media_from_row(row: Any) -> MediaRecord:
    media_id = _row_value(row, "id", 0)
    user_id = _row_value(row, "user_id", 1)
    filename = _row_value(row, "filename", 2)
    status = _row_value(row, "status", 3)
    source = _row_value(row, "file_path", 4)
    content_hash = _row_value(row, "content_hash", 5)
    upload_time = _row_value(row, "upload_time", 6)
    content_type = _row_value(row, "content_type", 7)
    if media_id is None or user_id is None or filename is None or source is None:
        raise TypeError("media row is missing required fields")
    return MediaRecord(
        media_id=int(media_id),
        user_id=int(user_id),
        filename=str(filename),
        source=str(source),
        content_hash=None if content_hash is None else str(content_hash),
        status=MediaStatus.COMPLETED if status is None else str(status),
        uploaded_at=_parse_timestamp(upload_time),
        content_type=None if content_type is None else str(content_type),
    )


def _validate_record(record: MediaRecord) -> MediaRecord:
    if not isinstance(record, MediaRecord):
        raise TypeError("media record is required")
    return record


class SqliteMediaRecordRepository(MediaRecordPort, MediaMetadataPort):
    """Thread-safe standard-library SQLite implementation of media records."""

    def __init__(
        self,
        path: str | Path | sqlite3.Connection | None = None,
        connection: sqlite3.Connection | None = None,
        *,
        db_path: str | Path | None = None,
        timeout: float = 5.0,
        initialize: bool = True,
    ) -> None:
        if isinstance(path, sqlite3.Connection):
            if connection is not None:
                raise ValueError("path and connection are mutually exclusive")
            connection = path
            path = None
        if path is not None and connection is not None:
            raise ValueError("path and connection are mutually exclusive")
        if path is None and db_path is not None:
            path = db_path
        if path is None and connection is None:
            path = ":memory:"
        self._owns_connection = connection is None
        if connection is None:
            path_value = str(path)
            if path_value != ":memory:":
                parent = Path(path_value).expanduser().parent
                if str(parent) not in ("", "."):
                    parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                path_value,
                timeout=timeout,
                check_same_thread=False,
            )
        self.connection = connection
        self._lock = threading.RLock()
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
        except sqlite3.Error:
            pass
        self._content_type_supported = False
        if initialize:
            self._initialize()

    def _initialize(self) -> None:
        try:
            with self._lock, self.connection:
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS media_files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        filename TEXT NOT NULL,
                        status TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        content_hash TEXT NULL,
                        ai_summary TEXT NULL,
                        transcript_text TEXT NULL,
                        cover_url TEXT NULL,
                        upload_time TEXT NOT NULL,
                        content_type TEXT NULL
                    )
                    """
                )
                columns = self.connection.execute(
                    "PRAGMA table_info(media_files)"
                ).fetchall()
                self._content_type_supported = any(
                    str(_row_value(column, "name", 1, "")) == "content_type"
                    for column in columns
                )
        except Exception as exc:
            raise MediaWriteError("初始化 media_files 表失败") from exc

    def _columns(self) -> tuple[str, ...]:
        base = (
            "user_id",
            "filename",
            "status",
            "file_path",
            "content_hash",
            "upload_time",
        )
        return base + (("content_type",) if self._content_type_supported else ())

    def _values(self, record: MediaRecord) -> tuple[Any, ...]:
        values: tuple[Any, ...] = (
            record.user_id,
            record.filename,
            record.status.value,
            record.source,
            record.content_hash,
            _timestamp(record.uploaded_at),
        )
        if self._content_type_supported:
            values += (record.content_type,)
        return values

    def _save_sync(self, record: MediaRecord) -> MediaRecord:
        record = _validate_record(record)
        with self._lock:
            cursor = None
            try:
                with self.connection:
                    cursor = self.connection.cursor()
                    columns = self._columns()
                    placeholders = ", ".join("?" for _ in columns)
                    assignments = ", ".join(
                        f"{column} = excluded.{column}"
                        for column in columns
                    )
                    if record.media_id is None:
                        cursor.execute(
                            f"INSERT INTO media_files ({', '.join(columns)}) "
                            f"VALUES ({placeholders})",
                            self._values(record),
                        )
                        media_id = cursor.lastrowid
                    else:
                        explicit_columns = ("id",) + columns
                        explicit_values = (record.media_id,) + self._values(record)
                        explicit_placeholders = ", ".join("?" for _ in explicit_columns)
                        explicit_assignments = ", ".join(
                            f"{column} = excluded.{column}" for column in columns
                        )
                        cursor.execute(
                            f"INSERT INTO media_files ({', '.join(explicit_columns)}) "
                            f"VALUES ({explicit_placeholders}) "
                            f"ON CONFLICT(id) DO UPDATE SET {explicit_assignments}",
                            explicit_values,
                        )
                        media_id = record.media_id
                    if media_id is None:
                        raise RuntimeError("SQLite did not return a media id")
                return replace(record, media_id=int(media_id))
            except Exception as exc:
                raise MediaWriteError("保存 media record 失败") from exc
            finally:
                if cursor is not None:
                    try:
                        cursor.close()
                    except Exception:
                        pass

    def _get_sync(self, media_id: int) -> MediaRecord | None:
        with self._lock:
            cursor = None
            try:
                cursor = self.connection.cursor()
                content_type_column = ", content_type" if self._content_type_supported else ""
                cursor.execute(
                    f"""
                    SELECT id, user_id, filename, status, file_path,
                           content_hash, upload_time{content_type_column}
                    FROM media_files WHERE id = ?
                    """,
                    (int(media_id),),
                )
                row = cursor.fetchone()
                return None if row is None else _media_from_row(row)
            except Exception as exc:
                raise MediaReadError("读取 media record 失败") from exc
            finally:
                if cursor is not None:
                    try:
                        cursor.close()
                    except Exception:
                        pass

    def _delete_sync(self, media_id: int) -> None:
        with self._lock:
            cursor = None
            try:
                with self.connection:
                    cursor = self.connection.cursor()
                    cursor.execute("DELETE FROM media_files WHERE id = ?", (int(media_id),))
            except Exception as exc:
                raise MediaDeleteError("删除 media record 失败") from exc
            finally:
                if cursor is not None:
                    try:
                        cursor.close()
                    except Exception:
                        pass

    async def save(self, record: MediaRecord) -> MediaRecord:
        return await asyncio.to_thread(self._save_sync, record)

    async def get(self, media_id: int) -> MediaRecord | None:
        return await asyncio.to_thread(self._get_sync, media_id)

    async def delete(self, media_id: int) -> None:
        await asyncio.to_thread(self._delete_sync, media_id)

    async def get_media(self, media_id: int) -> MediaRef | None:
        record = await self.get(media_id)
        return None if record is None else record.to_ref()

    # Synchronous names are useful to migration/CLI composition roots while
    # the public MediaRecordPort methods above remain async.
    save_sync = _save_sync
    get_sync = _get_sync
    delete_sync = _delete_sync
    save_record = _save_sync
    get_record = _get_sync
    delete_record = _delete_sync

    def close(self) -> None:
        if self._owns_connection:
            with self._lock:
                self.connection.close()

    def __enter__(self) -> "SqliteMediaRecordRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class MySqlMediaRecordRepository(MediaRecordPort, MediaMetadataPort):
    """DB-API MySQL media repository using only Java V1 columns."""

    _SELECT = """
        SELECT id, user_id, filename, status, file_path,
               content_hash, upload_time
        FROM media_files WHERE id = %s
    """

    def __init__(
        self,
        connection_factory: Callable[[], Any] | None = None,
        *,
        factory: Callable[[], Any] | None = None,
        connect: Callable[[], Any] | None = None,
        connection: Any | None = None,
        client: Any | None = None,
    ) -> None:
        candidates = [value for value in (connection_factory, factory, connect) if value is not None]
        if len(candidates) > 1:
            raise ValueError("only one MySQL connection factory may be supplied")
        if connection is not None and client is not None:
            raise ValueError("connection and client are mutually exclusive")
        injected = connection if connection is not None else client
        if candidates and injected is not None:
            raise ValueError("connection factory and connection are mutually exclusive")
        if candidates:
            resolved = candidates[0]
            if not callable(resolved):
                raise TypeError("MySQL connection factory must be callable")
            self._connection_factory = resolved
            self._owns_connection = True
            self._connection = None
        elif injected is not None:
            if callable(injected) and not hasattr(injected, "cursor"):
                self._connection_factory = injected
                self._owns_connection = True
                self._connection = None
            else:
                if not hasattr(injected, "cursor"):
                    raise TypeError("MySQL connection must expose cursor()")
                self._connection_factory = lambda: injected
                self._owns_connection = False
                self._connection = injected
        else:
            raise ValueError("a MySQL connection factory or connection is required")
        self._lock = threading.RLock()

    def _execute(self, operation: Callable[[Any], T], *, write: bool) -> T:
        with self._lock:
            connection = None
            cursor = None
            try:
                connection = self._connection_factory()
                cursor = connection.cursor()
                result = operation(cursor)
                if write:
                    connection.commit()
                return result
            except Exception as exc:
                if write and connection is not None:
                    try:
                        connection.rollback()
                    except Exception:
                        pass
                error_type = MediaWriteError if write else MediaReadError
                if operation.__name__.startswith("delete"):
                    error_type = MediaDeleteError
                raise error_type("MySQL media record operation failed") from exc
            finally:
                if cursor is not None:
                    try:
                        cursor.close()
                    except Exception:
                        pass
                if self._owns_connection and connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass

    @staticmethod
    def _values(record: MediaRecord) -> tuple[Any, ...]:
        return (
            record.user_id,
            record.filename,
            record.status.value,
            record.source,
            record.content_hash,
            _as_utc(record.uploaded_at),
        )

    def _save_sync(self, record: MediaRecord) -> MediaRecord:
        record = _validate_record(record)

        def save_row(cursor: Any) -> MediaRecord:
            if record.media_id is None:
                cursor.execute(
                    """
                    INSERT INTO media_files
                        (user_id, filename, status, file_path, content_hash, upload_time)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    self._values(record),
                )
                media_id = getattr(cursor, "lastrowid", None)
                if media_id is None:
                    cursor.execute("SELECT LAST_INSERT_ID()")
                    row = cursor.fetchone()
                    media_id = _row_value(row, "id", 0) if row is not None else None
                if media_id is None:
                    raise RuntimeError("MySQL did not return a media id")
            else:
                cursor.execute(
                    """
                    INSERT INTO media_files
                        (id, user_id, filename, status, file_path, content_hash, upload_time)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        user_id = VALUES(user_id),
                        filename = VALUES(filename),
                        status = VALUES(status),
                        file_path = VALUES(file_path),
                        content_hash = VALUES(content_hash),
                        upload_time = VALUES(upload_time)
                    """,
                    (record.media_id, *self._values(record)),
                )
                media_id = record.media_id
            return replace(record, media_id=int(media_id))

        return self._execute(save_row, write=True)

    def _get_sync(self, media_id: int) -> MediaRecord | None:
        def get_row(cursor: Any) -> MediaRecord | None:
            cursor.execute(self._SELECT, (int(media_id),))
            row = cursor.fetchone()
            return None if row is None else _media_from_row(row)

        return self._execute(get_row, write=False)

    def _delete_sync(self, media_id: int) -> None:
        def delete_row(cursor: Any) -> None:
            cursor.execute("DELETE FROM media_files WHERE id = %s", (int(media_id),))

        self._execute(delete_row, write=True)

    async def save(self, record: MediaRecord) -> MediaRecord:
        return await asyncio.to_thread(self._save_sync, record)

    async def get(self, media_id: int) -> MediaRecord | None:
        return await asyncio.to_thread(self._get_sync, media_id)

    async def delete(self, media_id: int) -> None:
        await asyncio.to_thread(self._delete_sync, media_id)

    async def get_media(self, media_id: int) -> MediaRef | None:
        record = await self.get(media_id)
        return None if record is None else record.to_ref()

    save_sync = _save_sync
    get_sync = _get_sync
    delete_sync = _delete_sync
    save_record = _save_sync
    get_record = _get_sync
    delete_record = _delete_sync


SQLiteMediaRecordRepository = SqliteMediaRecordRepository
SqliteMediaRepository = SqliteMediaRecordRepository
MySQLMediaRecordRepository = MySqlMediaRecordRepository
DbApiMySqlMediaRecordRepository = MySqlMediaRecordRepository
DbApiMySQLMediaRecordRepository = MySqlMediaRecordRepository


__all__ = [
    "DbApiMySQLMediaRecordRepository",
    "DbApiMySqlMediaRecordRepository",
    "MySQLMediaRecordRepository",
    "MySqlMediaRecordRepository",
    "SQLiteMediaRecordRepository",
    "SqliteMediaRecordRepository",
    "SqliteMediaRepository",
]
