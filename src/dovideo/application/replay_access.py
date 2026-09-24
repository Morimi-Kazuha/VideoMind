"""Authenticated access to deterministic historical execution replay.

The X2-C replay service is deliberately providerless and read-only, but it is
not itself an authorization boundary.  This module supplies that boundary for
the production API.  It resolves an execution id through the durable X2-B
record, binds ordinary-user access to the record's full :class:`TaskKey` and
the existing media ownership abstraction, and only then calls X2-C.

There is intentionally no replay job, replay execution record, or live-task
fallback here.  A replay response is derived from the immutable execution
record and the already durable X1 tool artifacts on every request.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from .execution_record import (
    ExecutionRecordPersistenceError,
    ExecutionRecordStatus,
)
from .historical_replay import (
    HistoricalAgentReplayService,
    HistoricalReplayError,
    HistoricalReplayResult,
)


class ReplayAccessError(RuntimeError):
    """Base class for safe authorization/composition failures."""

    status_code = 503
    code = "REPLAY_ACCESS_ERROR"

    def __init__(self, detail: str = "historical replay access failed") -> None:
        # The detail is for bounded internal diagnostics only.  The HTTP layer
        # maps this typed error to a stable message and never exposes it.
        normalized = " ".join(str(detail).split())[:256]
        super().__init__(f"{self.code}: {normalized or 'historical replay access failed'}")
        self.detail = normalized


class ReplayAccessNotFoundError(ReplayAccessError):
    status_code = 404
    code = "REPLAY_ACCESS_NOT_FOUND"


class ReplayAccessForbiddenError(ReplayAccessError):
    status_code = 403
    code = "REPLAY_ACCESS_FORBIDDEN"


class ReplayAccessUnavailableError(ReplayAccessError):
    status_code = 503
    code = "REPLAY_ACCESS_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class HistoricalReplayView:
    """Bounded API projection plus its stable derived-result digest."""

    result: HistoricalReplayResult
    payload: Mapping[str, Any]
    result_digest: str


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable(model_dump(mode="json", by_alias=True))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    # Historical DTOs are expected to be fully JSON-shaped.  Do not expose an
    # arbitrary object's repr if a future implementation accidentally adds one.
    raise ReplayAccessUnavailableError("historical replay projection is not JSON-shaped")


def _canonical_digest(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ReplayAccessUnavailableError("historical replay projection is not canonical") from error
    return hashlib.sha256(encoded).hexdigest()


def historical_replay_payload(result: HistoricalReplayResult) -> dict[str, Any]:
    """Project X2-C into the stable, privacy-bounded API response shape.

    In particular, ``mediaIdentity`` and the internal accumulated ``state``
    are not API fields.  Event payloads are also intentionally omitted; the
    bounded semantic projections below are the public historical result.
    """

    payload: dict[str, Any] = {
        "executionId": result.execution_id,
        "taskKey": _jsonable(result.task_key),
        "sourceRevision": result.source_revision,
        "sourceProvenanceVersion": result.source_provenance_version,
        "mode": _enum_value(result.mode),
        "recordSchemaVersion": result.record_schema_version,
        "executionContractVersion": result.execution_contract_version,
        "modeProfileVersion": result.mode_profile_version,
        "toolPolicyVersion": result.tool_policy_version,
        "status": _enum_value(result.status),
        "eventsReplayed": result.events_replayed,
        "lastSequenceNo": result.last_sequence_no,
        "historicalModelRoute": _jsonable(result.historical_model_route),
        "historicalPlans": _jsonable(result.historical_plans),
        "historicalPlan": _jsonable(result.historical_plan),
        "historicalRetrievals": _jsonable(result.historical_retrievals),
        "historicalExecutorTurns": _jsonable(result.historical_executor_turns),
        "historicalToolCalls": _jsonable(result.historical_tool_calls),
        "historicalCriticResults": _jsonable(result.historical_critic_results),
        "historicalEvidenceOutcomes": _jsonable(result.historical_evidence_outcomes),
        "historicalFinalResult": _jsonable(result.historical_final_result),
        "failure": _jsonable(result.failure),
    }
    payload["resultDigest"] = _canonical_digest(payload)
    return payload


def _record_task_key(record: Any) -> Any:
    task_key = getattr(record, "task_key_value", None)
    if task_key is not None:
        return task_key
    serialized = getattr(record, "task_key", None)
    converter = getattr(serialized, "to_task_key", None)
    if callable(converter):
        return converter()
    raise ReplayAccessUnavailableError("execution record has no trusted task key")


def _record_task_key_payload(record: Any) -> Any:
    serialized = getattr(record, "task_key", None)
    if serialized is None:
        raise ReplayAccessUnavailableError("execution record has no task key")
    return _jsonable(serialized)


def _record_status(record: Any) -> Any:
    return _enum_value(getattr(record, "status", None))


def historical_replay_metadata(record: Any) -> dict[str, Any]:
    """Return only bounded metadata from one durable execution record."""

    status = _record_status(record)
    if not isinstance(status, str) or not status:
        raise ReplayAccessUnavailableError("execution record has no stable status")
    events = getattr(record, "events", ())
    event_count = len(events) if isinstance(events, (tuple, list)) else 0
    last_sequence = 0
    if event_count:
        last_sequence = int(getattr(events[-1], "sequence_no", 0))
    created_at = getattr(record, "created_at", None)
    completed_at = getattr(record, "completed_at", None)
    terminal = status in {
        ExecutionRecordStatus.COMPLETED.value,
        ExecutionRecordStatus.FAILED.value,
    }
    route_lane: str | None = None
    route_recorded = False
    for event in tuple(events or ()):
        event_type = getattr(event, "event_type", None)
        if str(getattr(event_type, "value", event_type)) != "MODEL_ROUTE_RECORDED":
            continue
        route_recorded = True
        payload = getattr(event, "payload", {})
        if isinstance(payload, Mapping):
            candidate = payload.get("lane")
            if isinstance(candidate, str) and candidate.strip():
                route_lane = candidate.strip()[:16]
        break
    return {
        "executionId": str(getattr(record, "execution_id", "")),
        "taskKey": _record_task_key_payload(record),
        "mode": _enum_value(getattr(record, "mode", None)),
        "status": status,
        "replayable": bool(getattr(record, "replayable", False)),
        "historicalReplayAvailable": terminal,
        "recordSchemaVersion": str(getattr(record, "record_schema_version", "")),
        "executionContractVersion": str(
            getattr(record, "execution_contract_version", "")
        ),
        "sourceProvenanceVersion": str(
            getattr(record, "source_provenance_version", "")
        ),
        "sourceRevision": str(getattr(record, "source_revision", "")),
        "eventsReplayed": event_count,
        "lastSequenceNo": last_sequence,
        "routeRecorded": route_recorded,
        "modelRouteLane": route_lane,
        "createdAt": _jsonable(created_at),
        "completedAt": _jsonable(completed_at),
    }


class HistoricalReplayAccessService:
    """Authorize and expose X2-C without entering the live task path."""

    def __init__(
        self,
        historical_replay: HistoricalAgentReplayService,
        execution_records: Any,
        media_owner_reader: Any,
    ) -> None:
        if historical_replay is None:
            raise ValueError("historical replay service is required")
        if execution_records is None:
            raise ValueError("execution record source is required")
        if media_owner_reader is None:
            raise ValueError("media ownership reader is required")
        self._historical_replay = historical_replay
        self._execution_records = execution_records
        self._media_owner_reader = media_owner_reader

    async def metadata(
        self,
        execution_id: str,
        principal: Mapping[str, Any],
    ) -> dict[str, Any]:
        record = await self._authorized_record(execution_id, principal, require_owner=True)
        return historical_replay_metadata(record)

    async def read(
        self,
        execution_id: str,
        principal: Mapping[str, Any],
    ) -> HistoricalReplayView:
        record, trusted_key = await self._authorized_record_with_key(
            execution_id,
            principal,
            require_owner=True,
        )
        del record
        return await self._replay(execution_id, trusted_key)

    async def initiate(
        self,
        execution_id: str,
        principal: Mapping[str, Any],
    ) -> HistoricalReplayView:
        self._require_privileged(principal)
        # Resolve the durable record before X2-C so an unknown execution is a
        # stable 404 and never becomes a live re-analysis request.
        await self._authorized_record(execution_id, principal, require_owner=False)
        return await self._replay(execution_id, None)

    async def _replay(self, execution_id: str, trusted_key: Any) -> HistoricalReplayView:
        try:
            if trusted_key is None:
                result = await self._historical_replay.replay(execution_id)
            else:
                result = await self._historical_replay.replay(
                    execution_id,
                    expected_task_key=trusted_key,
                )
        except ReplayAccessError:
            raise
        except HistoricalReplayError:
            # X2-C's stable typed errors are mapped by the presentation layer;
            # their details never cross this application boundary.
            raise
        except Exception as error:
            raise ReplayAccessUnavailableError("historical replay execution failed") from error

        if not isinstance(result, HistoricalReplayResult):
            raise ReplayAccessUnavailableError("historical replay returned an invalid result")
        payload = historical_replay_payload(result)
        digest = str(payload["resultDigest"])
        return HistoricalReplayView(result=result, payload=payload, result_digest=digest)

    async def _authorized_record_with_key(
        self,
        execution_id: str,
        principal: Mapping[str, Any],
        *,
        require_owner: bool,
    ) -> tuple[Any, Any | None]:
        record = await self._authorized_record(
            execution_id,
            principal,
            require_owner=require_owner,
        )
        role, _user_id = self._principal(principal)
        if role in {"ADMIN", "OPERATOR"}:
            return record, None
        try:
            return record, _record_task_key(record)
        except ReplayAccessError:
            raise
        except Exception as error:
            raise ReplayAccessUnavailableError("execution record task identity is invalid") from error

    async def _authorized_record(
        self,
        execution_id: str,
        principal: Mapping[str, Any],
        *,
        require_owner: bool,
    ) -> Any:
        normalized_id = self._normalize_execution_id(execution_id)
        role, user_id = self._principal(principal)
        if not require_owner:
            if role not in {"ADMIN", "OPERATOR"}:
                raise ReplayAccessForbiddenError("privileged replay initiation is required")
        record = await self._load_record(normalized_id)
        if role in {"ADMIN", "OPERATOR"} or not require_owner:
            return record

        # The owner path must bind the full durable TaskKey to the existing
        # ownership abstraction.  A model-supplied or URL-supplied media id is
        # never used as the authorization source.
        try:
            task_key = _record_task_key(record)
        except ReplayAccessError:
            raise
        except Exception as error:
            raise ReplayAccessUnavailableError("execution record task identity is invalid") from error
        require_owned = getattr(self._media_owner_reader, "require_owned", None)
        if not callable(require_owned):
            raise ReplayAccessUnavailableError("media ownership boundary is unavailable")
        try:
            owned = require_owned(task_key.media_id, user_id)
            if inspect.isawaitable(owned):
                owned = await owned
        except Exception as error:
            # Do not disclose whether an execution exists to another user.
            raise ReplayAccessNotFoundError("historical execution is not owned by principal") from error
        if owned is None:
            raise ReplayAccessNotFoundError("historical execution ownership was not resolved")
        owner_id = getattr(owned, "user_id", None)
        if isinstance(owned, Mapping):
            owner_id = owned.get("user_id", owned.get("userId", owner_id))
        if owner_id is not None:
            try:
                if isinstance(owner_id, bool) or int(owner_id) != user_id:
                    raise ReplayAccessNotFoundError("historical execution is not owned by principal")
            except (TypeError, ValueError) as error:
                raise ReplayAccessNotFoundError("historical execution ownership was not resolved") from error
        return record

    async def _load_record(self, execution_id: str) -> Any:
        loader = getattr(self._execution_records, "load", None)
        if not callable(loader):
            loader = getattr(self._execution_records, "get", None)
        if not callable(loader):
            raise ReplayAccessUnavailableError("execution record source has no read operation")
        try:
            record = loader(execution_id)
            if inspect.isawaitable(record):
                record = await record
        except ExecutionRecordPersistenceError as error:
            raise ReplayAccessUnavailableError("execution record read failed") from error
        except Exception as error:
            raise ReplayAccessUnavailableError("execution record read failed") from error
        if record is None:
            raise ReplayAccessNotFoundError("historical execution was not found")
        return record

    @staticmethod
    def _normalize_execution_id(execution_id: str) -> str:
        if not isinstance(execution_id, str):
            raise ReplayAccessNotFoundError("execution id is invalid")
        normalized = execution_id.strip()
        if not normalized or len(normalized) > 256:
            raise ReplayAccessNotFoundError("execution id is invalid")
        return normalized

    @staticmethod
    def _principal(principal: Mapping[str, Any]) -> tuple[str, int]:
        if not isinstance(principal, Mapping):
            raise ReplayAccessForbiddenError("authenticated principal is invalid")
        raw_id = principal.get("id")
        try:
            if isinstance(raw_id, bool):
                raise ValueError
            user_id = int(raw_id)
        except (TypeError, ValueError) as error:
            raise ReplayAccessForbiddenError("authenticated principal is invalid") from error
        if user_id < 1:
            raise ReplayAccessForbiddenError("authenticated principal is invalid")
        role = str(principal.get("role", "USER")).strip().upper() or "USER"
        return role, user_id

    @classmethod
    def _require_privileged(cls, principal: Mapping[str, Any]) -> None:
        role, _user_id = cls._principal(principal)
        if role not in {"ADMIN", "OPERATOR"}:
            raise ReplayAccessForbiddenError("privileged replay initiation is required")


# Concise aliases for composition/tests that use the application terminology.
ReplayAccessService = HistoricalReplayAccessService
ReplayAccessNotFound = ReplayAccessNotFoundError
ReplayAccessForbidden = ReplayAccessForbiddenError
ReplayAccessUnavailable = ReplayAccessUnavailableError


__all__ = [
    "HistoricalReplayAccessService",
    "HistoricalReplayView",
    "ReplayAccessError",
    "ReplayAccessForbidden",
    "ReplayAccessForbiddenError",
    "ReplayAccessNotFound",
    "ReplayAccessNotFoundError",
    "ReplayAccessService",
    "ReplayAccessUnavailable",
    "ReplayAccessUnavailableError",
    "historical_replay_metadata",
    "historical_replay_payload",
]
