"""Small environment adapter; only lower application safety limits."""
import os

from dovideo.application.adaptive_retrieval import AdaptiveRetrievalSettings


def adaptive_settings_from_environment(environ=None):
    values = os.environ if environ is None else environ
    enabled = values.get("DOVIDEO_ADAPTIVE_RETRIEVAL_ENABLED", "false").strip().lower()
    if enabled not in {"true", "false", "1", "0"}:
        raise ValueError("invalid adaptive retrieval enabled flag")
    return AdaptiveRetrievalSettings(
        enabled=enabled in {"true", "1"},
        max_queries=int(values.get("DOVIDEO_ADAPTIVE_RETRIEVAL_MAX_QUERIES", "3")),
        max_candidates=int(values.get("DOVIDEO_ADAPTIVE_RETRIEVAL_MAX_CANDIDATES", "8")),
        planning_timeout_seconds=float(values.get("DOVIDEO_ADAPTIVE_RETRIEVAL_PLANNING_TIMEOUT_SECONDS", "5")),
    )
