"""Celery/RabbitMQ transport adapters for R3.

Only this module knows about Celery and RabbitMQ.  The application receives a
provider-neutral ``TaskTransportPort`` and the existing ``TaskWorker`` returns
a provider-neutral ``WorkerOutcome``.  Messages are explicit JSON documents;
no pickle, ORM instance, service object, or exception object crosses the
broker boundary.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from kombu import Connection, Exchange, Producer, Queue
from pydantic import ConfigDict, Field, field_validator
from pydantic import BaseModel

from dovideo.application.analysis_task_keys import normalize_content_hash
from dovideo.application.ports.tasks import TaskTransportPort
from dovideo.application.value_objects import AnalysisRequest
from dovideo.domain import AnalysisMode


DEFAULT_ANALYSIS_QUEUE = "dovideo.analysis"
DEFAULT_ANALYSIS_EXCHANGE = "dovideo.analysis"
DEFAULT_ANALYSIS_ROUTING_KEY = "dovideo.analysis"
DEFAULT_DEAD_LETTER_EXCHANGE = "dovideo.analysis.dlx"
DEFAULT_DEAD_LETTER_QUEUE = "dovideo.analysis.dlq"
DEFAULT_DEAD_LETTER_ROUTING_KEY = "dovideo.analysis.dead"
DEFAULT_TASK_NAME = "dovideo.analysis.deliver"
DEFAULT_MAX_ENVELOPE_BYTES = 64 * 1024


class R3ConfigurationError(RuntimeError):
    """Raised when the production Celery/RabbitMQ boundary is incomplete."""


class TransportEnqueueError(RuntimeError):
    """Safe application-independent description of a broker enqueue failure."""


class CeleryAnalysisEnvelope(BaseModel):
    """Bounded JSON task contract carried by Celery."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    media_id: int = Field(alias="mediaId")
    goal: str = Field(min_length=1, max_length=500)
    mode: AnalysisMode = AnalysisMode.GENERAL
    source: str = Field(min_length=1, max_length=4096)
    filename: str | None = Field(default=None, max_length=512)
    content_hash: str | None = Field(default=None, alias="contentHash", max_length=128)
    status: str | None = Field(default=None, max_length=64)
    request_id: str | None = Field(default=None, alias="requestId", max_length=128)
    action: str = Field(default="START_ANALYSIS", max_length=32)

    @field_validator("media_id")
    @classmethod
    def _valid_media_id(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("mediaId must be a non-negative integer")
        return value

    @field_validator("goal", "source")
    @classmethod
    def _trim_required_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("filename", "content_hash", "status", "request_id")
    @classmethod
    def _trim_optional_text(cls, value: str | None) -> str | None:
        return None if value is None else value.strip() or None

    @field_validator("action")
    @classmethod
    def _supported_action(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"START_ANALYSIS", "REVISE_ANALYSIS"}:
            raise ValueError("unsupported analysis action")
        return normalized

    @classmethod
    def from_request(
        cls,
        request: AnalysisRequest,
        *,
        action: str = "START_ANALYSIS",
    ) -> "CeleryAnalysisEnvelope":
        if not isinstance(request, AnalysisRequest):
            raise TypeError("request must be an AnalysisRequest")
        return cls(
            mediaId=request.media.media_id,
            goal=request.goal,
            mode=request.mode,
            source=request.media.source,
            filename=request.media.filename,
            contentHash=request.media.content_hash,
            status=request.media.status,
            requestId=request.request_id,
            action=action,
        )

    def to_request(self) -> AnalysisRequest:
        """Rebuild the application request without importing broker classes."""

        from dovideo.application.value_objects import MediaRef

        return AnalysisRequest(
            media=MediaRef(
                media_id=self.media_id,
                source=self.source,
                filename=self.filename,
                content_hash=self.content_hash,
                status=self.status,
            ),
            goal=self.goal,
            mode=self.mode,
            request_id=self.request_id,
        )

    def as_message(self, *, max_bytes: int = DEFAULT_MAX_ENVELOPE_BYTES) -> dict[str, Any]:
        """Return a JSON-compatible, bounded payload for ``send_task``."""

        payload = self.model_dump(mode="json", by_alias=True)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > max_bytes:
            raise ValueError("analysis message envelope exceeds its size bound")
        return payload


@dataclass(frozen=True, slots=True)
class CeleryTransportSettings:
    """Explicit RabbitMQ topology and Celery delivery settings."""

    broker_url: str
    queue: str = DEFAULT_ANALYSIS_QUEUE
    exchange: str = DEFAULT_ANALYSIS_EXCHANGE
    routing_key: str = DEFAULT_ANALYSIS_ROUTING_KEY
    dead_letter_exchange: str = DEFAULT_DEAD_LETTER_EXCHANGE
    dead_letter_queue: str = DEFAULT_DEAD_LETTER_QUEUE
    dead_letter_routing_key: str = DEFAULT_DEAD_LETTER_ROUTING_KEY
    task_name: str = DEFAULT_TASK_NAME
    retry_countdown_seconds: float = 0.25
    locked_countdown_seconds: float = 5.0
    connect_timeout_seconds: float = 5.0
    max_envelope_bytes: int = DEFAULT_MAX_ENVELOPE_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.broker_url, str) or not self.broker_url.strip():
            raise R3ConfigurationError("RabbitMQ broker URL is required")
        parsed = urlsplit(self.broker_url.strip())
        if parsed.scheme not in {"amqp", "amqps"} or not parsed.netloc:
            raise R3ConfigurationError("R3 broker URL must be an amqp(s) RabbitMQ URL")
        for name in (
            "queue",
            "exchange",
            "routing_key",
            "dead_letter_exchange",
            "dead_letter_queue",
            "dead_letter_routing_key",
            "task_name",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 255:
                raise R3ConfigurationError(f"R3 {name} is invalid")
            object.__setattr__(self, name, value.strip())
        for name in ("retry_countdown_seconds", "locked_countdown_seconds", "connect_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise R3ConfigurationError(f"R3 {name} is invalid")
            object.__setattr__(self, name, float(value))
        if self.connect_timeout_seconds <= 0:
            raise R3ConfigurationError("R3 connect timeout must be positive")
        if not math.isfinite(self.locked_countdown_seconds) or self.locked_countdown_seconds < 1:
            raise R3ConfigurationError("R3 locked retry delay must be finite and at least one second")
        if isinstance(self.max_envelope_bytes, bool) or not isinstance(self.max_envelope_bytes, int):
            raise R3ConfigurationError("R3 envelope size must be an integer")
        if self.max_envelope_bytes < 1024:
            raise R3ConfigurationError("R3 envelope size is too small")

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        require_production: bool = True,
    ) -> "CeleryTransportSettings":
        values = os.environ if environ is None else environ
        profile = _value(values, "DOVIDEO_PROFILE") or "local"
        if require_production and profile.casefold() != "production":
            raise R3ConfigurationError(
                "R3 production requires DOVIDEO_PROFILE=production; no local transport fallback is allowed"
            )
        broker_url = _first_value(
            values,
            "DOVIDEO_BROKER_URL",
            "DOVIDEO_RABBITMQ_URL",
            "CELERY_BROKER_URL",
        )
        if broker_url is None:
            raise R3ConfigurationError("R3 RabbitMQ broker URL is missing")
        return cls(
            broker_url=broker_url,
            queue=_first_value(values, "DOVIDEO_CELERY_QUEUE") or DEFAULT_ANALYSIS_QUEUE,
            exchange=_first_value(values, "DOVIDEO_CELERY_EXCHANGE") or DEFAULT_ANALYSIS_EXCHANGE,
            routing_key=_first_value(values, "DOVIDEO_CELERY_ROUTING_KEY") or DEFAULT_ANALYSIS_ROUTING_KEY,
            dead_letter_exchange=(
                _first_value(values, "DOVIDEO_CELERY_DLX") or DEFAULT_DEAD_LETTER_EXCHANGE
            ),
            dead_letter_queue=(
                _first_value(values, "DOVIDEO_CELERY_DLQ") or DEFAULT_DEAD_LETTER_QUEUE
            ),
            dead_letter_routing_key=(
                _first_value(values, "DOVIDEO_CELERY_DL_ROUTING_KEY")
                or DEFAULT_DEAD_LETTER_ROUTING_KEY
            ),
            task_name=_first_value(values, "DOVIDEO_CELERY_TASK_NAME") or DEFAULT_TASK_NAME,
            retry_countdown_seconds=_float_value(
                values,
                "DOVIDEO_CELERY_RETRY_COUNTDOWN_SECONDS",
                0.25,
                minimum=0.0,
            ),
            locked_countdown_seconds=_float_value(
                values, "DOVIDEO_CELERY_LOCKED_COUNTDOWN_SECONDS", 5.0, minimum=1.0,
            ),
            connect_timeout_seconds=_float_value(
                values,
                "DOVIDEO_CELERY_CONNECT_TIMEOUT_SECONDS",
                5.0,
                minimum=0.01,
            ),
            max_envelope_bytes=_int_value(
                values,
                "DOVIDEO_CELERY_MAX_ENVELOPE_BYTES",
                DEFAULT_MAX_ENVELOPE_BYTES,
                minimum=1024,
            ),
        )


class RabbitMQTopology:
    """Declare the durable analysis exchange/queue and explicit DLX/DLQ."""

    def __init__(self, settings: CeleryTransportSettings) -> None:
        self.settings = settings

    def ensure(self) -> None:
        settings = self.settings
        with Connection(
            settings.broker_url,
            connect_timeout=settings.connect_timeout_seconds,
        ) as connection:
            channel = connection.channel()
            main_exchange = Exchange(settings.exchange, type="direct", durable=True)
            dead_exchange = Exchange(
                settings.dead_letter_exchange,
                type="direct",
                durable=True,
            )
            main_exchange.declare(channel=channel)
            dead_exchange.declare(channel=channel)
            dead_queue = Queue(
                settings.dead_letter_queue,
                exchange=dead_exchange,
                routing_key=settings.dead_letter_routing_key,
                durable=True,
            )
            dead_queue.declare(channel=channel)
            main_queue = Queue(
                settings.queue,
                exchange=main_exchange,
                routing_key=settings.routing_key,
                durable=True,
                queue_arguments={
                    "x-dead-letter-exchange": settings.dead_letter_exchange,
                    "x-dead-letter-routing-key": settings.dead_letter_routing_key,
                },
            )
            main_queue.declare(channel=channel)

    def queue_stats(self) -> dict[str, int]:
        """Read main/DLQ message and consumer counts without consuming data."""

        settings = self.settings
        with Connection(
            settings.broker_url,
            connect_timeout=settings.connect_timeout_seconds,
        ) as connection:
            channel = connection.channel()
            main = channel.queue_declare(queue=settings.queue, passive=True)
            dead = channel.queue_declare(queue=settings.dead_letter_queue, passive=True)
            return {
                "mainMessages": int(main.message_count),
                "mainConsumers": int(main.consumer_count),
                "deadLetterMessages": int(dead.message_count),
                "deadLetterConsumers": int(dead.consumer_count),
            }


def create_celery_app(settings: CeleryTransportSettings):
    """Create a JSON-only Celery app without contacting RabbitMQ."""

    from celery import Celery

    main_exchange = Exchange(settings.exchange, type="direct", durable=True)
    dead_exchange = Exchange(settings.dead_letter_exchange, type="direct", durable=True)
    main_queue = Queue(
        settings.queue,
        exchange=main_exchange,
        routing_key=settings.routing_key,
        durable=True,
        queue_arguments={
            "x-dead-letter-exchange": settings.dead_letter_exchange,
            "x-dead-letter-routing-key": settings.dead_letter_routing_key,
        },
    )
    dead_queue = Queue(
        settings.dead_letter_queue,
        exchange=dead_exchange,
        routing_key=settings.dead_letter_routing_key,
        durable=True,
    )
    app = Celery("dovideo-r3", broker=settings.broker_url, backend=None)
    app.conf.update(
        task_serializer="json",
        accept_content=("json",),
        result_serializer="json",
        task_ignore_result=True,
        task_acks_late=True,
        task_acks_on_failure_or_timeout=False,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_create_missing_queues=False,
        task_default_queue=settings.queue,
        task_default_exchange=settings.exchange,
        task_default_exchange_type="direct",
        task_default_routing_key=settings.routing_key,
        task_queues=(main_queue, dead_queue),
        task_routes={
            settings.task_name: {
                "queue": settings.queue,
                "exchange": settings.exchange,
                "routing_key": settings.routing_key,
            }
        },
        broker_connection_retry_on_startup=True,
        broker_heartbeat=30,
    )
    app._dovideo_r3_settings = settings  # type: ignore[attr-defined]
    return app


class CeleryTaskTransport(TaskTransportPort):
    """Publish one validated envelope to the configured durable queue."""

    def __init__(self, app: Any, settings: CeleryTransportSettings) -> None:
        if app is None:
            raise ValueError("a Celery application is required")
        self.app = app
        self.settings = settings

    async def enqueue(self, request: AnalysisRequest) -> None:
        try:
            envelope = CeleryAnalysisEnvelope.from_request(request)
            payload = envelope.as_message(max_bytes=self.settings.max_envelope_bytes)
            await asyncio.to_thread(self._send, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TransportEnqueueError("analysis message enqueue failed") from exc

    def _send(self, payload: dict[str, Any]) -> None:
        self.app.send_task(
            self.settings.task_name,
            args=[payload],
            queue=self.settings.queue,
            exchange=self.settings.exchange,
            routing_key=self.settings.routing_key,
            serializer="json",
            retry=False,
            ignore_result=True,
        )


class RabbitMQDeadLetterPublisher:
    """Durable failure ledger plus explicit RabbitMQ DLQ publisher."""

    def __init__(
        self,
        settings: CeleryTransportSettings,
        *,
        failed_task_store: Any | None = None,
        redis_client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.failed_task_store = failed_task_store
        self.redis_client = redis_client
        self.topology = RabbitMQTopology(settings)

    async def publish(
        self,
        request: AnalysisRequest,
        *,
        attempt: int,
        error: BaseException,
    ) -> None:
        if not isinstance(request, AnalysisRequest):
            raise TypeError("request must be an AnalysisRequest")
        await asyncio.to_thread(self._publish_business_failure, request, attempt, error)

    async def publish_poison(self, payload: Any, error: BaseException) -> None:
        """Publish a bounded descriptor for an invalid/poison task message."""

        await asyncio.to_thread(self._publish_poison, payload, error)

    def _publish_business_failure(
        self,
        request: AnalysisRequest,
        attempt: int,
        error: BaseException,
    ) -> None:
        self._record_failure_once(request, attempt, error)
        envelope = CeleryAnalysisEnvelope.from_request(request)
        message = {
            "schema": "dovideo.analysis.dead-letter.v1",
            "kind": "business-failure",
            "task": envelope.as_message(max_bytes=self.settings.max_envelope_bytes),
            "attempt": max(1, int(attempt)),
            "error": _error_document(error),
        }
        self._publish_message(message, message_id=_message_id(request, attempt))

    def _publish_poison(self, payload: Any, error: BaseException) -> None:
        descriptor = _payload_descriptor(payload)
        message = {
            "schema": "dovideo.analysis.dead-letter.v1",
            "kind": "poison-message",
            "payload": descriptor,
            "attempt": 0,
            "error": _error_document(error),
        }
        self._publish_message(message, message_id="poison-" + _stable_digest(descriptor))

    def _record_failure_once(
        self,
        request: AnalysisRequest,
        attempt: int,
        error: BaseException,
    ) -> None:
        store = self.failed_task_store
        if store is None:
            return
        marker = (
            f"r3:failed-task:{request.media.media_id}:"
            f"{_stable_digest({'goal': request.goal, 'mode': request.mode.value})}"
        )
        if self.redis_client is not None:
            try:
                if self.redis_client.exists(marker):
                    return
            except Exception:
                # The durable SQL ledger remains authoritative; a Redis marker
                # outage must not suppress the ledger write.
                marker = ""
        from dovideo.infrastructure.persistence.sqlalchemy import FailedTaskRecord

        store.record(
            FailedTaskRecord(
                media_id=request.media.media_id,
                action="START_ANALYSIS",
                mode=request.mode.value,
                content_hash=normalize_content_hash(
                    request.media.media_id,
                    request.media.content_hash,
                ),
                user_goal=request.goal[:500],
                attempt_count=max(1, int(attempt)),
                error_type=type(error).__name__[:128],
                error_message=_safe_error_message(error, limit=1000),
                status="DEAD_LETTER_PENDING",
            )
        )
        if marker and self.redis_client is not None:
            try:
                self.redis_client.set(marker, "1", ex=7 * 24 * 60 * 60)
            except Exception:
                pass

    def _publish_message(self, message: dict[str, Any], *, message_id: str) -> None:
        settings = self.settings
        with Connection(
            settings.broker_url,
            connect_timeout=settings.connect_timeout_seconds,
        ) as connection:
            channel = connection.channel()
            exchange = Exchange(
                settings.dead_letter_exchange,
                type="direct",
                durable=True,
            )
            queue = Queue(
                settings.dead_letter_queue,
                exchange=exchange,
                routing_key=settings.dead_letter_routing_key,
                durable=True,
            )
            producer = Producer(channel, serializer="json")
            producer.publish(
                message,
                exchange=exchange,
                routing_key=settings.dead_letter_routing_key,
                declare=[exchange, queue],
                serializer="json",
                retry=False,
                delivery_mode=2,
                headers={"schema": "dovideo.analysis.dead-letter.v1"},
                message_id=message_id,
            )


def _value(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name)
    return None if value is None or not value.strip() else value.strip()


def _first_value(values: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = _value(values, name)
        if value is not None:
            return value
    return None


def _float_value(
    values: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
) -> float:
    raw = _value(values, name)
    try:
        result = default if raw is None else float(raw)
    except (TypeError, ValueError) as exc:
        raise R3ConfigurationError("R3 numeric setting is invalid") from exc
    if result < minimum:
        raise R3ConfigurationError("R3 numeric setting is outside its bound")
    return result


def _int_value(
    values: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
) -> int:
    raw = _value(values, name)
    try:
        result = default if raw is None else int(raw)
    except (TypeError, ValueError) as exc:
        raise R3ConfigurationError("R3 integer setting is invalid") from exc
    if result < minimum:
        raise R3ConfigurationError("R3 integer setting is outside its bound")
    return result


def _safe_error_message(error: BaseException, *, limit: int = 512) -> str:
    return _safe_text(str(error), limit=limit)


def _safe_text(value: Any, *, limit: int) -> str:
    text = str(value)
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"(?i)(api[_-]?key|password|secret)(\s*[=:]\s*)[^\s,;]+", r"\1=[redacted]", text)
    text = "".join(character if ord(character) >= 32 or character in "\r\n\t" else " " for character in text)
    return text[:limit]


def _error_document(error: BaseException) -> dict[str, Any]:
    document: dict[str, Any] = {
        "type": type(error).__name__[:128],
        "message": _safe_error_message(error),
    }
    diagnostic = getattr(error, "diagnostic", None)
    if isinstance(diagnostic, str) and diagnostic:
        # The provider adapter has already removed response values.  Apply a
        # second transport-boundary cap so a diagnostic cannot enlarge a
        # durable DLQ message without bound or replace the public error type.
        document["diagnostic"] = _safe_text(diagnostic, limit=2048)
    return document


def _payload_descriptor(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        keys = sorted(str(key)[:128] for key in payload.keys())[:64]
        return {"type": "object", "keys": keys}
    if isinstance(payload, list):
        return {"type": "array", "length": min(len(payload), 100000)}
    if payload is None:
        return {"type": "null"}
    return {"type": type(payload).__name__[:64]}


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _message_id(request: AnalysisRequest, attempt: int) -> str:
    return f"analysis-{request.media.media_id}-{_stable_digest({'goal': request.goal, 'mode': request.mode.value})}-{int(attempt)}"


__all__ = [
    "CeleryAnalysisEnvelope",
    "CeleryTaskTransport",
    "CeleryTransportSettings",
    "DEFAULT_ANALYSIS_EXCHANGE",
    "DEFAULT_ANALYSIS_QUEUE",
    "DEFAULT_ANALYSIS_ROUTING_KEY",
    "DEFAULT_DEAD_LETTER_EXCHANGE",
    "DEFAULT_DEAD_LETTER_QUEUE",
    "DEFAULT_DEAD_LETTER_ROUTING_KEY",
    "DEFAULT_MAX_ENVELOPE_BYTES",
    "DEFAULT_TASK_NAME",
    "RabbitMQDeadLetterPublisher",
    "RabbitMQTopology",
    "R3ConfigurationError",
    "TransportEnqueueError",
    "create_celery_app",
]
