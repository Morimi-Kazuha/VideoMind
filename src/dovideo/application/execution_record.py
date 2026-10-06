"""Bounded durable Agent execution history for X2-B.

This module deliberately does not implement replay.  It owns only the
historical execution contract, append-only semantic events, and the narrow
repository/application boundary that a later replay implementation can read.
Checkpoint state and the X1 tool ledger remain separate authorities.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisMode,
    AnalysisResult,
    CriticResult,
    PROVENANCE_VERSION,
)
from dovideo.domain._base import DomainModel, normalize_nullable_aliases, tuple_or_empty

from .analysis_task_keys import goal_digest
from .task_lease import TaskLeaseUnavailable, check_task_lease
from .model_routing import (
    ModelProfile,
    ModelRouteHistory,
    ModelRoutingDecision,
)
from .value_objects import TaskKey


EXECUTION_RECORD_SCHEMA_VERSION = "x2-b-record-v1"
EXECUTION_CONTRACT_VERSION_V1 = "agent-execution-semantics-v1"
EXECUTION_CONTRACT_VERSION_V2 = "agent-execution-semantics-v2"
# New production records use the route-aware contract.  The application
# service keeps a v1-compatible default for callers that are not composed
# with the J1 route recorder; R4 explicitly opts into v2.
EXECUTION_CONTRACT_VERSION = EXECUTION_CONTRACT_VERSION_V2
DEFAULT_EXECUTION_CONTRACT_VERSION = EXECUTION_CONTRACT_VERSION_V1
SUPPORTED_EXECUTION_CONTRACT_VERSIONS = frozenset(
    {EXECUTION_CONTRACT_VERSION_V1, EXECUTION_CONTRACT_VERSION_V2}
)
MODE_PROFILE_CONTRACT_VERSION = "mode-profile-v1"
TOOL_POLICY_CONTRACT_VERSION = "x1-tool-policy-v1"
MAX_EXECUTION_EVENTS = 128
MAX_EXECUTION_EVENT_PAYLOAD_BYTES = 128 * 1024
MAX_EXECUTION_ID_LENGTH = 96
MAX_EXECUTION_LOGICAL_EVENT_ID_LENGTH = 192
MAX_EXECUTION_MEDIA_IDENTITY_LENGTH = 512
MAX_EXECUTION_VERSION_LENGTH = 96
MAX_EXECUTION_RETRIEVAL_REFS = 32


class ExecutionRecordStatus(str, Enum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ExecutionEventType(str, Enum):
    EXECUTION_STARTED = "EXECUTION_STARTED"
    MODEL_ROUTE_RECORDED = "MODEL_ROUTE_RECORDED"
    PLAN_RECORDED = "PLAN_RECORDED"
    PLAN_REPAIRED = "PLAN_REPAIRED"
    PLAN_REPLANNED = "PLAN_REPLANNED"
    RETRIEVAL_SELECTED = "RETRIEVAL_SELECTED"
    EXECUTOR_TURN_RECORDED = "EXECUTOR_TURN_RECORDED"
    TOOL_CALL_REFERENCED = "TOOL_CALL_REFERENCED"
    CRITIC_RECORDED = "CRITIC_RECORDED"
    EVIDENCE_VERIFICATION_RECORDED = "EVIDENCE_VERIFICATION_RECORDED"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    EXECUTION_FAILED = "EXECUTION_FAILED"


class ExecutionRecordError(RuntimeError):
    """Base error for the historical execution boundary."""


class ExecutionRecordNotFoundError(ExecutionRecordError):
    """The requested execution record does not exist."""


class ExecutionRecordConflictError(ExecutionRecordError):
    """An append or transition would change an existing historical fact."""


class ExecutionRecordPersistenceError(ExecutionRecordError):
    """The durable execution-record backend could not complete an operation."""


class ExecutionTaskKey(DomainModel):
    """Stable serialized form of the existing logical :class:`TaskKey`."""

    media_id: int = Field(alias="mediaId")
    goal: str
    mode: AnalysisMode = AnalysisMode.GENERAL

    @model_validator(mode="before")
    @classmethod
    def _normalize_task_key(cls, data: Any) -> Any:
        if isinstance(data, TaskKey):
            return {
                "mediaId": data.media_id,
                "goal": data.goal,
                "mode": data.mode.value,
            }
        return normalize_nullable_aliases(
            data,
            {"goal": "", "mode": AnalysisMode.GENERAL},
            ("media_id", "mediaId"),
        )

    @field_validator("goal")
    @classmethod
    def _require_goal(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 500:
            raise ValueError("execution task goal must contain 1 to 500 characters")
        return normalized

    @field_validator("media_id")
    @classmethod
    def _valid_media_id(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("execution task media id must be positive")
        return value

    @classmethod
    def from_task_key(cls, key: TaskKey) -> "ExecutionTaskKey":
        if not isinstance(key, TaskKey):
            raise TypeError("task_key must be a TaskKey")
        return cls.model_validate(key)

    def to_task_key(self) -> TaskKey:
        return TaskKey(self.media_id, self.goal, self.mode)


class DurableExecutionEvent(DomainModel):
    """One immutable, ordered semantic fact in an Agent execution."""

    execution_id: str = Field(alias="executionId")
    sequence_no: int = Field(alias="sequenceNo")
    event_type: ExecutionEventType = Field(alias="eventType")
    logical_event_id: str = Field(alias="logicalEventId")
    agent_round: int = Field(default=0, alias="agentRound")
    stage: str = "AGENT"
    recorded_at: datetime = Field(alias="recordedAt")
    payload: Mapping[str, Any] = {}

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable(cls, data: Any) -> Any:
        return normalize_nullable_aliases(
            data,
            {"stage": "AGENT", "payload": {}},
            ("execution_id", "executionId"),
            ("sequence_no", "sequenceNo"),
            ("event_type", "eventType"),
            ("logical_event_id", "logicalEventId"),
            ("agent_round", "agentRound"),
            ("recorded_at", "recordedAt"),
        )

    @field_validator("execution_id", "logical_event_id", "stage")
    @classmethod
    def _bounded_text(cls, value: str, info) -> str:  # type: ignore[no-untyped-def]
        normalized = value.strip()
        limit = (
            MAX_EXECUTION_ID_LENGTH
            if info.field_name == "execution_id"
            else MAX_EXECUTION_LOGICAL_EVENT_ID_LENGTH
            if info.field_name == "logical_event_id"
            else 96
        )
        if not normalized or len(normalized) > limit:
            raise ValueError(f"{info.field_name} is blank or exceeds its bound")
        return normalized

    @field_validator("sequence_no")
    @classmethod
    def _positive_sequence(cls, value: int) -> int:
        if isinstance(value, bool) or value < 1:
            raise ValueError("sequence_no must be positive")
        return value

    @field_validator("agent_round")
    @classmethod
    def _nonnegative_round(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0:
            raise ValueError("agent_round must be non-negative")
        return value

    @field_validator("payload", mode="before")
    @classmethod
    def _bounded_payload(cls, value: Any) -> dict[str, Any]:
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ValueError("execution event payload must be an object")
        payload = _json_value(dict(value))
        if not isinstance(payload, dict):  # pragma: no cover - narrowed above
            raise ValueError("execution event payload must be an object")
        encoded = _canonical_json(payload)
        if len(encoded.encode("utf-8")) > MAX_EXECUTION_EVENT_PAYLOAD_BYTES:
            raise ValueError("execution event payload exceeds its bound")
        return payload


class DurableAgentExecutionRecord(DomainModel):
    """Bounded immutable historical header plus ordered semantic events."""

    record_schema_version: str = Field(
        default=EXECUTION_RECORD_SCHEMA_VERSION,
        alias="recordSchemaVersion",
    )
    execution_contract_version: str = Field(
        default=EXECUTION_CONTRACT_VERSION,
        alias="executionContractVersion",
    )
    execution_id: str = Field(alias="executionId")
    task_key: ExecutionTaskKey = Field(alias="taskKey")
    media_identity: str = Field(alias="mediaIdentity")
    source_revision: str = Field(alias="sourceRevision")
    source_provenance_version: str = Field(
        default=PROVENANCE_VERSION,
        alias="sourceProvenanceVersion",
    )
    mode: AnalysisMode
    mode_profile_version: str = Field(
        default=MODE_PROFILE_CONTRACT_VERSION,
        alias="modeProfileVersion",
    )
    tool_policy_version: str = Field(
        default=TOOL_POLICY_CONTRACT_VERSION,
        alias="toolPolicyVersion",
    )
    worker_attempt: int | None = Field(default=None, alias="workerAttempt")
    request_id: str | None = Field(default=None, alias="requestId")
    parent_execution_id: str | None = Field(default=None, alias="parentExecutionId")
    status: ExecutionRecordStatus = ExecutionRecordStatus.STARTED
    replayable: bool = False
    created_at: datetime = Field(alias="createdAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")
    events: tuple[DurableExecutionEvent, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable(cls, data: Any) -> Any:
        return normalize_nullable_aliases(
            data,
            {
                "source_provenance_version": PROVENANCE_VERSION,
                "sourceProvenanceVersion": PROVENANCE_VERSION,
                "mode_profile_version": MODE_PROFILE_CONTRACT_VERSION,
                "modeProfileVersion": MODE_PROFILE_CONTRACT_VERSION,
                "tool_policy_version": TOOL_POLICY_CONTRACT_VERSION,
                "toolPolicyVersion": TOOL_POLICY_CONTRACT_VERSION,
                "events": (),
                "worker_attempt": None,
                "workerAttempt": None,
                "request_id": None,
                "requestId": None,
                "parent_execution_id": None,
                "parentExecutionId": None,
                "completed_at": None,
                "completedAt": None,
            },
            ("execution_id", "executionId"),
            ("task_key", "taskKey"),
            ("media_identity", "mediaIdentity"),
            ("source_revision", "sourceRevision"),
            ("source_provenance_version", "sourceProvenanceVersion"),
            ("mode_profile_version", "modeProfileVersion"),
            ("tool_policy_version", "toolPolicyVersion"),
            ("worker_attempt", "workerAttempt"),
            ("request_id", "requestId"),
            ("parent_execution_id", "parentExecutionId"),
            ("created_at", "createdAt"),
            ("completed_at", "completedAt"),
        )

    @field_validator(
        "record_schema_version",
        "execution_contract_version",
        "source_provenance_version",
        "mode_profile_version",
        "tool_policy_version",
    )
    @classmethod
    def _version_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > MAX_EXECUTION_VERSION_LENGTH:
            raise ValueError("execution record version is blank or too long")
        return normalized

    @field_validator("execution_id")
    @classmethod
    def _execution_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > MAX_EXECUTION_ID_LENGTH:
            raise ValueError("execution_id is blank or too long")
        return normalized

    @field_validator("media_identity")
    @classmethod
    def _media_identity(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > MAX_EXECUTION_MEDIA_IDENTITY_LENGTH:
            raise ValueError("media_identity is blank or too long")
        return normalized

    @field_validator("source_revision")
    @classmethod
    def _source_revision(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > MAX_EXECUTION_VERSION_LENGTH:
            raise ValueError("source_revision is blank or too long")
        return normalized

    @field_validator("worker_attempt")
    @classmethod
    def _worker_attempt(cls, value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or value < 1):
            raise ValueError("worker_attempt must be positive when present")
        return value

    @field_validator("request_id", "parent_execution_id")
    @classmethod
    def _optional_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > MAX_EXECUTION_ID_LENGTH:
            raise ValueError("optional execution identity is invalid")
        return normalized

    @field_validator("events", mode="before")
    @classmethod
    def _copy_events(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)

    @model_validator(mode="after")
    def _validate_record(self) -> "DurableAgentExecutionRecord":
        if self.mode is not self.task_key.mode:
            raise ValueError("record mode must match task_key mode")
        if len(self.events) > MAX_EXECUTION_EVENTS:
            raise ValueError("execution event count exceeds its bound")
        expected = 1
        seen_logical: set[str] = set()
        for event in self.events:
            if event.execution_id != self.execution_id:
                raise ValueError("execution event belongs to another execution")
            if event.sequence_no != expected:
                raise ValueError("execution event sequence is not contiguous")
            if event.logical_event_id in seen_logical:
                raise ValueError("duplicate logical execution event")
            seen_logical.add(event.logical_event_id)
            expected += 1
        if self.status is ExecutionRecordStatus.COMPLETED:
            if self.completed_at is None or not self.replayable:
                raise ValueError("completed execution must be replayable and timestamped")
        if self.status is ExecutionRecordStatus.STARTED and self.completed_at is not None:
            raise ValueError("started execution cannot have completed_at")
        return self

    @property
    def task_key_value(self) -> TaskKey:
        return self.task_key.to_task_key()

    @property
    def latest_sequence(self) -> int:
        return self.events[-1].sequence_no if self.events else 0

    @property
    def recorded_semantic_events(self) -> int:
        return len(self.events)


class ExecutionRecordRepositoryPort(Protocol):
    """Narrow durable history repository; not a checkpoint repository."""

    def create(self, record: DurableAgentExecutionRecord) -> DurableAgentExecutionRecord:
        ...

    def get(self, execution_id: str) -> DurableAgentExecutionRecord | None:
        ...

    def latest_for_task(self, task_key: TaskKey) -> DurableAgentExecutionRecord | None:
        ...

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
        ...

    def update_status(
        self,
        execution_id: str,
        status: ExecutionRecordStatus,
        *,
        completed_at: datetime | None,
        replayable: bool,
    ) -> DurableAgentExecutionRecord:
        ...


# Descriptive alias used by composition roots and migration callers.
ExecutionRecordRepository = ExecutionRecordRepositoryPort


class ExecutionRecordService:
    """Application service for start, append, completion, and failure facts."""

    def __init__(
        self,
        repository: ExecutionRecordRepositoryPort,
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        trace_projection: Callable[..., Any] | None = None,
        execution_contract_version: str = DEFAULT_EXECUTION_CONTRACT_VERSION,
    ) -> None:
        if repository is None:
            raise ValueError("execution record repository is required")
        self.repository = repository
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._trace_projection = trace_projection
        self._execution_contract_version = _execution_contract_version(
            execution_contract_version
        )

    async def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        check_task_lease()
        method = getattr(self.repository, operation, None)
        if not callable(method):
            raise ExecutionRecordPersistenceError(
                f"execution record repository has no {operation} operation"
            )
        try:
            def guarded_call():
                check_task_lease()
                return method(*args, **kwargs)

            value = await asyncio.to_thread(guarded_call)
            check_task_lease()
            if inspect.isawaitable(value):
                value = await value
                check_task_lease()
            return value
        except (ExecutionRecordError, TaskLeaseUnavailable):
            raise
        except Exception as exc:
            raise ExecutionRecordPersistenceError(
                f"execution record {operation} failed"
            ) from exc

    async def load(self, execution_id: str) -> DurableAgentExecutionRecord | None:
        return await self._call("get", execution_id)

    async def load_for_task(self, task_key: TaskKey) -> DurableAgentExecutionRecord | None:
        return await self._call("latest_for_task", task_key)

    async def start_or_resume(
        self,
        task_key: TaskKey,
        *,
        media_identity: str,
        source_revision: str,
        worker_attempt: int | None = None,
        request_id: str | None = None,
        source_provenance_version: str = PROVENANCE_VERSION,
        mode_profile_version: str = MODE_PROFILE_CONTRACT_VERSION,
        tool_policy_version: str = TOOL_POLICY_CONTRACT_VERSION,
        execution_contract_version: str | None = None,
        force_new: bool = False,
    ) -> DurableAgentExecutionRecord:
        if not isinstance(task_key, TaskKey):
            raise TypeError("task_key must be a TaskKey")
        if not isinstance(media_identity, str) or not media_identity.strip():
            raise ValueError("media_identity is required")
        if not isinstance(source_revision, str) or not source_revision.strip():
            raise ValueError("source_revision is required")
        selected_contract_version = _execution_contract_version(
            execution_contract_version or self._execution_contract_version
        )
        latest = await self.load_for_task(task_key)
        if latest is not None:
            self._validate_header_match(
                latest,
                task_key,
                media_identity=media_identity,
                source_revision=source_revision,
            )
            if latest.status is ExecutionRecordStatus.STARTED and not force_new:
                return latest
            if latest.status is ExecutionRecordStatus.COMPLETED and not force_new:
                return latest
            if (
                latest.status is ExecutionRecordStatus.FAILED
                and not force_new
                and (request_id is None or request_id == latest.request_id)
            ):
                return latest
            if latest.status is ExecutionRecordStatus.CANCELLED and not force_new:
                return latest

        execution_id = str(self._id_factory()).strip()
        if not execution_id or len(execution_id) > MAX_EXECUTION_ID_LENGTH:
            raise ValueError("execution id factory returned an invalid id")
        now = _utc(self._clock())
        serialized_key = ExecutionTaskKey.from_task_key(task_key)
        record = DurableAgentExecutionRecord(
            record_schema_version=EXECUTION_RECORD_SCHEMA_VERSION,
            execution_contract_version=selected_contract_version,
            execution_id=execution_id,
            task_key=serialized_key,
            media_identity=media_identity.strip(),
            source_revision=source_revision.strip(),
            source_provenance_version=source_provenance_version,
            mode=task_key.mode,
            mode_profile_version=mode_profile_version,
            tool_policy_version=tool_policy_version,
            worker_attempt=worker_attempt,
            request_id=request_id,
            parent_execution_id=(
                latest.execution_id
                if latest is not None and latest.status is ExecutionRecordStatus.FAILED
                else None
            ),
            status=ExecutionRecordStatus.STARTED,
            replayable=False,
            created_at=now,
            events=(
                DurableExecutionEvent(
                    execution_id=execution_id,
                    sequence_no=1,
                    event_type=ExecutionEventType.EXECUTION_STARTED,
                    logical_event_id="execution.started",
                    agent_round=0,
                    stage="AGENT",
                    recorded_at=now,
                    payload={
                        "mediaId": task_key.media_id,
                        "mode": task_key.mode.value,
                        "goalDigest": goal_digest(task_key.goal, task_key.mode),
                        "sourceRevision": source_revision.strip(),
                        "sourceProvenanceVersion": source_provenance_version,
                        "workerAttempt": worker_attempt,
                        "recordSchemaVersion": EXECUTION_RECORD_SCHEMA_VERSION,
                        "executionContractVersion": selected_contract_version,
                    },
                ),
            ),
        )
        created = await self._call("create", record)
        self._project(created, created.events[-1])
        return created

    async def record_model_route(
        self,
        execution_id: str,
        decision: ModelRoutingDecision | ModelRouteHistory,
        *,
        profile: ModelProfile | None = None,
        profile_id: str | None = None,
        resolved_model_id: str,
        logical_event_id: str = "model.route",
        agent_round: int = 0,
    ) -> DurableExecutionEvent:
        """Persist the one v2 historical route fact before Planner work.

        The operational ``modelRouting`` checkpoint remains separate.  This
        method is the only execution-history writer for the semantic route
        event, so retries inherit the repository's logical-event idempotency
        and conflict behavior.
        """

        current = await self.load(execution_id)
        if current is None:
            raise ExecutionRecordNotFoundError("execution record was not found")
        if current.execution_contract_version != EXECUTION_CONTRACT_VERSION_V2:
            raise ExecutionRecordConflictError(
                "MODEL_ROUTE_RECORDED requires execution contract v2"
            )
        for existing_event in current.events:
            if existing_event.event_type is ExecutionEventType.MODEL_ROUTE_RECORDED:
                if existing_event.logical_event_id != logical_event_id:
                    raise ExecutionRecordConflictError(
                        "execution already has a different model route event identity"
                    )
                break
        if isinstance(decision, ModelRouteHistory):
            history = decision
            if profile_id is not None and profile_id != history.profile_id:
                raise ExecutionRecordConflictError("historical route profile conflicts")
            if str(resolved_model_id).strip() != history.resolved_model_id:
                raise ExecutionRecordConflictError(
                    "historical route model identity conflicts"
                )
        else:
            if not isinstance(decision, ModelRoutingDecision):
                raise TypeError("decision must be a ModelRoutingDecision or ModelRouteHistory")
            selected_profile = profile
            if selected_profile is None:
                if not profile_id:
                    raise ValueError("historical route profile is required")
                selected_profile = ModelProfile(
                    profileId=profile_id,
                    lane=decision.lane,
                )
            elif profile_id is not None and profile_id != selected_profile.profile_id:
                raise ExecutionRecordConflictError("historical route profile conflicts")
            history = ModelRouteHistory.from_decision(
                decision,
                selected_profile,
                resolved_model_id,
            )
        return await self.append_event(
            execution_id,
            event_type=ExecutionEventType.MODEL_ROUTE_RECORDED,
            logical_event_id=logical_event_id,
            agent_round=agent_round,
            stage="ROUTING",
            payload={
                "kind": ExecutionEventType.MODEL_ROUTE_RECORDED.value,
                **history.model_dump(mode="json", by_alias=True),
            },
        )

    async def append_event(
        self,
        execution_id: str,
        *,
        event_type: ExecutionEventType,
        logical_event_id: str,
        agent_round: int = 0,
        stage: str = "AGENT",
        payload: Mapping[str, Any] | None = None,
    ) -> DurableExecutionEvent:
        event = await self._call(
            "append_event",
            execution_id,
            event_type=ExecutionEventType(event_type),
            logical_event_id=str(logical_event_id),
            agent_round=agent_round,
            stage=stage,
            payload={} if payload is None else dict(payload),
            recorded_at=_utc(self._clock()),
        )
        record = await self.load(execution_id)
        if record is not None:
            self._project(record, event)
        return event

    async def record_plan(
        self,
        execution_id: str,
        plan: AgentPlan,
        *,
        event_type: ExecutionEventType = ExecutionEventType.PLAN_RECORDED,
        logical_event_id: str = "plan.initial",
        agent_round: int = 0,
        repair_used: bool = False,
        repair_attempts: int = 0,
    ) -> DurableExecutionEvent:
        if not isinstance(plan, AgentPlan):
            plan = AgentPlan.model_validate(plan)
        return await self.append_event(
            execution_id,
            event_type=event_type,
            logical_event_id=logical_event_id,
            agent_round=agent_round,
            stage="PLANNER",
            payload={
                "kind": event_type.value,
                "plan": plan.model_dump(mode="json", by_alias=True),
                "repairUsed": bool(repair_used),
                "repairAttempts": max(0, min(2, int(repair_attempts))),
            },
        )

    async def record_retrieval_selection(
        self,
        execution_id: str,
        selected: Any,
        *,
        purpose: str,
        logical_event_id: str,
        agent_round: int = 0,
        query_digest: str | None = None,
        budget_truncated: bool = False,
    ) -> DurableExecutionEvent:
        refs = _retrieval_references(selected)
        payload: dict[str, Any] = {
            "purpose": str(purpose).strip()[:64],
            "selected": refs,
            "selectedCount": len(refs),
            "budgetTruncated": bool(budget_truncated),
        }
        if query_digest:
            payload["queryDigest"] = str(query_digest).strip()[:128]
        return await self.append_event(
            execution_id,
            event_type=ExecutionEventType.RETRIEVAL_SELECTED,
            logical_event_id=logical_event_id,
            agent_round=agent_round,
            stage="RETRIEVAL",
            payload=payload,
        )

    async def record_executor_turn(
        self,
        execution_id: str,
        turn: Any,
        *,
        logical_event_id: str,
        agent_round: int,
        request_index: int | None = None,
        args_digest: str | None = None,
    ) -> DurableExecutionEvent:
        payload = _executor_turn_payload(
            turn,
            request_index=request_index,
            args_digest=args_digest,
        )
        return await self.append_event(
            execution_id,
            event_type=ExecutionEventType.EXECUTOR_TURN_RECORDED,
            logical_event_id=logical_event_id,
            agent_round=agent_round,
            stage="EXECUTOR",
            payload=payload,
        )

    async def record_tool_reference(
        self,
        execution_id: str,
        *,
        logical_event_id: str,
        agent_round: int,
        call_id: str,
        request_index: int,
        tool_name: str,
        args_digest: str,
        policy_decision: str | None,
        reason_code: str | None,
        result_status: str,
        ledger_reference: str | None = None,
    ) -> DurableExecutionEvent:
        return await self.append_event(
            execution_id,
            event_type=ExecutionEventType.TOOL_CALL_REFERENCED,
            logical_event_id=logical_event_id,
            agent_round=agent_round,
            stage="TOOL",
            payload={
                "callId": str(call_id)[:128],
                "requestIndex": int(request_index),
                "toolName": str(tool_name)[:128],
                "argsDigest": str(args_digest)[:128],
                "policyDecision": None if policy_decision is None else str(policy_decision)[:32],
                "reasonCode": None if reason_code is None else str(reason_code)[:64],
                "resultStatus": str(result_status)[:32],
                "ledgerReference": ledger_reference or f"tool-call:{call_id}",
            },
        )

    async def record_critic(
        self,
        execution_id: str,
        critique: CriticResult,
        *,
        logical_event_id: str,
        agent_round: int,
        repair_used: bool = False,
    ) -> DurableExecutionEvent:
        if not isinstance(critique, CriticResult):
            critique = CriticResult.model_validate(critique)
        return await self.append_event(
            execution_id,
            event_type=ExecutionEventType.CRITIC_RECORDED,
            logical_event_id=logical_event_id,
            agent_round=agent_round,
            stage="CRITIC",
            payload={
                "critic": critique.model_dump(mode="json", by_alias=True),
                "repairUsed": bool(repair_used),
            },
        )

    async def record_evidence_verification(
        self,
        execution_id: str,
        result: AnalysisResult | None,
        critique: CriticResult,
        *,
        logical_event_id: str,
        agent_round: int,
        source_revision: str,
    ) -> DurableExecutionEvent:
        if not isinstance(critique, CriticResult):
            critique = CriticResult.model_validate(critique)
        normalized_revision = str(source_revision).strip()
        if not normalized_revision:
            raise ValueError("evidence verification requires source_revision")
        refs = _evidence_references(result)
        return await self.append_event(
            execution_id,
            event_type=ExecutionEventType.EVIDENCE_VERIFICATION_RECORDED,
            logical_event_id=logical_event_id,
            agent_round=agent_round,
            stage="EVIDENCE_GUARD",
            payload={
                "passed": bool(critique.passed),
                "feedback": tuple(critique.feedback),
                "missingRequirements": tuple(critique.missing_requirements),
                "unsupportedClaims": tuple(critique.unsupported_claims),
                "requiredTimestamps": tuple(critique.required_timestamps),
                "sourceRevision": normalized_revision,
                "evidenceReferences": refs,
            },
        )

    async def complete(
        self,
        execution_id: str,
        state: AgentState,
    ) -> DurableAgentExecutionRecord:
        if not isinstance(state, AgentState):
            state = AgentState.model_validate(state)
        current = await self.load(execution_id)
        if current is None:
            raise ExecutionRecordNotFoundError("execution record was not found")
        if current.status is ExecutionRecordStatus.COMPLETED:
            self._project(current, current.events[-1] if current.events else None)
            return current
        if current.status is not ExecutionRecordStatus.STARTED:
            raise ExecutionRecordConflictError("terminal execution cannot complete")
        await self.append_event(
            execution_id,
            event_type=ExecutionEventType.EXECUTION_COMPLETED,
            logical_event_id="execution.completed",
            agent_round=max(0, int(state.round)),
            stage="AGENT",
            payload={
                "finalResult": (
                    None
                    if state.result is None
                    else state.result.model_dump(mode="json", by_alias=True)
                ),
                "finalCritic": (
                    None
                    if state.critique is None
                    else state.critique.model_dump(mode="json", by_alias=True)
                ),
                "finalEvidenceVerification": (
                    None
                    if state.critique is None
                    else {
                        "passed": bool(state.critique.passed),
                        "requiredTimestamps": tuple(state.critique.required_timestamps),
                    }
                ),
            },
        )
        completed = await self._call(
            "update_status",
            execution_id,
            ExecutionRecordStatus.COMPLETED,
            completed_at=_utc(self._clock()),
            replayable=True,
        )
        self._project(completed, completed.events[-1] if completed.events else None)
        return completed

    async def fail(
        self,
        execution_id: str,
        error: BaseException | None = None,
        *,
        classification: str = "EXECUTION_FAILED",
    ) -> DurableAgentExecutionRecord:
        current = await self.load(execution_id)
        if current is None:
            raise ExecutionRecordNotFoundError("execution record was not found")
        if current.status is ExecutionRecordStatus.FAILED:
            self._project(current, current.events[-1] if current.events else None)
            return current
        if current.status is not ExecutionRecordStatus.STARTED:
            raise ExecutionRecordConflictError("terminal execution cannot fail")
        error_type = "" if error is None else type(error).__name__
        await self.append_event(
            execution_id,
            event_type=ExecutionEventType.EXECUTION_FAILED,
            logical_event_id="execution.failed",
            agent_round=0,
            stage="AGENT",
            payload={
                "classification": str(classification)[:96],
                "errorType": error_type[:96],
                "retryable": False,
            },
        )
        failed = await self._call(
            "update_status",
            execution_id,
            ExecutionRecordStatus.FAILED,
            completed_at=_utc(self._clock()),
            replayable=False,
        )
        self._project(failed, failed.events[-1] if failed.events else None)
        return failed

    async def fail_for_task(
        self,
        task_key: TaskKey,
        error: BaseException | None = None,
        *,
        classification: str = "EXECUTION_FAILED",
    ) -> DurableAgentExecutionRecord | None:
        current = await self.load_for_task(task_key)
        if current is None or current.status is not ExecutionRecordStatus.STARTED:
            return current
        return await self.fail(current.execution_id, error, classification=classification)

    def _validate_header_match(
        self,
        record: DurableAgentExecutionRecord,
        task_key: TaskKey,
        *,
        media_identity: str,
        source_revision: str,
    ) -> None:
        if record.task_key_value != task_key:
            raise ExecutionRecordConflictError("execution record task identity mismatch")
        if record.media_identity != str(media_identity).strip():
            raise ExecutionRecordConflictError("execution record media identity mismatch")
        if record.source_revision != str(source_revision).strip():
            raise ExecutionRecordConflictError("execution record source revision mismatch")

    def _project(
        self,
        record: DurableAgentExecutionRecord,
        event: DurableExecutionEvent | None,
    ) -> None:
        if self._trace_projection is None:
            return
        try:
            self._trace_projection(
                record.task_key_value,
                execution_id=record.execution_id,
                status=record.status.value,
                event_type=None if event is None else event.event_type.value,
                latest_sequence=record.latest_sequence if event is None else event.sequence_no,
                recorded_semantic_events=(
                    record.recorded_semantic_events
                    if event is None
                    else max(record.recorded_semantic_events, event.sequence_no)
                ),
            )
        except Exception:
            # Redis is an operational projection.  A projection outage cannot
            # alter or invalidate the already durable historical fact.
            return


class InMemoryExecutionRecordRepository:
    """Thread-safe deterministic repository used by focused tests."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, DurableAgentExecutionRecord] = {}
        self._order: dict[str, int] = {}
        self._counter = 0

    def create(self, record: DurableAgentExecutionRecord) -> DurableAgentExecutionRecord:
        with self._lock:
            existing = self._records.get(record.execution_id)
            if existing is not None:
                if existing != record:
                    raise ExecutionRecordConflictError("execution id already has different history")
                return existing
            self._counter += 1
            self._records[record.execution_id] = record
            self._order[record.execution_id] = self._counter
            return record

    def get(self, execution_id: str) -> DurableAgentExecutionRecord | None:
        with self._lock:
            return self._records.get(str(execution_id))

    def latest_for_task(self, task_key: TaskKey) -> DurableAgentExecutionRecord | None:
        with self._lock:
            values = [
                record
                for record in self._records.values()
                if record.task_key_value == task_key
            ]
            if not values:
                return None
            return max(values, key=lambda value: (value.created_at, self._order[value.execution_id]))

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
        with self._lock:
            current = self._records.get(str(execution_id))
            if current is None:
                raise ExecutionRecordNotFoundError("execution record was not found")
            for existing in current.events:
                if existing.logical_event_id == logical_event_id:
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
                    if _same_event(existing, candidate):
                        return existing
                    raise ExecutionRecordConflictError(
                        "logical execution event has conflicting payload"
                    )
            if current.status is not ExecutionRecordStatus.STARTED:
                raise ExecutionRecordConflictError("cannot append to a terminal execution")
            event = DurableExecutionEvent(
                execution_id=current.execution_id,
                sequence_no=current.latest_sequence + 1,
                event_type=event_type,
                logical_event_id=logical_event_id,
                agent_round=agent_round,
                stage=stage,
                recorded_at=recorded_at,
                payload=payload,
            )
            if event.sequence_no > MAX_EXECUTION_EVENTS:
                raise ExecutionRecordConflictError("execution event count exceeds its bound")
            self._records[current.execution_id] = current.model_copy(
                update={"events": (*current.events, event)}
            )
            return event

    def update_status(
        self,
        execution_id: str,
        status: ExecutionRecordStatus,
        *,
        completed_at: datetime | None,
        replayable: bool,
    ) -> DurableAgentExecutionRecord:
        with self._lock:
            current = self._records.get(str(execution_id))
            if current is None:
                raise ExecutionRecordNotFoundError("execution record was not found")
            status = ExecutionRecordStatus(status)
            if current.status is status:
                return current
            if current.status is not ExecutionRecordStatus.STARTED:
                raise ExecutionRecordConflictError("terminal execution status cannot change")
            updated = current.model_copy(
                update={
                    "status": status,
                    "completed_at": completed_at,
                    "replayable": bool(replayable),
                }
            )
            self._records[current.execution_id] = updated
            return updated

    def all_records(self) -> tuple[DurableAgentExecutionRecord, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._records.values(),
                    key=lambda value: self._order[value.execution_id],
                )
            )


def _same_event(left: DurableExecutionEvent, right: DurableExecutionEvent) -> bool:
    return (
        left.execution_id == right.execution_id
        and left.sequence_no == right.sequence_no
        and left.event_type is right.event_type
        and left.logical_event_id == right.logical_event_id
        and left.agent_round == right.agent_round
        and left.stage == right.stage
        and _canonical_json(left.payload) == _canonical_json(right.payload)
    )


def _execution_contract_version(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("execution_contract_version must be text")
    normalized = value.strip()
    if normalized not in SUPPORTED_EXECUTION_CONTRACT_VERSIONS:
        raise ValueError("unsupported execution contract version")
    return normalized


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("execution record clock must return datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _json_value(value.model_dump(mode="json", by_alias=True))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, TaskKey):
        return {
            "mediaId": value.media_id,
            "goal": value.goal,
            "mode": value.mode.value,
        }
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("execution record payload contains a non-JSON value")


def _retrieval_references(selected: Any) -> list[dict[str, Any]]:
    values = tuple(selected or ())
    references: list[dict[str, Any]] = []
    for item in values[:MAX_EXECUTION_RETRIEVAL_REFS]:
        reference: dict[str, Any] = {}
        for source, target in (
            ("segment_id", "segmentId"),
            ("segmentId", "segmentId"),
            ("chunk_id", "chunkId"),
            ("chunkId", "chunkId"),
            ("source_revision", "sourceRevision"),
            ("sourceRevision", "sourceRevision"),
            ("start_ms", "startMs"),
            ("startMs", "startMs"),
            ("end_ms", "endMs"),
            ("endMs", "endMs"),
            ("score", "score"),
        ):
            value = _read_value(item, source)
            if value not in (None, "") and target not in reference:
                reference[target] = value
        item_ids = _read_value(item, "source_item_ids")
        if item_ids is None:
            item_ids = _read_value(item, "sourceItemIds")
        if item_ids:
            reference["sourceItemIds"] = [str(value)[:128] for value in tuple(item_ids)[:8]]
        if reference:
            references.append(_json_value(reference))
    return references


def _evidence_references(result: AnalysisResult | None) -> list[dict[str, Any]]:
    if result is None:
        return []
    values: list[dict[str, Any]] = []
    for evidence in tuple(result.evidence)[:MAX_EXECUTION_RETRIEVAL_REFS]:
        values.append(
            {
                "timestampMs": evidence.timestamp_ms,
                "sourceRevision": evidence.source_revision,
                "segmentId": evidence.segment_id,
                "sourceItemIds": list(evidence.source_item_ids[:8]),
            }
        )
    return values


def _executor_turn_payload(
    turn: Any,
    *,
    request_index: int | None,
    args_digest: str | None = None,
) -> dict[str, Any]:
    kind = getattr(getattr(turn, "kind", None), "value", getattr(turn, "kind", ""))
    normalized = str(kind).upper()
    if normalized == "FINAL":
        result = getattr(turn, "final_result", None)
        if not isinstance(result, AnalysisResult):
            result = AnalysisResult.model_validate(result)
        return {
            "kind": "FINAL",
            "result": result.model_dump(mode="json", by_alias=True),
            "requestIndex": request_index,
        }
    request = getattr(turn, "tool_request", None)
    tool_name = getattr(request, "tool_name", "")
    args_digest = args_digest or getattr(request, "canonical_args_digest", None)
    if not args_digest:
        args_digest = hashlib.sha256(
            _canonical_json(getattr(request, "arguments", {})).encode("utf-8")
        ).hexdigest()
    call_id = getattr(request, "call_id", None)
    if not call_id and request_index is not None:
        call_id = f"tool-call-{request_index}"
    return {
        "kind": normalized,
        "requestIndex": request_index,
        "callId": call_id,
        "toolName": str(getattr(tool_name, "value", tool_name))[:128],
        "argsDigest": str(args_digest)[:128],
    }


def _read_value(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


__all__ = [
    "DEFAULT_EXECUTION_CONTRACT_VERSION",
    "EXECUTION_CONTRACT_VERSION",
    "EXECUTION_CONTRACT_VERSION_V1",
    "EXECUTION_CONTRACT_VERSION_V2",
    "EXECUTION_RECORD_SCHEMA_VERSION",
    "ExecutionEventType",
    "ExecutionRecordConflictError",
    "ExecutionRecordError",
    "ExecutionRecordNotFoundError",
    "ExecutionRecordPersistenceError",
    "ExecutionRecordRepository",
    "ExecutionRecordRepositoryPort",
    "ExecutionRecordService",
    "ExecutionRecordStatus",
    "SUPPORTED_EXECUTION_CONTRACT_VERSIONS",
    "ExecutionTaskKey",
    "DurableAgentExecutionRecord",
    "DurableExecutionEvent",
    "InMemoryExecutionRecordRepository",
    "MAX_EXECUTION_EVENT_PAYLOAD_BYTES",
    "MAX_EXECUTION_EVENTS",
    "MAX_EXECUTION_RETRIEVAL_REFS",
    "MODE_PROFILE_CONTRACT_VERSION",
    "TOOL_POLICY_CONTRACT_VERSION",
]
