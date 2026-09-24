"""Canonical ``celery -A`` entry point for the production R3 worker."""

from __future__ import annotations

from .celery_tasks import create_worker_app
from .celery_transport import CeleryTransportSettings


# Celery's command-line loader imports this module.  Missing production
# settings intentionally fail at import/startup instead of selecting the R1
# local transport.
_module_settings = CeleryTransportSettings.from_environment(require_production=True)
celery_app = create_worker_app(_module_settings)


__all__ = ["celery_app"]
