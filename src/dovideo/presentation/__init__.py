"""User-facing composition and presentation boundaries for DOVideo."""

from .composition import (
    AnalysisConfigurationError,
    AnalysisRun,
    AnalysisSettings,
    EvidenceGuardError,
    InMemoryVectorIndex,
    LocalRetrievalPlanner,
    ProgressReporter,
    VideoAnalysisApplication,
    embedding_provider_config_from_environment,
    load_timestamped_asr_artifact,
    media_id_for_source,
    render_analysis,
)


def create_app(*args, **kwargs):
    """Lazy import for the R1 FastAPI factory.

    Keeping this import lazy preserves the lightweight CLI/core import path
    for callers that only need local analysis primitives.
    """

    from .api.app import create_app as factory

    return factory(*args, **kwargs)

__all__ = [
    "AnalysisConfigurationError",
    "AnalysisRun",
    "AnalysisSettings",
    "EvidenceGuardError",
    "InMemoryVectorIndex",
    "LocalRetrievalPlanner",
    "ProgressReporter",
    "VideoAnalysisApplication",
    "embedding_provider_config_from_environment",
    "load_timestamped_asr_artifact",
    "media_id_for_source",
    "render_analysis",
    "create_app",
]
