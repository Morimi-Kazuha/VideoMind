"""Task registration helpers kept import-safe for transport unit tests."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from celery import Celery
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

    @app.task(
        bind=True,
        name=selected_settings.task_name,
        acks_late=True,
        reject_on_worker_lost=True,
        ignore_result=True,
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
            except Exception as poison_error:
                # Late ACK plus this raised exception leaves the body available
                # for another delivery; nothing is silently acknowledged.
                raise PoisonMessageUnresolved(
                    "poison message could not be durably recorded and DLQed"
                ) from poison_error
            return {"disposition": "POISON_DLQ", "businessAttempt": 0}

        try:
            outcome = asyncio.run(get_runtime().process(envelope.to_request()))
        except Exception as exc:
            # This is a transport/recovery redelivery only.  TaskWorker's
            # persisted lifecycle remains the sole business-attempt counter.
            raise self.retry(
                exc=TransportRecoveryError("worker delivery recovery required"),
                countdown=selected_settings.retry_countdown_seconds,
                max_retries=None,
            ) from exc

        if outcome.disposition in {WorkerDisposition.RETRY, WorkerDisposition.LOCKED}:
            raise self.retry(
                exc=RuntimeError("TaskWorker requested another delivery"),
                countdown=selected_settings.retry_countdown_seconds,
                max_retries=None,
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
