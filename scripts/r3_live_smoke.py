"""Bounded live R3 proof over the real local RabbitMQ/Celery worker.

The script uses only generated/local R2 infrastructure credentials from the
ignored environment file.  It never loads generic provider/API-key variables,
and it prints topology and outcome markers rather than task payloads.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from celery import __version__ as CELERY_VERSION
from kombu import Connection, Consumer, Exchange, Queue

from dovideo.application import (
    AgentCheckpointService,
    AnalysisRequest,
    DispatchDisposition,
    MediaRef,
    TaskDispatchService,
    TaskKey,
)
from dovideo.application.analysis_task_keys import goal_digest
from dovideo.domain import AnalysisMode, TaskStatusState, VideoContext, VideoSegment
from dovideo.infrastructure.celery_runtime import R3RedisTaskEventPublisher
from dovideo.infrastructure.celery_transport import (
    CeleryTransportSettings,
    CeleryTaskTransport,
    create_celery_app,
    RabbitMQTopology,
)
from dovideo.infrastructure.r2_config import create_r2_infrastructure
from dovideo.infrastructure.persistence.task_lifecycle import CheckpointTaskLifecycleStore


_ALLOWED_ENV = {
    "DOVIDEO_PROFILE",
    "DOVIDEO_R2_DATA_ROOT",
    "DOVIDEO_DATABASE_URL",
    "DOVIDEO_REDIS_URL",
    "DOVIDEO_MINIO_ENDPOINT",
    "DOVIDEO_MINIO_ACCESS_KEY",
    "DOVIDEO_MINIO_SECRET_KEY",
    "DOVIDEO_MINIO_BUCKET",
    "DOVIDEO_MINIO_SECURE",
    "DOVIDEO_QDRANT_URL",
    "DOVIDEO_QDRANT_API_KEY",
    "DOVIDEO_QDRANT_COLLECTION",
    "MYSQL_ROOT_PASSWORD",
    "MYSQL_APP_USER",
    "DB_PASSWORD",
    "REDIS_PASSWORD",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
    "QDRANT_API_KEY",
    "DOVIDEO_BROKER_URL",
    "RABBITMQ_DEFAULT_USER",
    "RABBITMQ_DEFAULT_PASS",
}


def load_local_infrastructure_environment(root: Path) -> None:
    """Load only the ignored local-infrastructure allow-list."""

    path = root / ".env.r2.local"
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if name in _ALLOWED_ENV:
            os.environ[name] = value.strip().strip('"').strip("'")


def _request(media_id: int, goal: str) -> AnalysisRequest:
    return AnalysisRequest(
        MediaRef(
            media_id,
            f"minio://r3-smoke/{media_id}.mp4",
            filename=f"{media_id}.mp4",
            content_hash=(f"{media_id:032x}"[-32:]),
        ),
        goal,
        AnalysisMode.GENERAL,
        request_id=f"r3-{media_id}",
    )


async def _seed_context(checkpoint: AgentCheckpointService, request: AnalysisRequest) -> None:
    await checkpoint.save_context(
        request.media.media_id,
        VideoContext(
            source=request.media.source,
            userGoal=request.goal,
            segments=(
                VideoSegment(
                    startMs=0,
                    endMs=30_000,
                    transcript="The saved R3 temporal evidence is in the first window.",
                ),
                VideoSegment(
                    startMs=300_000,
                    endMs=330_000,
                    transcript="A second saved temporal window proves cross-process context recovery.",
                ),
            ),
        ),
    )


async def _wait_for(
    predicate,
    *,
    timeout_seconds: float = 30.0,
    interval_seconds: float = 0.25,
):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(interval_seconds)
    raise TimeoutError("R3 live condition timed out")


def _counter_key(request: AnalysisRequest) -> str:
    return (
        f"r3:smoke:agent-invocations:{request.media.media_id}:"
        f"{goal_digest(request.goal, request.mode)}"
    )


async def _lifecycle_state(store, request):
    return await store.load_lifecycle(request.task_key)


async def _wait_terminal(store, request):
    async def terminal_state():
        state = await _lifecycle_state(store, request)
        return None if state is None or not state.terminal else state

    return await _wait_for(
        terminal_state,
        timeout_seconds=45.0,
    )


def _start_worker(root: Path, settings: CeleryTransportSettings) -> subprocess.Popen[Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "dovideo.infrastructure.celery_worker:celery_app",
        "worker",
        "--loglevel=WARNING",
        "--pool=solo",
        "--concurrency=1",
        "-Q",
        settings.queue,
    ]
    return subprocess.Popen(
        command,
        cwd=str(root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_worker(process: subprocess.Popen[Any] | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _crash_worker(process: subprocess.Popen[Any] | None) -> None:
    """Abruptly terminate an in-flight worker so RabbitMQ must redeliver."""

    if process is None:
        return
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def _drain_dead_letters(settings: CeleryTransportSettings) -> tuple[str, ...]:
    values: list[str] = []
    exchange = Exchange(settings.dead_letter_exchange, type="direct", durable=True)
    queue = Queue(
        settings.dead_letter_queue,
        exchange=exchange,
        routing_key=settings.dead_letter_routing_key,
        durable=True,
    )
    with Connection(
        settings.broker_url,
        connect_timeout=settings.connect_timeout_seconds,
    ) as connection:
        channel = connection.channel()

        def callback(body: Any, message: Any) -> None:
            if isinstance(body, (bytes, bytearray, str)):
                try:
                    body = json.loads(body)
                except (TypeError, ValueError):
                    body = None
            if isinstance(body, dict):
                values.append(str(body.get("kind", "unknown")))
            else:
                values.append("unknown")
            message.ack()

        consumer = Consumer(channel, queues=[queue], callbacks=[callback], accept=["json"])
        consumer.consume()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                connection.drain_events(timeout=0.25)
            except socket.timeout:
                break
        consumer.cancel()
    return tuple(values)


async def _run(root: Path) -> None:
    load_local_infrastructure_environment(root)
    os.environ["DOVIDEO_PROFILE"] = "production"
    os.environ["DOVIDEO_R3_DETERMINISTIC_WORKER"] = "1"
    os.environ["DOVIDEO_CELERY_RETRY_COUNTDOWN_SECONDS"] = "0.2"
    os.environ["DOVIDEO_R3_RESTART_SLEEP_SECONDS"] = "30"
    os.environ["DOVIDEO_R3_TASK_LOCK_TTL_MS"] = "3000"

    suffix = uuid4().hex[:10]
    os.environ["DOVIDEO_CELERY_QUEUE"] = f"dovideo.r3.analysis.{suffix}"
    os.environ["DOVIDEO_CELERY_EXCHANGE"] = f"dovideo.r3.exchange.{suffix}"
    os.environ["DOVIDEO_CELERY_ROUTING_KEY"] = f"dovideo.r3.route.{suffix}"
    os.environ["DOVIDEO_CELERY_DLX"] = f"dovideo.r3.dlx.{suffix}"
    os.environ["DOVIDEO_CELERY_DLQ"] = f"dovideo.r3.dlq.{suffix}"
    os.environ["DOVIDEO_CELERY_DL_ROUTING_KEY"] = f"dovideo.r3.dead.{suffix}"

    settings = CeleryTransportSettings.from_environment(require_production=True)
    app = create_celery_app(settings)
    transport = CeleryTaskTransport(app, settings)
    topology = RabbitMQTopology(settings)
    await asyncio.to_thread(topology.ensure)

    infrastructure = create_r2_infrastructure()
    await infrastructure.initialize()
    checkpoint = AgentCheckpointService(infrastructure.checkpoint_repository)
    lifecycle = CheckpointTaskLifecycleStore.from_environment(
        infrastructure.checkpoint_repository,
        redis_client=infrastructure.redis_client,
    )
    events = R3RedisTaskEventPublisher(
        infrastructure.redis_client,
        lifecycle,
    )
    dispatcher = TaskDispatchService(
        infrastructure.active_marker,
        completion=infrastructure.completion_marker,
        lifecycle=lifecycle,
        events=events,
        transport=transport,
    )

    base_media_id = 910000 + int(suffix[:6], 16) % 50000
    saved_media_id = base_media_id + 4
    saved_goal = "R3 saved-result recovery __R3_SUCCESS__"
    os.environ["DOVIDEO_R3_FAIL_COMPLETION_SAVE_KEY"] = (
        f"{saved_media_id}:{goal_digest(saved_goal, AnalysisMode.GENERAL)}"
    )

    worker: subprocess.Popen[Any] | None = None
    try:
        worker = _start_worker(root, settings)
        await _wait_for(
            lambda: asyncio.to_thread(topology.queue_stats),
            timeout_seconds=30.0,
        )
        await _wait_for(
            lambda: _has_consumer(topology),
            timeout_seconds=30.0,
        )

        success = _request(base_media_id, "R3 success __R3_SUCCESS__")
        await _seed_context(checkpoint, success)
        if await dispatcher.dispatch(success) is not DispatchDisposition.ACCEPTED:
            raise AssertionError("success dispatch acceptance proof failed")
        success_state = await _wait_terminal(lifecycle, success)
        if success_state.state is not TaskStatusState.COMPLETED or success_state.attempt != 1:
            print(
                "R3_DEBUG_SUCCESS_STATE="
                f"{success_state.state}|{success_state.stage}|{success_state.attempt}"
            )
            raise AssertionError("success delivery proof failed")
        success_count = int(infrastructure.redis_client.get(_counter_key(success)) or 0)
        success_events = await events.read(success.task_key)
        if success_count != 1 or not any(item.terminal for item in success_events):
            raise AssertionError("success AgentLoop/event proof failed")
        print("R3_SUCCESS=YES business_attempt=1 agent_loop_invocations=1 event_terminal=YES")

        transient = _request(base_media_id + 1, "R3 transient __R3_TRANSIENT__")
        await _seed_context(checkpoint, transient)
        if await dispatcher.dispatch(transient) is not DispatchDisposition.ACCEPTED:
            raise AssertionError("transient dispatch acceptance proof failed")
        await _wait_for(
            lambda: _is_retrying(lifecycle, transient),
            timeout_seconds=30.0,
        )
        if not await infrastructure.active_marker.is_active(transient.task_key):
            raise AssertionError("active marker was released during retry")
        transient_state = await _wait_terminal(lifecycle, transient)
        transient_count = int(infrastructure.redis_client.get(_counter_key(transient)) or 0)
        transient_events = await events.read(transient.task_key)
        retry_events = sum(1 for item in transient_events if item.event.stage is not None and item.event.stage.value == "RETRYING")
        if (
            transient_state.state is not TaskStatusState.COMPLETED
            or transient_state.attempt != 3
            or transient_count != 3
            or retry_events < 2
        ):
            raise AssertionError("transient three-delivery proof failed")
        print(
            "R3_TRANSIENT=YES deliveries=3 business_attempt=3 "
            "no_fourth_delivery=YES active_during_retry=YES"
        )

        permanent = _request(base_media_id + 2, "R3 permanent __R3_PERMANENT__")
        await _seed_context(checkpoint, permanent)
        if await dispatcher.dispatch(permanent) is not DispatchDisposition.ACCEPTED:
            raise AssertionError("permanent dispatch acceptance proof failed")
        permanent_state = await _wait_terminal(lifecycle, permanent)
        if (
            permanent_state.state is not TaskStatusState.FAILED
            or permanent_state.stage is None
            or permanent_state.stage.value != "DEAD_LETTERED"
            or permanent_state.attempt != 1
        ):
            raise AssertionError("permanent failure proof failed")
        await _wait_for(
            lambda: _dlq_has_message(topology),
            timeout_seconds=30.0,
        )
        print("R3_PERMANENT=YES first_delivery_terminal=YES business_failed=YES dlq_pending=YES")

        saved = _request(saved_media_id, saved_goal)
        await _seed_context(checkpoint, saved)
        if await dispatcher.dispatch(saved) is not DispatchDisposition.ACCEPTED:
            raise AssertionError("saved-result dispatch acceptance proof failed")
        saved_state = await _wait_terminal(lifecycle, saved)
        saved_count = int(infrastructure.redis_client.get(_counter_key(saved)) or 0)
        saved_result = await checkpoint.load_result(saved.task_key)
        if (
            saved_state.state is not TaskStatusState.COMPLETED
            or saved_state.attempt != 1
            or saved_count != 1
            or saved_result is None
        ):
            raise AssertionError("saved-result recovery proof failed")
        print("R3_SAVED_RESULT_RECOVERY=YES agent_loop_invocations=1 lifecycle_recovered=YES")

        restart = _request(base_media_id + 3, "R3 restart __R3_RESTART__")
        await _seed_context(checkpoint, restart)
        if await dispatcher.dispatch(restart) is not DispatchDisposition.ACCEPTED:
            raise AssertionError("restart dispatch acceptance proof failed")
        await _wait_for(
            lambda: _processing_started(infrastructure.redis_client, lifecycle, restart),
            timeout_seconds=15.0,
        )
        _crash_worker(worker)
        await _wait_for(
            lambda: _no_consumer(topology),
            timeout_seconds=15.0,
        )
        worker = _start_worker(root, settings)
        await _wait_for(
            lambda: _has_consumer(topology),
            timeout_seconds=30.0,
        )
        restart_state = await _wait_terminal(lifecycle, restart)
        restart_count = int(infrastructure.redis_client.get(_counter_key(restart)) or 0)
        if (
            restart_state.state is not TaskStatusState.COMPLETED
            or restart_state.attempt != 2
            or restart_count != 2
        ):
            raise AssertionError("restart redelivery proof failed")
        print("R3_RESTART_REDELIVERY=YES worker_restart=YES broker_redelivery=YES business_attempt=2")

        app.send_task(
            settings.task_name,
            args=[{"mediaId": base_media_id + 5, "source": "r3-poison"}],
            queue=settings.queue,
            exchange=settings.exchange,
            routing_key=settings.routing_key,
            serializer="json",
            ignore_result=True,
        )
        await _wait_for(
            lambda: _dlq_count_at_least(topology, 2),
            timeout_seconds=30.0,
        )
        _stop_worker(worker)
        worker = None
        kinds = _drain_dead_letters(settings)
        if "business-failure" not in kinds or "poison-message" not in kinds:
            raise AssertionError("DLQ payload proof failed")
        print("R3_POISON=YES bounded=YES durable_failed_record=YES dlq_inspectable=YES")
        stats = topology.queue_stats()
        if stats["mainMessages"] != 0:
            raise AssertionError("main queue was not drained")
        print(
            "R3_TOPOLOGY=YES "
            f"queue={settings.queue} exchange={settings.exchange} "
            f"dlq={settings.dead_letter_queue} dlx={settings.dead_letter_exchange} "
            f"main_messages={stats['mainMessages']}"
        )
        print(f"R3_CELERY_VERSION={CELERY_VERSION}")
        print("R3_LIVE_SMOKE=PASS")
    finally:
        _stop_worker(worker)
        _close_infrastructure(infrastructure)


def _has_consumer(topology: RabbitMQTopology) -> bool:
    try:
        return topology.queue_stats()["mainConsumers"] >= 1
    except Exception:
        return False


async def _is_retrying(lifecycle: Any, request: AnalysisRequest) -> bool:
    state = await lifecycle.load_lifecycle(request.task_key)
    return state is not None and state.stage is not None and state.stage.value == "RETRYING"


def _no_consumer(topology: RabbitMQTopology) -> bool:
    try:
        return topology.queue_stats()["mainConsumers"] == 0
    except Exception:
        return False


def _dlq_has_message(topology: RabbitMQTopology) -> bool:
    try:
        return topology.queue_stats()["deadLetterMessages"] >= 1
    except Exception:
        return False


def _dlq_count_at_least(topology: RabbitMQTopology, count: int) -> bool:
    try:
        return topology.queue_stats()["deadLetterMessages"] >= count
    except Exception:
        return False


async def _processing_started(redis_client: Any, lifecycle: Any, request: AnalysisRequest) -> bool:
    state = await lifecycle.load_lifecycle(request.task_key)
    if state is None or state.state is not TaskStatusState.PROCESSING or state.attempt != 1:
        return False
    value = redis_client.get(_counter_key(request))
    return int(value or 0) == 1


def _close_infrastructure(infrastructure: Any) -> None:
    infrastructure.close()


if __name__ == "__main__":
    repository_root = Path(__file__).resolve().parents[1]
    try:
        asyncio.run(_run(repository_root))
    except Exception as exc:
        print(f"R3_LIVE_SMOKE=FAIL error={type(exc).__name__}")
        raise
