"""Redis-backed authentication and the original bounded AgentTelemetry view."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from dovideo.application.analysis_task_keys import goal_digest
from dovideo.application.value_objects import TaskKey
from dovideo.domain import BudgetUsage, TaskEvent, TaskStatusState

from .persistence.sqlalchemy import UserRecord


REDIS_SESSION_TTL_SECONDS = 24 * 60 * 60
REDIS_LOGIN_FAILURE_WINDOW_SECONDS = 10 * 60
REDIS_MAX_LOGIN_FAILURES = 8
REDIS_TRACE_TTL_SECONDS = 7 * 24 * 60 * 60
REDIS_MAX_RESPONSE_DIAGNOSTICS = 128
REDIS_MAX_RESPONSE_DIAGNOSTIC_BYTES = 8192


def _bearer(authorization: str | None) -> str | None:
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    return token or None


def _redis_int(value: Any) -> int:
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class RedisAuthService:
    """Java-compatible MySQL identity + Redis session auth boundary."""

    _ITERATIONS = 210_000

    def __init__(self, users: Any, client: Any) -> None:
        self.users = users
        self.client = client
        self.session_prefix = "auth:session"
        self.failure_prefix = "auth:login-failures"

    def register(self, username: str, password: str, nickname: str | None) -> Any:
        from dovideo.presentation.api.runtime import R1ServiceError
        from dovideo.presentation.api.schemas import AuthData, UserInfo

        normalized = username.strip()
        display_name = nickname.strip() if nickname and nickname.strip() else "用户"
        if not normalized:
            raise R1ServiceError("账号不能为空", status_code=400)
        try:
            user = self.users.create(
                normalized,
                self._hash_password(password),
                display_name,
            )
        except ValueError as exc:
            raise R1ServiceError("该账号已存在", status_code=409) from exc
        return AuthData(userInfo=UserInfo.model_validate(user.info()), token=None)

    def login(self, username: str, password: str) -> Any:
        from dovideo.presentation.api.runtime import R1ServiceError
        from dovideo.presentation.api.schemas import AuthData, UserInfo

        normalized = username.strip()
        failure_key = self._failure_key(normalized)
        current = _redis_int(self.client.get(failure_key))
        if current >= REDIS_MAX_LOGIN_FAILURES:
            raise R1ServiceError("登录尝试过于频繁，请稍后再试", status_code=429)
        user = self.users.get_by_username(normalized)
        if user is None or not self._matches(password, user.password_hash):
            count = int(self.client.incr(failure_key))
            if count == 1:
                self.client.expire(failure_key, REDIS_LOGIN_FAILURE_WINDOW_SECONDS)
            raise R1ServiceError("账号或密码错误", status_code=401)
        self.client.delete(failure_key)
        token = secrets.token_urlsafe(32)
        self.client.set(self._session_key(token), str(user.user_id), ex=REDIS_SESSION_TTL_SECONDS)
        return AuthData(userInfo=UserInfo.model_validate(user.info()), token=token)

    def require(self, authorization: str | None) -> dict[str, Any]:
        from dovideo.presentation.api.runtime import R1ServiceError

        token = _bearer(authorization)
        if token is None:
            raise R1ServiceError("请先登录", status_code=401)
        raw_user_id = self.client.get(self._session_key(token))
        if raw_user_id is None:
            raise R1ServiceError("登录状态已失效", status_code=401)
        user = None
        try:
            user = self.users.get(_redis_int(raw_user_id))
        except (TypeError, ValueError):
            pass
        if user is None:
            self.client.delete(self._session_key(token))
            raise R1ServiceError("登录状态已失效", status_code=401)
        return user.info()

    def logout(self, authorization: str | None) -> None:
        token = _bearer(authorization)
        if token is not None:
            self.client.delete(self._session_key(token))

    def _session_key(self, token: str) -> str:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"{self.session_prefix}:{digest}"

    def _failure_key(self, username: str) -> str:
        digest = hashlib.sha256(username.encode("utf-8")).hexdigest()
        return f"{self.failure_prefix}:{digest}"

    @classmethod
    def _hash_password(cls, password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, cls._ITERATIONS, dklen=32
        )
        encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
        return f"pbkdf2${cls._ITERATIONS}${encode(salt)}${encode(digest)}"

    @classmethod
    def _matches(cls, password: str, stored: str) -> bool:
        try:
            prefix, iterations, encoded_salt, encoded_digest = stored.split("$", 3)
            if prefix != "pbkdf2":
                return False
            salt = base64.urlsafe_b64decode(encoded_salt + "=" * (-len(encoded_salt) % 4))
            expected = base64.urlsafe_b64decode(encoded_digest + "=" * (-len(encoded_digest) % 4))
            actual = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt, int(iterations), dklen=32
            )
            return hmac.compare_digest(actual, expected)
        except (TypeError, ValueError):
            return False


class RedisTraceStore:
    """Persist the original AgentTelemetry baseline in Redis for seven days."""

    def __init__(self, client: Any, *, ttl_seconds: int = REDIS_TRACE_TTL_SECONDS) -> None:
        if ttl_seconds <= 0:
            raise ValueError("trace TTL must be positive")
        self.client = client
        self.ttl_seconds = int(ttl_seconds)
        self.trace_prefix = "agent:trace"
        self.latest_prefix = "agent:trace:latest"
        self.index_prefix = "agent:trace:tasks"

    def _trace_key(self, trace_id: str) -> str:
        return f"{self.trace_prefix}:{trace_id}"

    def _latest_key(self, key: TaskKey) -> str:
        return f"{self.latest_prefix}:{_task_suffix(key)}"

    def _index_key(self, media_id: int) -> str:
        return f"{self.index_prefix}:{int(media_id)}"

    def start(self, key: TaskKey) -> str:
        trace_id = str(uuid4())
        payload = {
            "traceId": trace_id,
            "taskId": int(key.media_id),
            "goalDigest": goal_digest(key.goal, key.mode),
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "stageDurationMs": {},
            "counters": {"taskSubmissions": 1},
            "values": {},
            "estimatedCost": 0.0,
        }
        redis_key = self._trace_key(trace_id)
        self._write_document(redis_key, payload)
        self.client.set(self._latest_key(key), trace_id, ex=self.ttl_seconds)
        index_key = self._index_key(key.media_id)
        self.client.sadd(index_key, trace_id)
        self.client.expire(index_key, self.ttl_seconds)
        return trace_id

    def record(self, key: TaskKey, event: TaskEvent) -> None:
        trace_id = self._decode(self.client.get(self._latest_key(key)))
        if not trace_id:
            return
        redis_key = self._trace_key(trace_id)
        raw = self.client.hgetall(redis_key)
        if not raw:
            return
        document = self._document(raw)
        stage = None if event.stage is None else event.stage.value
        if stage:
            durations = document.setdefault("stageDurationMs", {})
            durations.setdefault(stage, 0)
            counters = document.setdefault("counters", {})
            counters[f"{stage}Calls"] = int(counters.get(f"{stage}Calls", 0)) + 1
        values = document.setdefault("values", {})
        if event.state is TaskStatusState.COMPLETED:
            values["terminal"] = 1.0
        elif event.state is TaskStatusState.FAILED:
            values["terminal"] = 0.0
        self._write_document(redis_key, document)

    def latest(self, key: TaskKey) -> dict[str, Any]:
        trace_id = self._decode(self.client.get(self._latest_key(key)))
        if not trace_id:
            return {}
        raw = self.client.hgetall(self._trace_key(trace_id))
        return {} if not raw else self._document(raw)

    def increment_for_key(self, key: TaskKey, metric: str, amount: int = 1) -> None:
        """Increment one baseline counter in the current Redis trace.

        The original trace store already persisted lifecycle events.  These
        small mutation methods let the existing application telemetry hooks
        retain provider/media counters in the same seven-day trace document;
        they do not introduce a second trace repository.
        """

        if not isinstance(metric, str) or not metric.strip():
            return
        trace_id, document = self._latest_document(key)
        if trace_id is None or document is None:
            return
        counters = document.setdefault("counters", {})
        counters[metric.strip()] = int(counters.get(metric.strip(), 0)) + int(amount)
        self._write_document(self._trace_key(trace_id), document)

    def observe_for_key(self, key: TaskKey, metric: str, value: float) -> None:
        """Record one finite baseline numeric value in the current trace."""

        if not isinstance(metric, str) or not metric.strip():
            return
        numeric = float(value)
        if not math.isfinite(numeric):
            return
        trace_id, document = self._latest_document(key)
        if trace_id is None or document is None:
            return
        document.setdefault("values", {})[metric.strip()] = numeric
        self._write_document(self._trace_key(trace_id), document)

    def record_structural_for_key(
        self,
        key: TaskKey,
        diagnostic: Mapping[str, Any],
        *,
        max_entries: int = REDIS_MAX_RESPONSE_DIAGNOSTICS,
        max_bytes: int = REDIS_MAX_RESPONSE_DIAGNOSTIC_BYTES,
    ) -> None:
        """Append one bounded, already-sanitized response-shape record.

        This extends the existing trace document only.  Callers must provide
        structure without prompts, response text, credentials, or headers;
        this boundary enforces entry count and serialized byte limits.
        """

        if not isinstance(diagnostic, Mapping) or max_entries <= 0 or max_bytes <= 0:
            return
        trace_id, document = self._latest_document(key)
        if trace_id is None or document is None:
            return
        entry = dict(diagnostic)
        try:
            encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return
        if len(encoded.encode("utf-8")) > max_bytes:
            entry = {
                "truncated": True,
                "diagnosticBytes": len(encoded.encode("utf-8")),
            }
        current = document.get("responseDiagnostics", [])
        if not isinstance(current, list):
            current = []
        current.append(entry)
        document["responseDiagnostics"] = current[-int(max_entries) :]
        self._write_document(self._trace_key(trace_id), document)

    def record_execution_projection(
        self,
        key: TaskKey,
        *,
        execution_id: str,
        status: str,
        event_type: str | None,
        latest_sequence: int,
        recorded_semantic_events: int,
    ) -> None:
        """Project only bounded execution identity/status into the trace.

        Durable execution events remain the historical authority.  This
        method intentionally stores no event payload, model DTO, query, or
        tool result body in Redis.
        """

        if not execution_id or not status:
            return
        trace_id, document = self._latest_document(key)
        if trace_id is None or document is None:
            return
        document["executionId"] = str(execution_id)[:96]
        document["executionRecordStatus"] = str(status)[:32]
        document["latestRecordedSequence"] = max(0, int(latest_sequence))
        document["recordedSemanticEvents"] = max(0, int(recorded_semantic_events))
        if event_type:
            document["lastRecordedEventType"] = str(event_type)[:64]
        self._write_document(self._trace_key(trace_id), document)

    def add_usage_for_key(
        self,
        key: TaskKey,
        *,
        estimated_tokens: int | float = 0,
        estimated_cost: float = 0.0,
    ) -> BudgetUsage:
        """Add provider-reported usage to the current baseline trace."""

        tokens = int(float(estimated_tokens))
        cost = float(estimated_cost)
        if tokens < 0 or cost < 0 or not math.isfinite(cost):
            raise ValueError("trace usage must be finite and non-negative")
        trace_id, document = self._latest_document(key)
        if trace_id is None or document is None:
            return BudgetUsage()
        current_tokens = int(document.get("estimatedTokens", 0))
        current_cost = float(document.get("estimatedCost", 0.0))
        usage = BudgetUsage(
            estimatedTokens=current_tokens + tokens,
            estimatedCost=current_cost + cost,
        )
        document["estimatedTokens"] = usage.estimated_tokens
        document["estimatedCost"] = usage.estimated_cost
        self._write_document(self._trace_key(trace_id), document)
        return usage

    def current_usage_for_key(self, key: TaskKey) -> BudgetUsage:
        """Read cumulative provider usage from the current baseline trace."""

        _trace_id, document = self._latest_document(key)
        if document is None:
            return BudgetUsage()
        return BudgetUsage(
            estimatedTokens=document.get("estimatedTokens", 0),
            estimatedCost=document.get("estimatedCost", 0.0),
        )

    def _latest_document(
        self, key: TaskKey
    ) -> tuple[str | None, dict[str, Any] | None]:
        trace_id = self._decode(self.client.get(self._latest_key(key)))
        if not trace_id:
            return None, None
        raw = self.client.hgetall(self._trace_key(trace_id))
        return trace_id, None if not raw else self._document(raw)

    def delete_media(self, media_id: int) -> None:
        index = self._index_key(media_id)
        values = self.client.smembers(index) or ()
        keys = [index]
        keys.extend(self._trace_key(self._decode(value) or "") for value in values)
        self.client.delete(*keys)
        pattern = f"{self.latest_prefix}:{int(media_id)}:*"
        latest_keys = tuple(self.client.scan_iter(match=pattern))
        if latest_keys:
            self.client.delete(*latest_keys)

    def _write_document(self, key: str, document: dict[str, Any]) -> None:
        mapping = {
            name: json.dumps(value, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else str(value)
            for name, value in document.items()
        }
        self.client.hset(key, mapping=mapping)
        self.client.expire(key, self.ttl_seconds)

    @classmethod
    def _document(cls, raw: dict[Any, Any]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for name, value in raw.items():
            name_text = cls._decode(name) or ""
            value_text = cls._decode(value) or ""
            if name_text in {
                "stageDurationMs",
                "counters",
                "values",
                "responseDiagnostics",
            }:
                try:
                    document[name_text] = json.loads(value_text)
                except (TypeError, ValueError):
                    document[name_text] = {}
            elif name_text == "taskId":
                document[name_text] = _redis_int(value_text)
            elif name_text == "estimatedTokens":
                document[name_text] = _redis_int(value_text)
            elif name_text == "estimatedCost":
                try:
                    document[name_text] = float(value_text)
                except (TypeError, ValueError):
                    document[name_text] = 0.0
            elif name_text in {
                "latestRecordedSequence",
                "recordedSemanticEvents",
            }:
                document[name_text] = _redis_int(value_text)
            else:
                document[name_text] = value_text
        return document

    @staticmethod
    def _decode(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)


def _task_suffix(key: TaskKey) -> str:
    return f"{int(key.media_id)}:{goal_digest(key.goal, key.mode)}"


RedisAgentTelemetry = RedisTraceStore


__all__ = [
    "REDIS_LOGIN_FAILURE_WINDOW_SECONDS",
    "REDIS_MAX_LOGIN_FAILURES",
    "REDIS_MAX_RESPONSE_DIAGNOSTIC_BYTES",
    "REDIS_MAX_RESPONSE_DIAGNOSTICS",
    "REDIS_SESSION_TTL_SECONDS",
    "REDIS_TRACE_TTL_SECONDS",
    "RedisAgentTelemetry",
    "RedisAuthService",
    "RedisTraceStore",
]
