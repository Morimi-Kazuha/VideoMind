"""Task registration helpers kept import-safe for transport unit tests."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, NoReturn

from celery import Celery, signals
from celery.exceptions import Reject, Retry
from pydantic import ValidationError

from dovideo.application.worker import WorkerDisposition

from .celery_runtime import (
    InvalidTransportEnvelope,
    PoisonMessageUnresolved,
    R3WorkerRuntime,
    TransportRecoveryError,
)
from .celery_transport import (
    CeleryAnalysisEnvelope,
    CeleryTransportSettings,
    create_celery_app,
)


def register_analysis_task(
    app: Celery,
    *,
    runtime_factory: Callable[[], R3WorkerRuntime] | None = None,
):
    """Register the one analysis task and return the registered task object."""

    selected_settings = getattr(app, "_dovideo_r3_settings", None)
    if not isinstance(selected_settings, CeleryTransportSettings):
        raise TypeError("Celery app does not carry R3 transport settings")
    runtime: Any | None = None

    def get_runtime() -> Any:
        nonlocal runtime
        if runtime is None:
            if runtime_factory is not None:
                runtime = runtime_factory()
            else:
                # Production now resolves to the R4 composition.  It still
                # exposes the exact R3 transport contract and TaskWorker
                # boundary; the import stays lazy so transport unit tests do
                # not require provider credentials or Whisper/Torch.
                from .r4_runtime import R4WorkerRuntime

                runtime = R4WorkerRuntime.from_environment(settings=selected_settings)
        return runtime

    def close_runtime(**_kwargs):
        nonlocal runtime
        owned, runtime = runtime, None
        if owned is not None:
            close = getattr(owned, "close", None)
            if callable(close):
                try:
                    asyncio.run(close())
                except Exception:
                    logging.getLogger(__name__).warning("Worker resource cleanup failed")

    signals.worker_process_shutdown.connect(close_runtime, weak=True)
    signals.worker_shutdown.connect(close_runtime, weak=True)
    app._dovideo_close_runtime = close_runtime

    def retry_delivery(task, error, *, countdown) -> NoReturn:
        # Retry publishes before acknowledging the original delivery. If that
        # publication fails, retain the original instead of dead-lettering it.
        try:
            raise task.retry(exc=error, countdown=countdown, max_retries=None)
        except Retry:
            raise
        except Exception:
            logging.getLogger(__name__).warning("Recovery retry publication failed; requeueing delivery")
            raise Reject("recovery retry publication failed", requeue=True) from None

    @app.task(
        bind=True,
        name=selected_settings.task_name,
        acks_late=True,
        reject_on_worker_lost=True,
        ignore_result=True,
        # retry(max_retries=None) alone means "use the task default" (3).
        # TaskWorker, not Celery delivery retries, owns business attempt limits.
        max_retries=None,
    )
    def deliver(self: Any, payload: Any) -> dict[str, Any]:
        try:
            envelope = CeleryAnalysisEnvelope.model_validate(payload)
        except (ValidationError, TypeError, ValueError):
            # Do not echo Pydantic input/value details.  The DLQ receives only
            # a bounded descriptor and a safe error summary.
            invalid = InvalidTransportEnvelope("transport envelope validation failed")
            try:
                asyncio.run(get_runtime().record_poison(payload, invalid))
            except Exception:
                # An ordinary failure is rejected without requeue by Celery.
                # Explicit retry preserves at-least-once application handoff.
                logging.getLogger(__name__).warning("Poison handoff incomplete; retrying delivery")
                retry_delivery(self, PoisonMessageUnresolved("poison handoff requires recovery"),
                               countdown=selected_settings.retry_countdown_seconds)
            return {"disposition": "POISON_DLQ", "businessAttempt": 0}

        try:
            outcome = asyncio.run(get_runtime().process(envelope.to_request()))
        except Exception:
            # This is a transport/recovery redelivery only.  TaskWorker's
            # persisted lifecycle remains the sole business-attempt counter.
            retry_delivery(
                self, TransportRecoveryError("worker delivery recovery required"),
                countdown=selected_settings.retry_countdown_seconds,
            )

        if outcome.disposition in {WorkerDisposition.RETRY, WorkerDisposition.LOCKED}:
            retry_delivery(
                self, RuntimeError("TaskWorker requested another delivery"),
                countdown=(
                    selected_settings.locked_countdown_seconds
                    if outcome.disposition is WorkerDisposition.LOCKED
                    else selected_settings.retry_countdown_seconds
                ),
            )
        return {
            "disposition": outcome.disposition.value,
            "businessAttempt": outcome.attempt,
            "recovered": outcome.recovered,
        }

    app._dovideo_r3_analysis_task = deliver  # type: ignore[attr-defined]
    return deliver


def create_worker_app(
    settings: CeleryTransportSettings | None = None,
    *,
    runtime_factory: Callable[[], R3WorkerRuntime] | None = None,
) -> Celery:
    """Build an app for the canonical ``celery -A`` command or unit tests."""

    selected = settings or CeleryTransportSettings.from_environment(
        require_production=True
    )
    app = create_celery_app(selected)
    register_analysis_task(app, runtime_factory=runtime_factory)
    return app


__all__ = ["create_worker_app", "register_analysis_task"]
