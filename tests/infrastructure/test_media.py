from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from dovideo.infrastructure.media import (
    AsyncSubprocessRunner,
    AudioSegmenter,
    FfprobeDurationAdapter,
    KeyframeExtractor,
    KeyframeSelection,
    MediaDuration,
    MediaPreprocessor,
    MediaProbeError,
    MediaWorkspace,
    SubprocessExecutionError,
    SubprocessResult,
    SubprocessTimeoutError,
    WorkspaceError,
    WorkspaceClosedError,
)
from dovideo.infrastructure.media.keyframes import _parse_timestamps


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Path | None, float | None]] = []
        self.responses: list[SubprocessResult] = []
        self.create_audio: tuple[int, ...] = ()
        self.create_frames: tuple[int, ...] = ()

    async def run(
        self,
        args: tuple[str, ...],
        *,
        cwd: str | Path | None = None,
        timeout: float | None = None,
        env: object = None,
    ) -> SubprocessResult:
        command = tuple(os.fspath(value) for value in args)
        working_dir = None if cwd is None else Path(cwd)
        self.calls.append((command, working_dir, timeout))
        output = Path(command[-1])
        if "-segment_time" in command:
            output.parent.mkdir(parents=True, exist_ok=True)
            for index in self.create_audio:
                (output.parent / f"audio_{index:03d}.mp3").write_bytes(b"audio")
        elif "-vf" in command and "fps=1/30,showinfo" in command:
            output.parent.mkdir(parents=True, exist_ok=True)
            for index in self.create_frames:
                (output.parent / f"frame_{index:06d}.jpg").write_bytes(b"jpeg")
        elif "-vf" in command:
            output.parent.mkdir(parents=True, exist_ok=True)
            # The caller can choose to leave the scene pass empty to trigger
            # fallback.  A response carries its showinfo output either way.
            for index in self.create_frames:
                (output.parent / f"frame_{index:06d}.jpg").write_bytes(b"jpeg")
        if self.responses:
            return self.responses.pop(0)
        return SubprocessResult(command, 0, "", "")


class FailingAudioRunner(FakeRunner):
    async def run(
        self,
        args: tuple[str, ...],
        *,
        cwd: str | Path | None = None,
        timeout: float | None = None,
        env: object = None,
    ) -> SubprocessResult:
        if "-segment_time" in args:
            raise RuntimeError("audio preprocessing failed")
        return await super().run(args, cwd=cwd, timeout=timeout, env=env)


@pytest.mark.asyncio
async def test_async_runner_captures_output_without_shell() -> None:
    runner = AsyncSubprocessRunner(default_timeout=5)
    result = await runner.run(
        (
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1]); print('err', file=sys.stderr)",
            "value;not-shell",
        )
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "value;not-shell"
    assert result.stderr.strip() == "err"


@pytest.mark.asyncio
async def test_async_runner_maps_nonzero_exit_and_keeps_streams() -> None:
    runner = AsyncSubprocessRunner(default_timeout=5)
    with pytest.raises(SubprocessExecutionError) as caught:
        await runner.run(
            (
                sys.executable,
                "-c",
                "import sys; print('out'); print('bad', file=sys.stderr); sys.exit(7)",
            )
        )

    assert caught.value.returncode == 7
    assert caught.value.stdout.strip() == "out"
    assert caught.value.stderr.strip() == "bad"


@pytest.mark.asyncio
async def test_async_runner_terminates_on_timeout() -> None:
    runner = AsyncSubprocessRunner(default_timeout=0.05)
    with pytest.raises(SubprocessTimeoutError) as caught:
        await runner.run((sys.executable, "-c", "import time; time.sleep(30)"))

    assert caught.value.timeout_seconds == pytest.approx(0.05, abs=0.01)


@pytest.mark.asyncio
async def test_ffprobe_parses_finite_duration_and_maps_bad_output(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.responses.append(SubprocessResult(("ffprobe",), 0, " 12.345678\n", ""))
    adapter = FfprobeDurationAdapter(runner, timeout=4)

    duration = await adapter.probe(tmp_path / "input.mp4")

    assert duration == MediaDuration(12.345678)
    assert duration.milliseconds == 12345
    assert runner.calls[0][0][:3] == ("ffprobe", "-v", "error")
    assert runner.calls[0][2] == 4

    runner.responses.append(SubprocessResult(("ffprobe",), 0, "N/A\n", "metadata missing"))
    with pytest.raises(MediaProbeError):
        await adapter.probe("input.mp4")


@pytest.mark.asyncio
async def test_audio_segmenter_uses_java_arguments_and_numeric_discovery(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.create_audio = (10, 0, 2)
    async with MediaWorkspace(parent=tmp_path) as workspace:
        segments = await AudioSegmenter(runner, timeout=9).segment("input.mp4", workspace)

        assert [segment.filename for segment in segments] == [
            "audio_000.mp3",
            "audio_002.mp3",
            "audio_010.mp3",
        ]
        assert [segment.start_ms for segment in segments] == [0, 120_000, 600_000]
        command = runner.calls[0][0]
        assert command[command.index("-vn") : command.index("-reset_timestamps") + 2] == (
            "-vn",
            "-acodec",
            "libmp3lame",
            "-f",
            "segment",
            "-segment_time",
            "60",
            "-reset_timestamps",
            "1",
        )
        assert workspace.path.exists()

    assert workspace.closed
    with pytest.raises(WorkspaceClosedError):
        _ = segments[0].path


@pytest.mark.asyncio
async def test_keyframe_scene_pass_is_sorted_and_timestamps_are_parsed(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.create_frames = (3, 0, 1)
    runner.responses.append(
        SubprocessResult(
            ("ffmpeg",),
            0,
            "",
            "[Parsed_showinfo] n:0 pts_time:0.000\n[Parsed_showinfo] n:1 pts_time:61.25\n[Parsed_showinfo] n:2 pts_time:120.500\n",
        )
    )
    async with MediaWorkspace(parent=tmp_path) as workspace:
        frames = await KeyframeExtractor(runner).extract("input.mp4", workspace)

        assert [frame.filename for frame in frames] == [
            "frame_000000.jpg",
            "frame_000001.jpg",
            "frame_000003.jpg",
        ]
        assert [frame.timestamp_ms for frame in frames] == [0, 61_250, 120_500]
        assert all(frame.selection is KeyframeSelection.SCENE_CHANGE for frame in frames)
        scene_filter = runner.calls[0][0][runner.calls[0][0].index("-vf") + 1]
        assert scene_filter == (
            "select=eq(n\\,0)+gt(scene\\,0.35)+gte(t-prev_selected_t\\,30),showinfo"
        )


@pytest.mark.asyncio
async def test_keyframe_empty_scene_pass_falls_back_to_30_seconds(tmp_path: Path) -> None:
    runner = FakeRunner()
    # The fake creates no frames in the first call and two fixed samples in
    # the fallback call.
    runner.create_frames = ()
    runner.responses.extend(
        [
            SubprocessResult(("ffmpeg",), 0, "", ""),
            SubprocessResult(("ffmpeg",), 0, "", ""),
        ]
    )

    original_run = runner.run
    calls = 0

    async def run_with_fallback(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            runner.create_frames = (0, 1)
        return await original_run(*args, **kwargs)

    runner.run = run_with_fallback  # type: ignore[method-assign]
    async with MediaWorkspace(parent=tmp_path) as workspace:
        frames = await KeyframeExtractor(runner).extract("input.mp4", workspace)

    assert len(frames) == 2
    assert [frame.timestamp_ms for frame in frames] == [0, 30_000]
    assert all(frame.selection is KeyframeSelection.FIXED_INTERVAL for frame in frames)
    assert "fps=1/30,showinfo" in runner.calls[1][0]


@pytest.mark.asyncio
async def test_keyframe_uses_fps_mode_instead_of_rejected_vsync(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.create_frames = ()
    original_run = runner.run
    calls = 0

    async def reject_legacy_vsync(args, *, cwd=None, timeout=None, env=None):
        nonlocal calls
        command = tuple(os.fspath(value) for value in args)
        if "-vsync" in command:
            raise SubprocessExecutionError(
                "FFmpeg rejected option vsync",
                command=command,
                returncode=64,
            )
        calls += 1
        runner.create_frames = () if calls == 1 else (0,)
        return await original_run(args, cwd=cwd, timeout=timeout, env=env)

    runner.run = reject_legacy_vsync  # type: ignore[method-assign]
    async with MediaWorkspace(parent=tmp_path) as workspace:
        frames = await KeyframeExtractor(runner).extract("input.mp4", workspace)

    assert len(frames) == 1
    assert len(runner.calls) == 2
    for command, _, _ in runner.calls:
        assert command[command.index("-fps_mode") : command.index("-fps_mode") + 2] == (
            "-fps_mode",
            "vfr",
        )
        assert "-vsync" not in command


def test_parse_timestamps_matches_java_showinfo_line_filter() -> None:
    result = SubprocessResult(
        ("ffmpeg",),
        0,
        "unrelated pts_time:9.0\n"
        "showinfo pts_time:1.250\n"
        "showinfo pts_time:not-a-number\n"
        "showinfo pts_time:-2.0\n"
        "showinfo pts_time:NaN\n",
        "diagnostic pts_time:8.0",
    )

    assert _parse_timestamps(result) == [1_250]


@pytest.mark.asyncio
async def test_workspace_rejects_traversal_and_cleans_on_failure(tmp_path: Path) -> None:
    async with MediaWorkspace(parent=tmp_path) as workspace:
        with pytest.raises(WorkspaceError):
            await workspace.directory("../outside")
        inside = await workspace.directory("safe")
        assert await workspace.directory("safe") == inside
        (workspace.path / "not-a-directory").write_bytes(b"x")
        with pytest.raises(FileExistsError):
            await workspace.directory("not-a-directory")
        (inside / "artifact.bin").write_bytes(b"x")
        artifact = workspace.artifact(inside / "artifact.bin")
        assert artifact.relative_path == "safe/artifact.bin"
        assert artifact.path.exists()
        owned_root = workspace.path

    assert not owned_root.exists()
    with pytest.raises(WorkspaceClosedError):
        _ = artifact.path

    with pytest.raises(RuntimeError):
        async with MediaWorkspace(parent=tmp_path) as workspace:
            raise RuntimeError("operation failed")
    # The context manager still cleans up its own child after an operation error.
    assert not any(path.name.startswith("dovideo-media-") for path in tmp_path.iterdir())


@pytest.mark.asyncio
async def test_composed_preprocessor_cleans_after_success(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.responses.append(SubprocessResult(("ffprobe",), 0, "4.5", ""))
    runner.create_audio = (0,)
    runner.create_frames = (0,)
    probe = FfprobeDurationAdapter(runner)
    segmenter = AudioSegmenter(runner)
    extractor = KeyframeExtractor(runner)
    preprocessor = MediaPreprocessor(probe, segmenter, extractor)

    async with preprocessor.preprocess("input.mp4", parent=tmp_path) as result:
        assert result.duration.seconds == 4.5
        assert result.audio_segments and result.keyframes
        owned_root = result.audio_segments[0].artifact.workspace.path
        assert owned_root.exists()
    assert not owned_root.exists()


@pytest.mark.asyncio
async def test_composed_preprocessor_cleans_when_audio_fails(tmp_path: Path) -> None:
    runner = FailingAudioRunner()
    runner.responses.append(SubprocessResult(("ffprobe",), 0, "4.5", ""))
    preprocessor = MediaPreprocessor(
        FfprobeDurationAdapter(runner),
        AudioSegmenter(runner),
        KeyframeExtractor(runner),
    )

    with pytest.raises(RuntimeError, match="audio preprocessing failed"):
        async with preprocessor.preprocess("input.mp4", parent=tmp_path):
            raise AssertionError("preprocessor should fail before yielding")

    assert not any(
        path.name.startswith("dovideo-media-") for path in tmp_path.iterdir()
    )


@pytest.mark.asyncio
async def test_media_duration_is_immutable() -> None:
    duration = MediaDuration(1.0)
    with pytest.raises(AttributeError):
        duration.seconds = 2.0  # type: ignore[misc]
