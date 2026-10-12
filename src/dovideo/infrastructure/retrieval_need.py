"""Environment adapter for the OFF/SHADOW/ENABLED feature flag."""
import os

from dovideo.application.retrieval_need import RetrievalNeedMode, RetrievalNeedSettings


def mixed_follow_up_enabled_from_environment(environ=None):
    values = os.environ if environ is None else environ
    raw = values.get("DOVIDEO_MIXED_FOLLOW_UP_ENABLED", "false")
    if not isinstance(raw, str) or raw.strip().lower() not in {"true", "false", "1", "0"}:
        raise ValueError("invalid mixed follow-up flag")
    return raw.strip().lower() in {"true", "1"}


def validate_direct_memory_configuration(settings, store):
    """Fail startup on an unsafe writer configuration; shared readers are a rollout gate."""
    if settings.mode == RetrievalNeedMode.ENABLED and not getattr(store, "write_sources", False):
        raise ValueError("ENABLED retrieval need requires memory source writing and compatible shared readers")


def retrieval_need_settings_from_environment(environ=None):
    values = os.environ if environ is None else environ
    raw = values.get("DOVIDEO_RETRIEVAL_NEED_MODE", "OFF")
    if not isinstance(raw, str):
        raise ValueError("invalid retrieval need mode")
    try:
        mode = RetrievalNeedMode(raw.strip().upper())
    except ValueError:
        raise ValueError("retrieval need mode must be OFF, SHADOW or ENABLED") from None
    return RetrievalNeedSettings(mode=mode)
