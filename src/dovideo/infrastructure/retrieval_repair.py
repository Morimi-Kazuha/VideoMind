"""Server-owned feedback switch; no client parameter or permissive fallback."""
import os
from dovideo.application.retrieval_repair import FeedbackRetrievalMode


def feedback_mode_from_environment(environ=None):
    values = os.environ if environ is None else environ
    raw = values.get("DOVIDEO_FEEDBACK_RETRIEVAL_MODE", "OFF")
    if not isinstance(raw, str):
        raise ValueError("invalid feedback retrieval mode")
    return FeedbackRetrievalMode(raw.strip().upper())
