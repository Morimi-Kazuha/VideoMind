"""Standard-library SQLite durable checkpoint adapter."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from dovideo.domain import TaskStage

from .errors import CheckpointDeleteError, CheckpointReadError, CheckpointWriteError
from .models import CheckpointRecord


def _stage_value(stage: TaskStage | str | None) -> str:
    if isinstance(stage, TaskStage):
        return stage.value
    return "" if stage is None else str(stage)


class SqliteCheckpointStore:
    """Transactional, thread-safe implementation of the durable store.

    ``path`` and ``connection`` are mutually exclusive injection points.  No
    default file is created in a user/system directory: omitted arguments use
    an in-memory SQLite database.  An injected connection remains owned by
    the caller and is not closed by :meth:`close`.
    """

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
            # Some injected SQLite-compatible connections do not support the
            # pragma; normal operations still provide lock/transaction safety.
            pass
        if initialize:
            self._initialize()

    def _initialize(self) -> None:
        try:
            with self._lock, self.connection:
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_checkpoints (
                        media_id INTEGER NOT NULL,
                        checkpoint_key TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        payload TEXT NULL,
                        updated_at TEXT NOT NULL DEFAULT
                            (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                        PRIMARY KEY (media_id, checkpoint_key)
                    )
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_agent_checkpoint_updated
                    ON agent_checkpoints(updated_at)
                    """
                )
        except Exception as exc:
            raise CheckpointWriteError("初始化 Agent Checkpoint 表失败") from exc

    @staticmethod
    def _record_from_row(row: sqlite3.Row | None) -> CheckpointRecord | None:
        if row is None:
            return None
        return CheckpointRecord(
            media_id=int(row["media_id"]),
            checkpoint_name=str(row["checkpoint_key"]),
            stage=str(row["stage"]),
            payload=row["payload"],
            updated_at=row["updated_at"],
        )

    def read(self, media_id: int, checkpoint_name: str) -> CheckpointRecord | None:
        try:
            with self._lock:
                row = self.connection.execute(
                    """
                    SELECT media_id, checkpoint_key, stage, payload, updated_at
                    FROM agent_checkpoints
                    WHERE media_id = ? AND checkpoint_key = ?
                    """,
                    (int(media_id), str(checkpoint_name)),
                ).fetchone()
            return self._record_from_row(row)
        except Exception as exc:
            if isinstance(exc, CheckpointReadError):
                raise
            raise CheckpointReadError(
                f"读取 Agent Checkpoint 失败: {checkpoint_name}"
            ) from exc

    def upsert(
        self,
        media_id: int,
        checkpoint_name: str,
        stage: TaskStage | str | None,
        payload: str | None,
    ) -> None:
        self.upsert_many(
            [CheckpointRecord(int(media_id), str(checkpoint_name), _stage_value(stage), payload)]
        )

    def upsert_many(self, records: Iterable[CheckpointRecord]) -> None:
        values = tuple(records)
        try:
            with self._lock, self.connection:
                self.connection.executemany(
                    """
                    INSERT INTO agent_checkpoints
                        (media_id, checkpoint_key, stage, payload, updated_at)
                    VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    ON CONFLICT(media_id, checkpoint_key) DO UPDATE SET
                        stage = excluded.stage,
                        payload = excluded.payload,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    """,
                    tuple(
                        (
                            int(record.media_id),
                            str(record.checkpoint_name),
                            _stage_value(record.stage),
                            record.payload,
                        )
                        for record in values
                    ),
                )
        except Exception as exc:
            if isinstance(exc, CheckpointWriteError):
                raise
            raise CheckpointWriteError("保存 Agent Checkpoint 失败") from exc

    def delete(self, media_id: int, checkpoint_name: str) -> None:
        try:
            with self._lock, self.connection:
                self.connection.execute(
                    "DELETE FROM agent_checkpoints WHERE media_id = ? AND checkpoint_key = ?",
                    (int(media_id), str(checkpoint_name)),
                )
        except Exception as exc:
            raise CheckpointDeleteError(
                f"删除 Agent Checkpoint 失败: {checkpoint_name}"
            ) from exc

    def delete_prefix(self, media_id: int, checkpoint_prefix: str) -> None:
        try:
            with self._lock, self.connection:
                rows = self.connection.execute(
                    "SELECT checkpoint_key FROM agent_checkpoints WHERE media_id = ?",
                    (int(media_id),),
                ).fetchall()
                names = tuple(
                    str(row["checkpoint_key"])
                    for row in rows
                    if str(row["checkpoint_key"]).startswith(str(checkpoint_prefix))
                )
                self.connection.executemany(
                    "DELETE FROM agent_checkpoints WHERE media_id = ? AND checkpoint_key = ?",
                    ((int(media_id), name) for name in names),
                )
        except Exception as exc:
            raise CheckpointDeleteError(
                f"删除 Agent Checkpoint 前缀失败: {checkpoint_prefix}"
            ) from exc

    def delete_media(self, media_id: int) -> None:
        try:
            with self._lock, self.connection:
                self.connection.execute(
                    "DELETE FROM agent_checkpoints WHERE media_id = ?",
                    (int(media_id),),
                )
        except Exception as exc:
            raise CheckpointDeleteError(
                f"删除 media Agent Checkpoint 失败: {media_id}"
            ) from exc

    # Java mapper/service spelling aliases.
    find = read
    get = read
    find_payload_record = read
    save = upsert
    remove = delete
    delete_by_prefix = delete_prefix
    delete_by_media_id = delete_media

    def records(self, media_id: int | None = None) -> tuple[CheckpointRecord, ...]:
        try:
            with self._lock:
                if media_id is None:
                    rows = self.connection.execute(
                        """
                        SELECT media_id, checkpoint_key, stage, payload, updated_at
                        FROM agent_checkpoints ORDER BY media_id, checkpoint_key
                        """
                    ).fetchall()
                else:
                    rows = self.connection.execute(
                        """
                        SELECT media_id, checkpoint_key, stage, payload, updated_at
                        FROM agent_checkpoints WHERE media_id = ?
                        ORDER BY checkpoint_key
                        """,
                        (int(media_id),),
                    ).fetchall()
            return tuple(self._record_from_row(row) for row in rows if row is not None)
        except Exception as exc:
            raise CheckpointReadError("读取 Agent Checkpoint 列表失败") from exc

    def close(self) -> None:
        if self._owns_connection:
            with self._lock:
                self.connection.close()

    def __enter__(self) -> "SqliteCheckpointStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


SQLiteCheckpointStore = SqliteCheckpointStore
SQLiteDurableCheckpointStore = SqliteCheckpointStore
SqliteDurableCheckpointStore = SqliteCheckpointStore
InMemoryDurableCheckpointStore = SqliteCheckpointStore
InMemoryCheckpointStore = SqliteCheckpointStore


__all__ = [
    "InMemoryDurableCheckpointStore",
    "InMemoryCheckpointStore",
    "SQLiteCheckpointStore",
    "SQLiteDurableCheckpointStore",
    "SqliteCheckpointStore",
    "SqliteDurableCheckpointStore",
]
