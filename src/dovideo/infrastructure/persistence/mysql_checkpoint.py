"""DB-API MySQL adapter for the Java ``agent_checkpoints`` table.

The module intentionally imports no MySQL driver.  A composition root injects
either a DB-API connection factory (the preferred production shape) or a
single connection for small offline fakes.  Factory-created connections are
opened and closed for each operation, while a directly injected connection is
serialized with a lock and remains owned by its caller.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any, TypeVar

from dovideo.domain import TaskStage

from .errors import (
    CheckpointDeleteError,
    CheckpointError,
    CheckpointReadError,
    CheckpointWriteError,
)
from .models import CheckpointRecord


T = TypeVar("T")
ConnectionFactory = Callable[[], Any]


def _stage_value(stage: TaskStage | str | None) -> str:
    if isinstance(stage, TaskStage):
        return stage.value
    return "" if stage is None else str(stage)


def _payload_value(payload: Any) -> str | None:
    if payload is None:
        return None
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode("utf-8")
    return str(payload)


def _row_value(row: Any, name: str, index: int, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        pass
    return getattr(row, name, row[index] if isinstance(row, (tuple, list)) and len(row) > index else default)


def _record(row: Any) -> CheckpointRecord:
    media_id = _row_value(row, "media_id", 0)
    checkpoint_name = _row_value(row, "checkpoint_key", 1)
    if checkpoint_name is None:
        checkpoint_name = _row_value(row, "checkpoint_name", 1)
    stage = _row_value(row, "stage", 2, "")
    payload = _row_value(row, "payload", 3)
    updated_at = _row_value(row, "updated_at", 4)
    if isinstance(updated_at, datetime):
        updated_at = updated_at.isoformat()
    if media_id is None or checkpoint_name is None:
        raise TypeError("invalid checkpoint row")
    return CheckpointRecord(
        media_id=int(media_id),
        checkpoint_name=str(checkpoint_name),
        stage=_stage_value(stage),
        payload=_payload_value(payload),
        updated_at=None if updated_at is None else str(updated_at),
    )


class MySqlCheckpointStore:
    """Transactional DB-API implementation of ``DurableCheckpointStore``."""

    def __init__(
        self,
        connection_factory: ConnectionFactory | None = None,
        *,
        factory: ConnectionFactory | None = None,
        connect: ConnectionFactory | None = None,
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
            self._connection_factory: ConnectionFactory = resolved
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

    def _execute(
        self,
        operation: Callable[[Any], T],
        *,
        write: bool,
    ) -> T:
        """Run one operation and classify DB-API failures without secrets."""

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
            except CheckpointError:
                raise
            except Exception as exc:
                if write and connection is not None:
                    try:
                        connection.rollback()
                    except Exception:
                        pass
                error_type = CheckpointWriteError if write else CheckpointReadError
                if operation.__name__.startswith("delete"):
                    error_type = CheckpointDeleteError
                raise error_type("MySQL Agent Checkpoint operation failed") from exc
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

    def read(self, media_id: int, checkpoint_name: str) -> CheckpointRecord | None:
        def read_one(cursor: Any) -> CheckpointRecord | None:
            cursor.execute(
                """
                SELECT media_id, checkpoint_key, stage, payload, updated_at
                FROM agent_checkpoints
                WHERE media_id = %s AND checkpoint_key = %s
                """,
                (int(media_id), str(checkpoint_name)),
            )
            row = cursor.fetchone()
            return None if row is None else _record(row)

        return self._execute(read_one, write=False)

    def upsert(
        self,
        media_id: int,
        checkpoint_name: str,
        stage: TaskStage | str | None,
        payload: str | None,
    ) -> None:
        def put(cursor: Any) -> None:
            cursor.execute(
                """
                INSERT INTO agent_checkpoints
                    (media_id, checkpoint_key, stage, payload)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    stage = VALUES(stage),
                    payload = VALUES(payload),
                    updated_at = CURRENT_TIMESTAMP(3)
                """,
                (int(media_id), str(checkpoint_name), _stage_value(stage), payload),
            )

        self._execute(put, write=True)

    def upsert_many(self, records: Iterable[CheckpointRecord]) -> None:
        values = tuple(records)

        def put_many(cursor: Any) -> None:
            statement = """
                INSERT INTO agent_checkpoints
                    (media_id, checkpoint_key, stage, payload)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    stage = VALUES(stage),
                    payload = VALUES(payload),
                    updated_at = CURRENT_TIMESTAMP(3)
            """
            for item in values:
                cursor.execute(
                    statement,
                    (
                        int(item.media_id),
                        str(item.checkpoint_name),
                        _stage_value(item.stage),
                        item.payload,
                    ),
                )

        self._execute(put_many, write=True)

    def delete(self, media_id: int, checkpoint_name: str) -> None:
        def delete_one(cursor: Any) -> None:
            cursor.execute(
                "DELETE FROM agent_checkpoints WHERE media_id = %s AND checkpoint_key = %s",
                (int(media_id), str(checkpoint_name)),
            )

        self._execute(delete_one, write=True)

    def delete_prefix(self, media_id: int, checkpoint_prefix: str) -> None:
        def delete_prefix_rows(cursor: Any) -> None:
            cursor.execute(
                """
                DELETE FROM agent_checkpoints
                WHERE media_id = %s
                  AND checkpoint_key LIKE CONCAT(%s, '%')
                """,
                (int(media_id), str(checkpoint_prefix)),
            )

        self._execute(delete_prefix_rows, write=True)

    def delete_media(self, media_id: int) -> None:
        def delete_media_rows(cursor: Any) -> None:
            cursor.execute(
                "DELETE FROM agent_checkpoints WHERE media_id = %s",
                (int(media_id),),
            )

        self._execute(delete_media_rows, write=True)

    def records(self, media_id: int | None = None) -> tuple[CheckpointRecord, ...]:
        def read_rows(cursor: Any) -> tuple[CheckpointRecord, ...]:
            if media_id is None:
                cursor.execute(
                    """
                    SELECT media_id, checkpoint_key, stage, payload, updated_at
                    FROM agent_checkpoints
                    ORDER BY media_id, checkpoint_key
                    """
                )
            else:
                cursor.execute(
                    """
                    SELECT media_id, checkpoint_key, stage, payload, updated_at
                    FROM agent_checkpoints
                    WHERE media_id = %s
                    ORDER BY checkpoint_key
                    """,
                    (int(media_id),),
                )
            return tuple(_record(row) for row in cursor.fetchall())

        return self._execute(read_rows, write=False)

    # Mapper/repository migration spellings.
    find = read
    get = read
    save = upsert
    remove = delete
    delete_by_prefix = delete_prefix
    delete_by_media_id = delete_media
    delete_media_id = delete_media

    def find_payload(self, media_id: int, checkpoint_name: str) -> str | None:
        row = self.read(media_id, checkpoint_name)
        return None if row is None else row.payload

    def find_stage(self, media_id: int, checkpoint_name: str) -> str | None:
        row = self.read(media_id, checkpoint_name)
        return None if row is None else row.stage


DbApiMySqlCheckpointStore = MySqlCheckpointStore
MySQLCheckpointStore = MySqlCheckpointStore
MySqlDurableCheckpointStore = MySqlCheckpointStore
MySQLDurableCheckpointStore = MySqlCheckpointStore


__all__ = [
    "DbApiMySqlCheckpointStore",
    "MySQLCheckpointStore",
    "MySQLDurableCheckpointStore",
    "MySqlCheckpointStore",
    "MySqlDurableCheckpointStore",
]
