"""Server-owned OFF/SHADOW only configuration."""
import os
from dovideo.application.evidence_reflection import ReflectionMode
from dovideo.infrastructure.providers.config import ModelRequestSettings


def reflection_mode_from_environment(environ=None):
    values = os.environ if environ is None else environ
    raw = values.get("DOVIDEO_EVIDENCE_REFLECTION_MODE", "OFF")
    if not isinstance(raw, str):
        raise ValueError("invalid reflection mode")
    return ReflectionMode(raw.strip().upper())


def reflection_request_settings_from_environment(environ=None):
    """Opt-in, stage-only control; empty preserves existing profile defaults.

    Only none was verified in R3.6. The Chat client also validates the pinned
    model/provider; unsupported settings never fall back to default reasoning.
    """
    values = os.environ if environ is None else environ
    raw = values.get("DOVIDEO_EVIDENCE_REFLECTION_REASONING_EFFORT", "")
    if not isinstance(raw, str) or raw.strip() not in {"", "none"}:
        raise ValueError("Reflection reasoning override must be empty or none")
    return ModelRequestSettings(reasoning_effort="none") if raw.strip() else None
