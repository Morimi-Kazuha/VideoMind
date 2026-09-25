"""SQLAlchemy 2.x durable adapters for the R2 production profile.

The application only sees ``MediaRecord``, ``MediaRef`` and
``CheckpointRecord`` values.  SQLAlchemy sessions and mapped rows stop at
this module, which keeps the accepted application ports independent from the
database driver and prevents ORM objects from leaking into API code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    inspect,
    select,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from dovideo.application import MediaRecord, MediaStatus
from dovideo.application.execution_record import (
    DurableAgentExecutionRecord,
    DurableExecutionEvent,
    ExecutionEventType,
    ExecutionRecordConflictError,
    ExecutionRecordNotFoundError,
    ExecutionRecordStatus,
    ExecutionTaskKey,
    MAX_EXECUTION_EVENT_PAYLOAD_BYTES,
    MAX_EXECUTION_EVENTS,
)
from dovideo.application.value_objects import MediaRef
from dovideo.application.value_objects import TaskKey
from dovideo.domain import AnalysisMode

from .models import CheckpointRecord


class R2DatabaseError(RuntimeError):
    """Safe database-boundary error without driver messages or credentials."""


class Base(DeclarativeBase):
    """Private SQLAlchemy metadata for the Java-compatible R2 schema."""


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    nickname: Mapped[str] = mapped_column(String(50), nullable=False)
    avatar: Mapped[str | None] = mapped_column(String(512), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="USER")


class MediaRow(Base):
    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    upload_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, default=lambda: _db_now()
    )


class CheckpointRow(Base):
    __tablename__ = "agent_checkpoints"

    media_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    checkpoint_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    payload: Mapped[str | None] = mapped_column(
        Text().with_variant(LONGTEXT(), "mysql"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, default=lambda: _db_now(), onupdate=lambda: _db_now()
    )


class FailedAnalysisTaskRow(Base):
    __tablename__ = "failed_analysis_tasks"

    # SQLite uses INTEGER PRIMARY KEY for rowid-backed autoincrement in the
    # adapter tests; production remains the existing BIGINT column.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    media_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="GENERAL")
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    user_goal: Mapped[str] = mapped_column(String(500), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error_type: Mapped[str] = mapped_column(String(128), nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="FAILED")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, default=lambda: _db_now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, default=lambda: _db_now(), onupdate=lambda: _db_now()
    )


class FailedAnalysisTaskReplayRow(Base):
    """Append-only administrative replay attempts for one historical failure."""

    __tablename__ = "failed_analysis_task_replays"
    __table_args__ = (
        UniqueConstraint(
            "failed_task_id",
            "idempotency_digest",
            name="uq_failed_task_replay_idempotency",
        ),
        Index("ix_failed_task_replay_history", "failed_task_id", "created_at"),
    )

    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    failed_task_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("failed_analysis_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Persist only a one-way digest; caller-provided idempotency values are
    # neither stored nor exposed by this adapter.
    idempotency_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="REQUESTED")
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, default=lambda: _db_now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, default=lambda: _db_now(), onupdate=lambda: _db_now()
    )


class AgentExecutionRecordRow(Base):
    """Immutable-history header; intentionally separate from checkpoints."""

    __tablename__ = "agent_execution_records"
    __table_args__ = (
        Index("ix_execution_record_task_history", "media_id", "goal", "mode", "created_at"),
    )

    execution_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    media_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    goal: Mapped[str] = mapped_column(String(500), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    media_identity: Mapped[str] = mapped_column(String(512), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(96), nullable=False)
    source_provenance_version: Mapped[str] = mapped_column(String(96), nullable=False)
    mode_profile_version: Mapped[str] = mapped_column(String(96), nullable=False)
    tool_policy_version: Mapped[str] = mapped_column(String(96), nullable=False)
    record_schema_version: Mapped[str] = mapped_column(String(96), nullable=False)
    execution_contract_version: Mapped[str] = mapped_column(String(96), nullable=False)
    worker_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    parent_execution_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    replayable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, default=lambda: _db_now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, default=lambda: _db_now(), onupdate=lambda: _db_now()
    )


class AgentExecutionEventRow(Base):
    """Append-only ordered semantic event row."""

    __tablename__ = "agent_execution_events"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "logical_event_id",
            name="uq_execution_event_logical_id",
        ),
        Index("ix_execution_event_history", "execution_id", "sequence_no"),
    )

    execution_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    sequence_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    logical_event_id: Mapped[str] = mapped_column(String(192), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_round: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(96), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, default=lambda: _db_now()
    )
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


def create_sqlalchemy_engine(
    database_url: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 10,
    pool_timeout: float = 3.0,
    pool_recycle: int = 1800,
) -> Engine:
    """Create a bounded, pre-ping SQLAlchemy engine without connecting eagerly."""

    if not isinstance(database_url, str) or not database_url.strip():
        raise ValueError("database URL is required")
    if pool_size <= 0 or max_overflow < 0 or pool_timeout <= 0:
        raise ValueError("database pool settings are invalid")
    url = database_url.strip()
    options: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_recycle": int(pool_recycle),
        "future": True,
    }
    # SQLite is useful for adapter-level tests.  Production configuration
    # rejects it before composition, but keeping this branch makes the
    # adapter deterministic without weakening the production profile.
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    else:
        options.update(
            pool_size=int(pool_size),
            max_overflow=int(max_overflow),
            pool_timeout=float(pool_timeout),
        )
    return create_engine(url, **options)


def create_schema(engine: Engine) -> None:
    """Create tables and widen legacy MySQL checkpoints for source-rich chunks."""

    Base.metadata.create_all(engine)
    if engine.dialect.name != "mysql":
        return
    with engine.begin() as connection:
        columns = inspect(connection).get_columns("agent_checkpoints")
        payload = next(column for column in columns if column["name"] == "payload")
        # MySQL reflection appends collation to str(type), so use its dialect class.
        column_type = type(payload["type"]).__name__.upper()
        if column_type == "TEXT":
            connection.exec_driver_sql(
                "ALTER TABLE agent_checkpoints MODIFY COLUMN payload LONGTEXT NULL"
            )
        elif column_type not in {"MEDIUMTEXT", "LONGTEXT"}:
            raise R2DatabaseError("MySQL checkpoint payload column has an unsupported type")


def _db_now() -> datetime:
    # MySQL TIMESTAMP is stored as UTC-naive by this boundary.  Conversion is
    # explicit on both sides so server/session timezone cannot alter values.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _idempotency_digest(value: str) -> str:
    if not isinstance(value, str) or not 16 <= len(value) <= 128:
        raise ValueError("idempotency key must contain 16 to 128 characters")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _replay_attempt_value(row: FailedAnalysisTaskReplayRow) -> FailedTaskReplayAttempt:
    return FailedTaskReplayAttempt(
        attempt_id=str(row.attempt_id),
        failed_task_id=int(row.failed_task_id),
        attempt_number=int(row.attempt_number),
        status=str(row.status),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
        error_type=None if row.error_type is None else str(row.error_type),
    )


def _session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)


class SqlAlchemyCheckpointStore:
    """Synchronous durable store consumed through ``CheckpointRepository``."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sessions = _session_factory(engine)

    def read(self, media_id: int, checkpoint_name: str) -> CheckpointRecord | None:
        with self._sessions() as session:
            row = session.get(
                CheckpointRow,
                {"media_id": int(media_id), "checkpoint_key": str(checkpoint_name)},
            )
            return None if row is None else _checkpoint_value(row)

    def upsert(
        self,
        media_id: int,
        checkpoint_name: str,
        stage: str | None,
        payload: str | None,
    ) -> None:
        self.upsert_many((CheckpointRecord(int(media_id), str(checkpoint_name), stage or "", payload),))

    def upsert_many(self, records: Iterable[CheckpointRecord]) -> None:
        values = tuple(records)
        if not values:
            return
        try:
            with self._sessions.begin() as session:
                for value in values:
                    row = session.get(
                        CheckpointRow,
                        {
                            "media_id": int(value.media_id),
                            "checkpoint_key": str(value.checkpoint_name),
                        },
                    )
                    if row is None:
                        row = CheckpointRow(
                            media_id=int(value.media_id),
                            checkpoint_key=str(value.checkpoint_name),
                        )
                        session.add(row)
                    row.stage = str(value.stage or "")
                    row.payload = value.payload
                    row.updated_at = _db_now()
        except Exception as exc:
            raise R2DatabaseError("MySQL checkpoint transaction failed") from exc

    def delete(self, media_id: int, checkpoint_name: str) -> None:
        self._delete_where(
            CheckpointRow.media_id == int(media_id),
            CheckpointRow.checkpoint_key == str(checkpoint_name),
        )

    def delete_prefix(self, media_id: int, checkpoint_prefix: str) -> None:
        self._delete_where(
            CheckpointRow.media_id == int(media_id),
            CheckpointRow.checkpoint_key.startswith(str(checkpoint_prefix)),
        )

    def delete_media(self, media_id: int) -> None:
        self._delete_where(CheckpointRow.media_id == int(media_id))

    def records(self, media_id: int | None = None) -> tuple[CheckpointRecord, ...]:
        with self._sessions() as session:
            statement = select(CheckpointRow).order_by(
                CheckpointRow.media_id, CheckpointRow.checkpoint_key
            )
            if media_id is not None:
                statement = statement.where(CheckpointRow.media_id == int(media_id))
            return tuple(_checkpoint_value(row) for row in session.scalars(statement))

    def _delete_where(self, *conditions: Any) -> None:
        try:
            with self._sessions.begin() as session:
                session.execute(delete(CheckpointRow).where(*conditions))
        except Exception as exc:
            raise R2DatabaseError("MySQL checkpoint delete failed") from exc


class SqlAlchemyExecutionRecordRepository:
    """Transactional MySQL/SQLite repository for X2-B execution history.

    The execution header and event rows are deliberately separate from
    ``agent_checkpoints``.  A locked header row serializes sequence allocation
    while the unique logical-event constraint makes crash retries idempotent.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sessions = _session_factory(engine)

    def create(self, record: DurableAgentExecutionRecord) -> DurableAgentExecutionRecord:
        if not isinstance(record, DurableAgentExecutionRecord):
            raise TypeError("execution record must be a DurableAgentExecutionRecord")
        try:
            with self._sessions.begin() as session:
                existing = session.get(AgentExecutionRecordRow, record.execution_id)
                if existing is not None:
                    loaded = self._record_value(session, existing)
                    if loaded != record:
                        raise ExecutionRecordConflictError(
                            "execution id already has different history"
                        )
                    return loaded
                session.add(_execution_record_row(record))
                session.flush()
                for event in record.events:
                    session.add(_execution_event_row(event))
                session.flush()
            return record
        except ExecutionRecordConflictError:
            raise
        except IntegrityError as exc:
            raise ExecutionRecordConflictError(
                "execution record identity conflicts with existing history"
            ) from exc
        except Exception as exc:
            raise R2DatabaseError("MySQL execution record creation failed") from exc

    def get(self, execution_id: str) -> DurableAgentExecutionRecord | None:
        try:
            with self._sessions() as session:
                row = session.get(AgentExecutionRecordRow, str(execution_id))
                return None if row is None else self._record_value(session, row)
        except Exception as exc:
            raise R2DatabaseError("MySQL execution record read failed") from exc

    def latest_for_task(self, task_key: TaskKey) -> DurableAgentExecutionRecord | None:
        if not isinstance(task_key, TaskKey):
            raise TypeError("task_key must be a TaskKey")
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(AgentExecutionRecordRow)
                    .where(
                        AgentExecutionRecordRow.media_id == int(task_key.media_id),
                        AgentExecutionRecordRow.goal == task_key.goal,
                        AgentExecutionRecordRow.mode == task_key.mode.value,
                    )
                    .order_by(
                        AgentExecutionRecordRow.created_at.desc(),
                        AgentExecutionRecordRow.execution_id.desc(),
                    )
                    .limit(1)
                )
                return None if row is None else self._record_value(session, row)
        except Exception as exc:
            raise R2DatabaseError("MySQL execution record task lookup failed") from exc

    def append_event(
        self,
        execution_id: str,
        *,
        event_type: ExecutionEventType,
        logical_event_id: str,
        agent_round: int,
        stage: str,
        payload: Mapping[str, Any],
        recorded_at: datetime,
    ) -> DurableExecutionEvent:
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(AgentExecutionRecordRow)
                    .where(AgentExecutionRecordRow.execution_id == str(execution_id))
                    .with_for_update()
                )
                if row is None:
                    raise ExecutionRecordNotFoundError("execution record was not found")
                existing_row = session.scalar(
                    select(AgentExecutionEventRow).where(
                        AgentExecutionEventRow.execution_id == str(execution_id),
                        AgentExecutionEventRow.logical_event_id == str(logical_event_id),
                    )
                )
                if existing_row is not None:
                    existing = _execution_event_value(existing_row)
                    candidate = DurableExecutionEvent(
                        execution_id=existing.execution_id,
                        sequence_no=existing.sequence_no,
                        event_type=event_type,
                        logical_event_id=logical_event_id,
                        agent_round=agent_round,
                        stage=stage,
                        recorded_at=recorded_at,
                        payload=payload,
                    )
                    if _same_execution_event(existing, candidate):
                        return existing
                    raise ExecutionRecordConflictError(
                        "logical execution event has conflicting payload"
                    )
                if str(row.status) != ExecutionRecordStatus.STARTED.value:
                    raise ExecutionRecordConflictError(
                        "cannot append to a terminal execution"
                    )
                sequence = session.scalar(
                    select(func.max(AgentExecutionEventRow.sequence_no)).where(
                        AgentExecutionEventRow.execution_id == str(execution_id)
                    )
                )
                next_sequence = int(sequence or 0) + 1
                if next_sequence > MAX_EXECUTION_EVENTS:
                    raise ExecutionRecordConflictError(
                        "execution event count exceeds its bound"
                    )
                event = DurableExecutionEvent(
                    execution_id=str(execution_id),
                    sequence_no=next_sequence,
                    event_type=event_type,
                    logical_event_id=logical_event_id,
                    agent_round=agent_round,
                    stage=stage,
                    recorded_at=recorded_at,
                    payload=payload,
                )
                session.add(_execution_event_row(event))
                session.flush()
                return event
        except (ExecutionRecordConflictError, ExecutionRecordNotFoundError):
            raise
        except IntegrityError as exc:
            # The unique logical identity is the historical conflict boundary;
            # never replace a row after an insert race.
            raise ExecutionRecordConflictError(
                "execution event append conflicts with existing history"
            ) from exc
        except Exception as exc:
            raise R2DatabaseError("MySQL execution event append failed") from exc

    def update_status(
        self,
        execution_id: str,
        status: ExecutionRecordStatus,
        *,
        completed_at: datetime | None,
        replayable: bool,
    ) -> DurableAgentExecutionRecord:
        target = ExecutionRecordStatus(status)
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(AgentExecutionRecordRow)
                    .where(AgentExecutionRecordRow.execution_id == str(execution_id))
                    .with_for_update()
                )
                if row is None:
                    raise ExecutionRecordNotFoundError("execution record was not found")
                current = ExecutionRecordStatus(str(row.status))
                if current is target:
                    return self._record_value(session, row)
                if current is not ExecutionRecordStatus.STARTED:
                    raise ExecutionRecordConflictError(
                        "terminal execution status cannot change"
                    )
                if target is ExecutionRecordStatus.COMPLETED and (
                    completed_at is None or not replayable
                ):
                    raise ExecutionRecordConflictError(
                        "completed execution must be marked replayable"
                    )
                row.status = target.value
                row.replayable = bool(replayable)
                row.completed_at = None if completed_at is None else _naive_utc(completed_at)
                row.updated_at = _db_now()
                session.flush()
                return self._record_value(session, row)
        except (ExecutionRecordConflictError, ExecutionRecordNotFoundError):
            raise
        except Exception as exc:
            raise R2DatabaseError("MySQL execution record status update failed") from exc

    @staticmethod
    def _record_value(session: Session, row: AgentExecutionRecordRow) -> DurableAgentExecutionRecord:
        event_rows = session.scalars(
            select(AgentExecutionEventRow)
            .where(AgentExecutionEventRow.execution_id == str(row.execution_id))
            .order_by(AgentExecutionEventRow.sequence_no.asc())
        )
        return DurableAgentExecutionRecord(
            recordSchemaVersion=str(row.record_schema_version),
            executionContractVersion=str(row.execution_contract_version),
            executionId=str(row.execution_id),
            taskKey=ExecutionTaskKey(
                mediaId=int(row.media_id),
                goal=str(row.goal),
                mode=AnalysisMode(str(row.mode)),
            ),
            mediaIdentity=str(row.media_identity),
            sourceRevision=str(row.source_revision),
            sourceProvenanceVersion=str(row.source_provenance_version),
            mode=AnalysisMode(str(row.mode)),
            modeProfileVersion=str(row.mode_profile_version),
            toolPolicyVersion=str(row.tool_policy_version),
            workerAttempt=None if row.worker_attempt is None else int(row.worker_attempt),
            requestId=None if row.request_id is None else str(row.request_id),
            parentExecutionId=(
                None if row.parent_execution_id is None else str(row.parent_execution_id)
            ),
            status=ExecutionRecordStatus(str(row.status)),
            replayable=bool(row.replayable),
            createdAt=_utc(row.created_at),
            completedAt=None if row.completed_at is None else _utc(row.completed_at),
            events=tuple(_execution_event_value(value) for value in event_rows),
        )


class MySQLExecutionRecordRepository(SqlAlchemyExecutionRecordRepository):
    """Production spelling for the SQLAlchemy execution-record repository."""


def _execution_record_row(record: DurableAgentExecutionRecord) -> AgentExecutionRecordRow:
    return AgentExecutionRecordRow(
        execution_id=record.execution_id,
        media_id=record.task_key.media_id,
        goal=record.task_key.goal,
        mode=record.mode.value,
        media_identity=record.media_identity,
        source_revision=record.source_revision,
        source_provenance_version=record.source_provenance_version,
        mode_profile_version=record.mode_profile_version,
        tool_policy_version=record.tool_policy_version,
        record_schema_version=record.record_schema_version,
        execution_contract_version=record.execution_contract_version,
        worker_attempt=record.worker_attempt,
        request_id=record.request_id,
        parent_execution_id=record.parent_execution_id,
        status=record.status.value,
        replayable=record.replayable,
        created_at=_naive_utc(record.created_at),
        completed_at=None if record.completed_at is None else _naive_utc(record.completed_at),
        updated_at=_db_now(),
    )


def _execution_event_row(event: DurableExecutionEvent) -> AgentExecutionEventRow:
    payload = json.dumps(
        event.payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(payload.encode("utf-8")) > MAX_EXECUTION_EVENT_PAYLOAD_BYTES:
        raise ValueError("execution event payload exceeds its bound")
    return AgentExecutionEventRow(
        execution_id=event.execution_id,
        sequence_no=event.sequence_no,
        logical_event_id=event.logical_event_id,
        event_type=event.event_type.value,
        agent_round=event.agent_round,
        stage=event.stage,
        recorded_at=_naive_utc(event.recorded_at),
        payload=payload,
        payload_digest=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )


def _execution_event_value(row: AgentExecutionEventRow) -> DurableExecutionEvent:
    try:
        payload = json.loads(str(row.payload))
    except (TypeError, ValueError) as exc:
        raise R2DatabaseError("MySQL execution event payload is malformed") from exc
    return DurableExecutionEvent(
        executionId=str(row.execution_id),
        sequenceNo=int(row.sequence_no),
        eventType=ExecutionEventType(str(row.event_type)),
        logicalEventId=str(row.logical_event_id),
        agentRound=int(row.agent_round),
        stage=str(row.stage),
        recordedAt=_utc(row.recorded_at),
        payload=payload,
    )


def _same_execution_event(
    left: DurableExecutionEvent,
    right: DurableExecutionEvent,
) -> bool:
    return (
        left.execution_id == right.execution_id
        and left.sequence_no == right.sequence_no
        and left.event_type is right.event_type
        and left.logical_event_id == right.logical_event_id
        and left.agent_round == right.agent_round
        and left.stage == right.stage
        and json.dumps(left.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        == json.dumps(right.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )


def _naive_utc(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)


class MySQLCheckpointStore(SqlAlchemyCheckpointStore):
    """Java spelling alias for the SQLAlchemy 2.x canonical store."""


def _checkpoint_value(row: CheckpointRow) -> CheckpointRecord:
    return CheckpointRecord(
        media_id=int(row.media_id),
        checkpoint_name=str(row.checkpoint_key),
        stage=str(row.stage or ""),
        payload=row.payload,
        updated_at=_utc(row.updated_at).isoformat(),
    )


class SqlAlchemyMediaRecordRepository:
    """Async application-port adapter over synchronous SQLAlchemy sessions."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sessions = _session_factory(engine)

    async def save(self, record: MediaRecord) -> MediaRecord:
        return await asyncio.to_thread(self._save_sync, record)

    async def get(self, media_id: int) -> MediaRecord | None:
        return await asyncio.to_thread(self._get_sync, media_id)

    async def delete(self, media_id: int) -> None:
        await asyncio.to_thread(self._delete_sync, media_id)

    async def list_owned(self, user_id: int) -> tuple[MediaRecord, ...]:
        return await asyncio.to_thread(self._list_owned_sync, user_id)

    async def get_media(self, media_id: int) -> MediaRef | None:
        record = await self.get(media_id)
        return None if record is None else record.to_ref()

    def _save_sync(self, record: MediaRecord) -> MediaRecord:
        try:
            with self._sessions.begin() as session:
                row = (
                    session.get(MediaRow, int(record.media_id))
                    if record.media_id is not None
                    else None
                )
                if row is None:
                    row = MediaRow()
                    session.add(row)
                row.user_id = int(record.user_id)
                row.filename = record.filename
                row.status = record.status.value
                row.file_path = record.source
                row.content_hash = record.content_hash
                row.content_type = record.content_type
                row.upload_time = _db_now() if record.media_id is None else record.uploaded_at.replace(tzinfo=None)
                session.flush()
                return _media_value(row)
        except IntegrityError as exc:
            raise R2DatabaseError("MySQL media record conflicts with existing data") from exc
        except R2DatabaseError:
            raise
        except Exception as exc:
            raise R2DatabaseError("MySQL media transaction failed") from exc

    def _get_sync(self, media_id: int) -> MediaRecord | None:
        with self._sessions() as session:
            row = session.get(MediaRow, int(media_id))
            return None if row is None else _media_value(row)

    def _delete_sync(self, media_id: int) -> None:
        try:
            with self._sessions.begin() as session:
                session.execute(delete(MediaRow).where(MediaRow.id == int(media_id)))
        except Exception as exc:
            raise R2DatabaseError("MySQL media delete failed") from exc

    def _list_owned_sync(self, user_id: int) -> tuple[MediaRecord, ...]:
        with self._sessions() as session:
            statement = (
                select(MediaRow)
                .where(MediaRow.user_id == int(user_id))
                .order_by(MediaRow.upload_time.desc(), MediaRow.id.desc())
            )
            return tuple(_media_value(row) for row in session.scalars(statement))


class MySQLMediaRecordRepository(SqlAlchemyMediaRecordRepository):
    """Java spelling alias for the SQLAlchemy media repository."""


def _media_value(row: MediaRow) -> MediaRecord:
    return MediaRecord(
        user_id=int(row.user_id),
        filename=str(row.filename),
        source=str(row.file_path),
        content_hash=row.content_hash,
        media_id=int(row.id),
        status=MediaStatus(str(row.status).upper()),
        uploaded_at=_utc(row.upload_time),
        content_type=row.content_type,
    )


@dataclass(frozen=True, slots=True)
class UserRecord:
    user_id: int
    username: str
    password_hash: str
    nickname: str
    avatar: str | None = None
    role: str = "USER"

    def info(self) -> dict[str, Any]:
        return {
            "id": self.user_id,
            "username": self.username,
            "nickname": self.nickname,
            "avatar": self.avatar,
            "role": self.role,
        }


class SqlAlchemyUserStore:
    """Durable identity store; session/token state remains in Redis."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sessions = _session_factory(engine)

    def create(
        self,
        username: str,
        password_hash: str,
        nickname: str,
        *,
        avatar: str | None = None,
        role: str = "USER",
    ) -> UserRecord:
        try:
            with self._sessions.begin() as session:
                row = UserRow(
                    username=username,
                    password=password_hash,
                    nickname=nickname,
                    avatar=avatar,
                    role=role,
                )
                session.add(row)
                session.flush()
                return _user_value(row)
        except IntegrityError as exc:
            raise ValueError("username already exists") from exc
        except Exception as exc:
            raise R2DatabaseError("MySQL user transaction failed") from exc

    def get_by_username(self, username: str) -> UserRecord | None:
        with self._sessions() as session:
            row = session.scalar(select(UserRow).where(UserRow.username == username))
            return None if row is None else _user_value(row)

    def get(self, user_id: int) -> UserRecord | None:
        with self._sessions() as session:
            row = session.get(UserRow, int(user_id))
            return None if row is None else _user_value(row)

    def delete(self, user_id: int) -> None:
        try:
            with self._sessions.begin() as session:
                session.execute(delete(UserRow).where(UserRow.id == int(user_id)))
        except Exception as exc:
            raise R2DatabaseError("MySQL user delete failed") from exc


def _user_value(row: UserRow) -> UserRecord:
    return UserRecord(
        user_id=int(row.id),
        username=str(row.username),
        password_hash=str(row.password),
        nickname=str(row.nickname),
        avatar=row.avatar,
        role=str(row.role),
    )


@dataclass(frozen=True, slots=True)
class FailedTaskRecord:
    media_id: int
    action: str
    mode: str
    content_hash: str
    user_goal: str
    attempt_count: int
    error_type: str
    error_message: str | None = None
    status: str = "FAILED"
    task_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    replay_attempt_count: int = 0
    replay_status: str = "NEVER_REPLAYED"
    latest_replay_attempt_id: str | None = None


@dataclass(frozen=True, slots=True)
class FailedTaskReplayAttempt:
    attempt_id: str
    failed_task_id: int
    attempt_number: int
    status: str
    created_at: datetime
    updated_at: datetime
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class FailedTaskReplayReservation:
    attempt: FailedTaskReplayAttempt | None
    conflict: str | None = None


class SqlAlchemyFailedTaskStore:
    """Small durable operations ledger for bounded failure recovery."""

    def __init__(self, engine: Engine) -> None:
        self._sessions = _session_factory(engine)
        # MySQL row locking provides cross-process serialization. The local
        # guard also makes the same contract deterministic for SQLite tests,
        # whose dialect does not implement SELECT FOR UPDATE.
        self._replay_guard = RLock()

    def record(self, value: FailedTaskRecord) -> FailedTaskRecord:
        try:
            with self._sessions.begin() as session:
                row = FailedAnalysisTaskRow(
                    media_id=value.media_id,
                    action=value.action,
                    mode=value.mode,
                    content_hash=value.content_hash,
                    user_goal=value.user_goal,
                    attempt_count=value.attempt_count,
                    error_type=value.error_type,
                    error_message=value.error_message,
                    status=value.status,
                )
                session.add(row)
                session.flush()
                return self._record_value(session, row)
        except Exception as exc:
            raise R2DatabaseError("MySQL failed-task transaction failed") from exc

    def list_failed(self, *, limit: int = 50, offset: int = 0) -> tuple[tuple[FailedTaskRecord, ...], int]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("failed-task page limit must be between 1 and 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
            raise ValueError("failed-task page offset is outside its bound")
        try:
            with self._sessions() as session:
                total = int(session.scalar(select(func.count()).select_from(FailedAnalysisTaskRow)) or 0)
                rows = session.scalars(
                    select(FailedAnalysisTaskRow)
                    .order_by(FailedAnalysisTaskRow.created_at.desc(), FailedAnalysisTaskRow.id.desc())
                    .offset(offset)
                    .limit(limit)
                ).all()
                return tuple(self._record_value(session, row) for row in rows), total
        except Exception as exc:
            raise R2DatabaseError("MySQL failed-task listing failed") from exc

    def get_failed(self, task_id: int) -> FailedTaskRecord | None:
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
            raise ValueError("failed-task identifier must be positive")
        try:
            with self._sessions() as session:
                row = session.get(FailedAnalysisTaskRow, task_id)
                return None if row is None else self._record_value(session, row)
        except Exception as exc:
            raise R2DatabaseError("MySQL failed-task read failed") from exc

    def get_replay_attempt(
        self,
        task_id: int,
        idempotency_key: str,
    ) -> FailedTaskReplayAttempt | None:
        digest = _idempotency_digest(idempotency_key)
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(FailedAnalysisTaskReplayRow).where(
                        FailedAnalysisTaskReplayRow.failed_task_id == int(task_id),
                        FailedAnalysisTaskReplayRow.idempotency_digest == digest,
                    )
                )
                return None if row is None else _replay_attempt_value(row)
        except Exception as exc:
            raise R2DatabaseError("MySQL failed-task replay lookup failed") from exc

    def list_replay_attempts(
        self,
        task_id: int,
        *,
        limit: int = 20,
    ) -> tuple[FailedTaskReplayAttempt, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("replay history limit must be between 1 and 100")
        try:
            with self._sessions() as session:
                rows = session.scalars(
                    select(FailedAnalysisTaskReplayRow)
                    .where(FailedAnalysisTaskReplayRow.failed_task_id == int(task_id))
                    .order_by(
                        FailedAnalysisTaskReplayRow.attempt_number.desc(),
                        FailedAnalysisTaskReplayRow.created_at.desc(),
                    )
                    .limit(limit)
                ).all()
                return tuple(_replay_attempt_value(row) for row in rows)
        except Exception as exc:
            raise R2DatabaseError("MySQL failed-task replay history read failed") from exc

    def reserve_replay(
        self,
        task_id: int,
        idempotency_key: str,
    ) -> FailedTaskReplayReservation:
        digest = _idempotency_digest(idempotency_key)
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
            raise ValueError("failed-task identifier must be positive")
        with self._replay_guard:
            try:
                with self._sessions.begin() as session:
                    failed = session.scalar(
                        select(FailedAnalysisTaskRow)
                        .where(FailedAnalysisTaskRow.id == task_id)
                        .with_for_update()
                    )
                    if failed is None:
                        return FailedTaskReplayReservation(None, "NOT_FOUND")
                    existing = session.scalar(
                        select(FailedAnalysisTaskReplayRow).where(
                            FailedAnalysisTaskReplayRow.failed_task_id == task_id,
                            FailedAnalysisTaskReplayRow.idempotency_digest == digest,
                        )
                    )
                    if existing is not None:
                        if existing.status == "DISPATCH_FAILED":
                            existing.status = "REQUESTED"
                            existing.error_type = None
                            existing.updated_at = _db_now()
                        session.flush()
                        return FailedTaskReplayReservation(_replay_attempt_value(existing))

                    latest = session.scalar(
                        select(FailedAnalysisTaskReplayRow)
                        .where(FailedAnalysisTaskReplayRow.failed_task_id == task_id)
                        .order_by(
                            FailedAnalysisTaskReplayRow.attempt_number.desc(),
                            FailedAnalysisTaskReplayRow.created_at.desc(),
                        )
                        .limit(1)
                    )
                    if latest is not None and latest.status in {"REQUESTED", "DISPATCHED"}:
                        return FailedTaskReplayReservation(None, "IN_PROGRESS")
                    if latest is not None and latest.status == "SUCCEEDED":
                        return FailedTaskReplayReservation(None, "ALREADY_SUCCEEDED")
                    number = int(
                        session.scalar(
                            select(func.count()).select_from(FailedAnalysisTaskReplayRow).where(
                                FailedAnalysisTaskReplayRow.failed_task_id == task_id
                            )
                        )
                        or 0
                    ) + 1
                    row = FailedAnalysisTaskReplayRow(
                        attempt_id=str(uuid4()),
                        failed_task_id=task_id,
                        attempt_number=number,
                        idempotency_digest=digest,
                        status="REQUESTED",
                    )
                    session.add(row)
                    session.flush()
                    return FailedTaskReplayReservation(_replay_attempt_value(row))
            except Exception as exc:
                raise R2DatabaseError("MySQL failed-task replay reservation failed") from exc

    def mark_replay_attempt(
        self,
        attempt_id: str,
        status: str,
        *,
        error_type: str | None = None,
    ) -> FailedTaskReplayAttempt:
        allowed = {
            "REQUESTED": {"DISPATCHED", "DISPATCH_FAILED", "CONFLICT"},
            "DISPATCHED": {"SUCCEEDED", "FAILED_AGAIN", "CONFLICT"},
            "DISPATCH_FAILED": {"REQUESTED", "CONFLICT"},
            "SUCCEEDED": set(),
            "FAILED_AGAIN": set(),
            "CONFLICT": set(),
        }
        if status not in allowed:
            raise ValueError("invalid failed-task replay status")
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(FailedAnalysisTaskReplayRow)
                    .where(FailedAnalysisTaskReplayRow.attempt_id == str(attempt_id))
                    .with_for_update()
                )
                if row is None:
                    raise ValueError("failed-task replay attempt not found")
                if status != row.status and status not in allowed[row.status]:
                    raise ValueError("invalid failed-task replay status transition")
                row.status = status
                row.error_type = None if error_type is None else str(error_type)[:128]
                row.updated_at = _db_now()
                session.flush()
                return _replay_attempt_value(row)
        except ValueError:
            raise
        except Exception as exc:
            raise R2DatabaseError("MySQL failed-task replay update failed") from exc

    @staticmethod
    def _record_value(session: Session, row: FailedAnalysisTaskRow) -> FailedTaskRecord:
        latest = session.scalar(
            select(FailedAnalysisTaskReplayRow)
            .where(FailedAnalysisTaskReplayRow.failed_task_id == int(row.id))
            .order_by(
                FailedAnalysisTaskReplayRow.attempt_number.desc(),
                FailedAnalysisTaskReplayRow.created_at.desc(),
            )
            .limit(1)
        )
        count = int(
            session.scalar(
                select(func.count()).select_from(FailedAnalysisTaskReplayRow).where(
                    FailedAnalysisTaskReplayRow.failed_task_id == int(row.id)
                )
            )
            or 0
        )
        return FailedTaskRecord(
            media_id=int(row.media_id),
            action=str(row.action),
            mode=str(row.mode),
            content_hash=str(row.content_hash),
            user_goal=str(row.user_goal),
            attempt_count=int(row.attempt_count),
            error_type=str(row.error_type),
            error_message=row.error_message,
            status=str(row.status),
            task_id=int(row.id),
            created_at=_utc(row.created_at),
            updated_at=_utc(row.updated_at),
            replay_attempt_count=count,
            replay_status="NEVER_REPLAYED" if latest is None else str(latest.status),
            latest_replay_attempt_id=None if latest is None else str(latest.attempt_id),
        )


__all__ = [
    "Base",
    "CheckpointRow",
    "AgentExecutionRecordRow",
    "AgentExecutionEventRow",
    "FailedAnalysisTaskRow",
    "FailedAnalysisTaskReplayRow",
    "FailedTaskRecord",
    "FailedTaskReplayAttempt",
    "FailedTaskReplayReservation",
    "MediaRow",
    "MySQLCheckpointStore",
    "MySQLExecutionRecordRepository",
    "MySQLMediaRecordRepository",
    "R2DatabaseError",
    "SqlAlchemyCheckpointStore",
    "SqlAlchemyExecutionRecordRepository",
    "SqlAlchemyFailedTaskStore",
    "SqlAlchemyMediaRecordRepository",
    "SqlAlchemyUserStore",
    "UserRecord",
    "UserRow",
    "create_schema",
    "create_sqlalchemy_engine",
]
