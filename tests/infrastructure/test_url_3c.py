from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest

from dovideo.application.errors import UrlResolutionError, UrlValidationError
from dovideo.infrastructure.media import (
    AsyncSubprocessRunner,
    MediaWorkspace,
    PublicUrlValidator,
    SubprocessExecutionError,
    SubprocessResult,
    UrlDownloadFailure,
    YTDLP_FORMAT,
    YtDlpDownloader,
    is_disallowed_address,
)


class FakeResolver:
    def __init__(self, addresses: object) -> None:
        self.addresses = addresses
        self.hosts: list[str] = []

    async def resolve(self, host: str):
        self.hosts.append(host)
        if isinstance(self.addresses, BaseException):
            raise self.addresses
        return self.addresses


class FakeRunner:
    def __init__(self, *, create_output: bool = True, error: Exception | None = None) -> None:
        self.create_output = create_output
        self.error = error
        self.calls: list[tuple[tuple[str, ...], Path | None, float | None]] = []

    async def run(self, args, *, cwd=None, timeout=None, env=None) -> SubprocessResult:
        command = tuple(str(item) for item in args)
        self.calls.append((command, None if cwd is None else Path(cwd), timeout))
        if self.error is not None:
            raise self.error
        output = Path(command[command.index("-o") + 1])
        if self.create_output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"video")
        return SubprocessResult(command, 0, "", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "0.0.0.0",
        "169.254.1.1",
        "10.1.2.3",
        "192.168.1.1",
        "172.16.0.1",
        "100.64.1.1",
        "224.0.0.1",
        "240.0.0.1",
        "fc00::1",
        "::1",
        "fe80::1",
    ],
)
async def test_public_url_rejects_non_public_addresses(address: str) -> None:
    assert is_disallowed_address(ipaddress.ip_address(address))
    with pytest.raises(UrlValidationError):
        await PublicUrlValidator(FakeResolver((address,))).validate(
            "https://video.example/movie"
        )


@pytest.mark.asyncio
async def test_public_url_accepts_public_dns_and_rejects_credentials_or_bad_scheme() -> None:
    resolver = FakeResolver(("93.184.216.34",))
    validator = PublicUrlValidator(resolver)
    result = await validator.validate(" HTTPS://video.example:443/movie.mp4 ")
    assert result.scheme == "https"
    assert result.host == "video.example"
    assert result.port == 443
    assert resolver.hosts == ["video.example"]
    with pytest.raises(UrlValidationError):
        await validator.validate("file:///tmp/movie.mp4")
    with pytest.raises(UrlValidationError):
        await validator.validate("https://user:secret@video.example/movie")
    with pytest.raises(UrlValidationError):
        await validator.validate("https:///movie")


@pytest.mark.asyncio
async def test_dns_failure_and_invalid_dns_address_are_typed() -> None:
    with pytest.raises(UrlResolutionError):
        await PublicUrlValidator(FakeResolver(OSError("dns down"))).validate(
            "https://video.example/movie"
        )
    with pytest.raises(UrlResolutionError):
        await PublicUrlValidator(FakeResolver(("not-an-ip",))).validate(
            "https://video.example/movie"
        )


@pytest.mark.asyncio
async def test_ytdlp_command_uses_argument_vector_java_flags_and_cleans_output(tmp_path: Path) -> None:
    runner = FakeRunner()
    resolver = FakeResolver(("93.184.216.34",))
    async with MediaWorkspace(parent=tmp_path) as workspace:
        result = await YtDlpDownloader(
            runner,
            resolver,
            executable="yt-dlp",
            ffmpeg_location="/opt/ffmpeg",
            timeout=123,
        ).download("https://video.example/watch?v=1", workspace)
        command = runner.calls[0][0]
        assert command[:1] == ("yt-dlp",)
        assert "--no-playlist" in command
        assert command[command.index("--socket-timeout") + 1] == "30"
        assert command[command.index("--retries") + 1] == "3"
        assert command[command.index("--max-filesize") + 1] == "2048M"
        assert command[command.index("-f") + 1] == YTDLP_FORMAT
        assert command[command.index("--merge-output-format") + 1] == "mp4"
        assert command[command.index("--recode-video") + 1] == "mp4"
        assert command[command.index("--ffmpeg-location") + 1] == "/opt/ffmpeg"
        assert command[-1] == "https://video.example/watch?v=1"
        assert runner.calls[0][2] == 123
        assert result.path.exists()
        assert result.filename.startswith("WEB_") and result.filename.endswith(".mp4")

    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_ytdlp_failure_and_empty_output_are_typed_and_cleaned(tmp_path: Path) -> None:
    async with MediaWorkspace(parent=tmp_path) as workspace:
        with pytest.raises(UrlDownloadFailure):
            await YtDlpDownloader(
                FakeRunner(error=SubprocessExecutionError(
                    "failed", command=("yt-dlp",), stderr="secret"
                )),
                FakeResolver(("93.184.216.34",)),
            ).download("https://video.example/watch", workspace)
        assert not list(workspace.path.rglob("*.mp4"))

    async with MediaWorkspace(parent=tmp_path) as workspace:
        with pytest.raises(UrlDownloadFailure):
            await YtDlpDownloader(
                FakeRunner(create_output=False),
                FakeResolver(("93.184.216.34",)),
            ).download("https://video.example/watch", workspace)
        assert not list(workspace.path.rglob("*.mp4"))


@pytest.mark.asyncio
async def test_async_subprocess_runner_does_not_use_shell() -> None:
    runner = AsyncSubprocessRunner(default_timeout=5)
    result = await runner.run(("python", "-c", "print('safe')"))
    assert result.stdout.strip() == "safe"
