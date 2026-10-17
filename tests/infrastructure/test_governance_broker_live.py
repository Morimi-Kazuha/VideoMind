"""Real broker delivery, ACK and DLQ semantics; dedicated topology per test."""
import os
import threading
import time
from uuid import uuid4

import pytest
from celery.contrib.testing.worker import start_worker
from dovideo.infrastructure.celery_tasks import register_analysis_task
from dovideo.infrastructure.celery_transport import (
    CeleryTransportSettings, RabbitMQTopology, RabbitMQDeadLetterPublisher, create_celery_app,
)

pytestmark = pytest.mark.skipif(not os.environ.get("DOVIDEO_GOVERNANCE_BROKER"), reason="isolated broker opt-in")


@pytest.mark.parametrize("failed_stage", ["record", "publish", "retry_publish"])
def test_poison_retries_until_application_handoff_then_ack(failed_stage, monkeypatch):
    prefix = "governance." + uuid4().hex
    settings = CeleryTransportSettings(os.environ["DOVIDEO_GOVERNANCE_BROKER"],
        queue=prefix, exchange=prefix, routing_key=prefix,
        dead_letter_exchange=prefix + ".dlx", dead_letter_queue=prefix + ".dlq",
        dead_letter_routing_key=prefix + ".dlq", task_name=prefix + ".task")
    RabbitMQTopology(settings).ensure()
    app = create_celery_app(settings)
    done = threading.Event()
    class Runtime:
        attempts = 0
        records = 0
        async def record_poison(self, payload, error):
            self.attempts += 1
            if failed_stage in {"record", "retry_publish"} and self.attempts <= 5:
                raise OSError("injected ledger outage")
            self.records += 1
            if failed_stage == "publish" and self.attempts <= 5:
                raise OSError("injected DLQ outage")
            await RabbitMQDeadLetterPublisher(settings).publish_poison(payload, error)
            done.set()
        async def process(self, request):
            raise AssertionError("poison cannot enter business processing")
    runtime = Runtime()
    task = register_analysis_task(app, runtime_factory=lambda: runtime)
    if failed_stage == "retry_publish":
        # The initial send uses app.send_task. Only the retry publication fails;
        # the live worker must reject with requeue=True to retain the original.
        def fail_retry(*args, **kwargs):
            raise OSError("injected retry publication outage")
        monkeypatch.setattr(task, "apply_async", fail_retry)
    try:
        with start_worker(app, pool="solo", perform_ping_check=False, shutdown_timeout=15):
            app.send_task(settings.task_name, args=[{"secret": "must-not-reach-dlq"}])
            assert done.wait(15), "handoff never recovered"
            assert runtime.attempts == 6
            with app.connection_for_read() as connection:
                queue = connection.SimpleQueue(settings.dead_letter_queue)
                message = queue.get(timeout=5)
                assert message.payload["schema"] == "dovideo.analysis.dead-letter.v1"
                assert "must-not-reach-dlq" not in str(message.payload)
                message.ack()
                queue.close()
            time.sleep(.5)
            assert runtime.attempts == 6
    finally:
        with app.connection() as connection:
            channel = connection.channel()
            for name in (settings.queue, settings.dead_letter_queue):
                channel.queue_delete(queue=name)
            for name in (settings.exchange, settings.dead_letter_exchange):
                channel.exchange_delete(exchange=name)
        app.close()


@pytest.mark.parametrize("recovery", ["exception", "LOCKED", "RETRY"])
def test_business_recovery_survives_publish_failure_and_default_retry_limit(recovery, monkeypatch):
    from types import SimpleNamespace
    from dovideo.application.worker import WorkerDisposition
    from dovideo.application.value_objects import AnalysisRequest, MediaRef
    from dovideo.infrastructure.celery_transport import CeleryAnalysisEnvelope
    prefix = "governance." + uuid4().hex
    settings = CeleryTransportSettings(os.environ["DOVIDEO_GOVERNANCE_BROKER"],
        queue=prefix, exchange=prefix, routing_key=prefix, task_name=prefix + ".task",
        dead_letter_exchange=prefix + ".dlx", dead_letter_queue=prefix + ".dlq",
        dead_letter_routing_key=prefix + ".dlq", retry_countdown_seconds=.05,
        locked_countdown_seconds=1)
    RabbitMQTopology(settings).ensure()
    app = create_celery_app(settings)
    done = threading.Event()
    requests, retries = [], []
    expected = AnalysisRequest(MediaRef(9, "minio://media/9.mp4"), "goal", request_id=prefix)
    class Runtime:
        async def process(self, request):
            requests.append(request)
            retries.append(task.request.retries)
            if len(requests) <= 6:
                if recovery == "exception":
                    raise OSError("injected recovery outage")
                return SimpleNamespace(disposition=WorkerDisposition(recovery))
            done.set()
            return SimpleNamespace(disposition=WorkerDisposition.COMPLETED, attempt=1, recovered=True)
    task = register_analysis_task(app, runtime_factory=Runtime)
    original_publish = task.apply_async
    publications = 0
    def publish(*args, **kwargs):
        nonlocal publications
        publications += 1
        if publications == 1:
            raise OSError("injected retry publication outage")
        return original_publish(*args, **kwargs)
    monkeypatch.setattr(task, "apply_async", publish)
    try:
        with start_worker(app, pool="solo", perform_ping_check=False, shutdown_timeout=15):
            app.send_task(settings.task_name, args=[CeleryAnalysisEnvelope.from_request(expected).as_message()])
            assert done.wait(20), "business recovery stopped before completion"
            time.sleep(.5)
            assert requests == [expected] * 7
            assert max(retries) >= 5
            with app.connection() as connection:
                for name in (settings.queue, settings.dead_letter_queue):
                    assert connection.channel().queue_declare(queue=name, passive=True).message_count == 0
    finally:
        with app.connection() as connection:
            channel = connection.channel()
            for name in (settings.queue, settings.dead_letter_queue):
                channel.queue_delete(queue=name)
            for name in (settings.exchange, settings.dead_letter_exchange):
                channel.exchange_delete(exchange=name)
        app.close()
