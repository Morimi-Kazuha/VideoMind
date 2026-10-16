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


@pytest.mark.parametrize("failed_stage", ["record", "publish"])
def test_poison_retries_until_application_handoff_then_ack(failed_stage):
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
            if failed_stage == "record" and self.attempts == 1:
                raise OSError("injected ledger outage")
            self.records += 1
            if failed_stage == "publish" and self.attempts == 1:
                raise OSError("injected DLQ outage")
            await RabbitMQDeadLetterPublisher(settings).publish_poison(payload, error)
            done.set()
        async def process(self, request):
            raise AssertionError("poison cannot enter business processing")
    runtime = Runtime()
    register_analysis_task(app, runtime_factory=lambda: runtime)
    try:
        with start_worker(app, pool="solo", perform_ping_check=False, shutdown_timeout=15):
            app.send_task(settings.task_name, args=[{"secret": "must-not-reach-dlq"}])
            assert done.wait(15), "handoff never recovered"
            assert runtime.attempts == 2
            with app.connection_for_read() as connection:
                queue = connection.SimpleQueue(settings.dead_letter_queue)
                message = queue.get(timeout=5)
                assert message.payload["schema"] == "dovideo.analysis.dead-letter.v1"
                assert "must-not-reach-dlq" not in str(message.payload)
                message.ack()
                queue.close()
            time.sleep(.5)
            assert runtime.attempts == 2
    finally:
        with app.connection() as connection:
            channel = connection.channel()
            for name in (settings.queue, settings.dead_letter_queue):
                channel.queue_delete(queue=name)
            for name in (settings.exchange, settings.dead_letter_exchange):
                channel.exchange_delete(exchange=name)
        app.close()
