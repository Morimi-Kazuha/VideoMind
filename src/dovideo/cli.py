"""Command-line interface for the productized DOVideo Python slice."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from dovideo.infrastructure.providers import ProviderConfigurationError

from .presentation import (
    AnalysisConfigurationError,
    AnalysisSettings,
    EvidenceGuardError,
    VideoAnalysisApplication,
    render_analysis,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dovideo",
        description="Analyze local video with timestamped evidence and a guarded AgentLoop.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser(
        "analyze",
        help="analyze one local video file",
        description="Run the existing media -> VideoContext -> retrieval -> AgentLoop pipeline.",
    )
    analyze.add_argument("video_path", type=Path, help="path to a local video file")
    analyze.add_argument("--goal", required=True, help="analysis goal")
    analyze.add_argument(
        "--embedding-mode",
        choices=("local", "remote"),
        default="local",
        help="semantic vector provider (default: local)",
    )
    analyze.add_argument(
        "--output",
        type=Path,
        help="optional Markdown output path; stdout remains provider-neutral",
    )
    analyze.add_argument(
        "--whisper-model",
        help="override the local Whisper model name (default: DOVIDEO_WHISPER_MODEL or tiny.en)",
    )
    analyze.add_argument(
        "--whisper-device",
        help="override local Whisper device (default: cpu)",
    )
    analyze.add_argument(
        "--no-progress",
        action="store_true",
        help="suppress progress lines on stderr",
    )
    web = commands.add_parser(
        "web",
        help="start the local Web Demo",
        description="Start the optional local Web Demo presentation layer.",
    )
    web.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    web.add_argument("--port", type=int, default=8765, help="bind port (default: 8765)")
    api = commands.add_parser(
        "api",
        help="start the FastAPI R1 product surface",
        description="Start the FastAPI + Vue/SSE compatible local R1 API.",
    )
    api.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    api.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "analyze":
        return _run_analyze(args)
    if args.command == "web":
        return _run_web(args)
    if args.command == "api":
        return _run_api(args)
    parser.error("a command is required")
    return 2


def _run_web(args: argparse.Namespace) -> int:
    if not 1 <= args.port <= 65535:
        print("dovideo: web port must be between 1 and 65535", file=sys.stderr)
        return 2
    try:
        from .web import run_web

        return run_web(host=args.host, port=args.port)
    except OSError as exc:
        print(f"dovideo: could not start Web Demo: {exc}", file=sys.stderr)
        return 2


def _run_api(args: argparse.Namespace) -> int:
    if not 1 <= args.port <= 65535:
        print("dovideo: api port must be between 1 and 65535", file=sys.stderr)
        return 2
    try:
        import uvicorn

        from .presentation.api import create_app

        uvicorn.run(create_app, factory=True, host=args.host, port=args.port)
        return 0
    except ModuleNotFoundError:
        print(
            "dovideo: FastAPI runtime is not installed; install project dependencies first",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print(f"dovideo: could not start API: {exc}", file=sys.stderr)
        return 2


def _run_analyze(args: argparse.Namespace) -> int:
    try:
        settings = AnalysisSettings.from_environment(
            embedding_mode=args.embedding_mode,
        )
        if args.whisper_model is not None or args.whisper_device is not None:
            settings = replace(
                settings,
                whisper_model=args.whisper_model or settings.whisper_model,
                whisper_device=args.whisper_device or settings.whisper_device,
            )

        def progress(stage: str, message: str) -> None:
            if not args.no_progress:
                print(f"[{stage}] {message}", file=sys.stderr)

        run = asyncio.run(
            VideoAnalysisApplication(settings, progress=progress).analyze(
                args.video_path,
                args.goal,
            )
        )
        rendered = render_analysis(run)
        if args.output is not None:
            destination = args.output.expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(rendered, encoding="utf-8")
            print(f"Wrote user-facing result to {destination}", file=sys.stderr)
        print(rendered, end="")
        return 0
    except (AnalysisConfigurationError, EvidenceGuardError, ProviderConfigurationError) as exc:
        print(f"dovideo: {exc}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"dovideo: analysis failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
