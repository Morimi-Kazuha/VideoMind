from __future__ import annotations

from pathlib import Path

import pytest

from dovideo.cli import build_parser
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisResult,
    CriticResult,
    VideoContext,
    VideoSegment,
)
from dovideo.presentation import (
    AnalysisRun,
    AnalysisSettings,
    ProgressReporter,
    embedding_provider_config_from_environment,
    media_id_for_source,
    render_analysis,
)
from dovideo.infrastructure.providers import ProviderConfigurationError


def test_cli_exposes_one_clean_analyze_command() -> None:
    args = build_parser().parse_args(
        [
            "analyze",
            "sample.mp4",
            "--goal",
            "find the conclusion",
            "--embedding-mode",
            "remote",
        ]
    )

    assert args.command == "analyze"
    assert args.video_path == Path("sample.mp4")
    assert args.goal == "find the conclusion"
    assert args.embedding_mode == "remote"


def test_settings_resolve_provisioned_tools_without_network() -> None:
    settings = AnalysisSettings.from_environment(
        environ={
            "DOVIDEO_FFMPEG_DIR": r"C:\media-tools",
            "DOVIDEO_TESSERACT_PATH": r"C:\ocr\tesseract.exe",
            "DOVIDEO_WHISPER_MODEL": "base.en",
            "DOVIDEO_WHISPER_DEVICE": "cpu",
        },
        project_root=Path("C:/empty-project"),
    )

    assert settings.ffmpeg_executable == str(Path(r"C:\media-tools") / "ffmpeg.exe")
    assert settings.ffprobe_executable == str(Path(r"C:\media-tools") / "ffprobe.exe")
    assert settings.tesseract_executable == r"C:\ocr\tesseract.exe"
    assert settings.whisper_model == "base.en"


def test_remote_embedding_config_requires_dedicated_credential() -> None:
    with pytest.raises(ProviderConfigurationError, match="DOVIDEO_EMBEDDING_API_KEY"):
        embedding_provider_config_from_environment(
            {"DOVIDEO_MODEL_API_KEY": "chat-only-placeholder"},
            required=True,
        )


def test_remote_embedding_config_matches_java_endpoint_and_hides_key() -> None:
    config = embedding_provider_config_from_environment(
        {
            "DOVIDEO_EMBEDDING_API_KEY": "embedding-placeholder",
            "DOVIDEO_EMBEDDING_BASE_URL": "https://example.test/v1/",
            "DOVIDEO_EMBEDDING_MODEL": "BAAI/bge-m3",
        },
        required=True,
    )

    assert config.embeddings_url == "https://example.test/v1/embeddings"
    assert config.embedding_model == "BAAI/bge-m3"
    assert "embedding-placeholder" not in repr(config)


def test_progress_reporter_emits_each_stage_once() -> None:
    events: list[tuple[str, str]] = []
    progress = ProgressReporter(lambda stage, message: events.append((stage, message)))

    progress("context", "first")
    progress("CONTEXT", "duplicate")
    progress("DONE", "finished")

    assert events == [("CONTEXT", "first"), ("DONE", "finished")]


def test_rendered_result_contains_timestamp_range_and_source() -> None:
    context = VideoContext(
        source="clip.mp4",
        user_goal="find the conclusion",
        segments=(
            VideoSegment(
                start_ms=60_000,
                end_ms=120_000,
                transcript="The conclusion is supported by this sentence.",
            ),
        ),
    )
    result = AnalysisResult(
        title="Evidence result",
        conclusions=("The conclusion is supported by this sentence.",),
        evidence=(
            AnalysisEvidence(
                timestamp_ms=75_000,
                source="ASR",
                content="The conclusion is supported by this sentence.",
                claim="The conclusion is supported by this sentence.",
            ),
        ),
    )
    run = AnalysisRun(
        source=Path("clip.mp4"),
        goal="find the conclusion",
        duration_seconds=120,
        context=context,
        state=AgentState(
            goal="find the conclusion",
            plan=AgentPlan(understoodGoal="find the conclusion", tasks=("quote it",)),
            result=result,
            critique=CriticResult(passed=True),
            round=1,
        ),
        media_id=media_id_for_source("clip.mp4"),
        asr_span_count=1,
        ocr_observation_count=0,
        chunk_count=1,
        embedding_dimension=4,
    )

    rendered = render_analysis(run)

    assert "# Evidence result" in rendered
    assert "[01:15 in 01:00–02:00] ASR:" in rendered
    assert "The conclusion is supported by this sentence." in rendered
