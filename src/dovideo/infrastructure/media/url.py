"""Public URL validation and safe yt-dlp download boundary for Phase 3C."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from dovideo.application import UrlDownloadResult
from dovideo.application.errors import UrlDownloadFailure, UrlResolutionError, UrlValidationError
from dovideo.application.ports.ingest import DnsResolverPort

from .runner import AsyncSubprocessRunner
from .workspace import MediaWorkspace

YTDLP_SOCKET_TIMEOUT_SECONDS = 30
YTDLP_RETRIES = 3
YTDLP_MAX_FILESIZE = "2048M"
YTDLP_TIMEOUT_SECONDS = 30 * 60
YTDLP_FORMAT = (
    "bv*[vcodec^=avc1][ext=mp4]+ba[acodec^=mp4a][ext=m4a]/"
    "b[vcodec^=avc1][ext=mp4]/bv*[vcodec^=avc1]+ba[acodec^=mp4a]"
)


@dataclass(frozen=True, slots=True)
class PublicUrl:
    """A URL that passed scheme, host, DNS, and address policy checks."""

    value: str
    scheme: str
    host: str
    port: int | None = None


class SystemDnsResolver(DnsResolverPort):
    """Resolve DNS in a worker thread so the event loop is not blocked."""

    async def resolve(self, host: str) -> tuple[str, ...]:
        try:
            direct = ipaddress.ip_address(host)
        except ValueError:
            direct = None
        if direct is not None:
            return (str(direct),)
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                None,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
        except (OSError, UnicodeError) as exc:
            raise UrlResolutionError("could not resolve URL host") from exc
        addresses: list[str] = []
        for info in infos:
            address = info[4][0]
            if address not in addresses:
                addresses.append(address)
        if not addresses:
            raise UrlResolutionError("URL host resolved to no addresses")
        return tuple(addresses)


class PublicUrlValidator:
    """Validate an HTTP(S) URL against a DNS-resolved public-address policy."""

    def __init__(self, resolver: DnsResolverPort | None = None) -> None:
        self._resolver = resolver or SystemDnsResolver()

    async def validate(self, value: str) -> PublicUrl:
        if not isinstance(value, str) or not value.strip():
            raise UrlValidationError("video URL is required")
        candidate = value.strip()
        try:
            parsed = urlsplit(candidate)
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise UrlValidationError("video URL is malformed") from exc
        if parsed.scheme.casefold() not in {"http", "https"} or not host:
            raise UrlValidationError("only public HTTP/HTTPS URLs are supported")
        if parsed.username is not None or parsed.password is not None:
            raise UrlValidationError("URL credentials are not permitted")

        try:
            resolved = await self._resolver.resolve(host)
        except UrlResolutionError:
            raise
        except Exception as exc:
            raise UrlResolutionError("could not resolve URL host") from exc
        try:
            addresses = tuple(_parse_address(address) for address in resolved)
        except UrlResolutionError:
            raise
        except Exception as exc:
            raise UrlResolutionError("DNS returned no usable addresses") from exc
        if not addresses:
            raise UrlResolutionError("URL host resolved to no addresses")
        if any(is_disallowed_address(address) for address in addresses):
            raise UrlValidationError("URL host resolves to a private or reserved address")
        return PublicUrl(
            value=candidate,
            scheme=parsed.scheme.casefold(),
            host=host,
            port=port,
        )


class YtDlpDownloader:
    """Run yt-dlp with Java-compatible format/size/time flags and no shell."""

    def __init__(
        self,
        runner: AsyncSubprocessRunner,
        resolver: DnsResolverPort | None = None,
        *,
        executable: str | PathLike[str] = "yt-dlp",
        ffmpeg_location: str | PathLike[str] | None = None,
        timeout: float = YTDLP_TIMEOUT_SECONDS,
    ) -> None:
        if timeout <= 0:
            raise ValueError("yt-dlp timeout must be positive")
        self._runner = runner
        self._validator = PublicUrlValidator(resolver)
        self._executable = _tool_name(executable)
        self._ffmpeg_location = None if ffmpeg_location is None else _tool_name(ffmpeg_location)
        self._timeout = timeout

    async def download(self, url: str, workspace: MediaWorkspace) -> UrlDownloadResult:
        if not isinstance(workspace, MediaWorkspace):
            raise TypeError("download workspace must be a MediaWorkspace")
        validated = await self._validator.validate(url)
        output_dir = await workspace.directory("url")
        output_filename = f"WEB_{uuid4().hex}.mp4"
        output_path = output_dir / output_filename
        command = self._command(validated, output_path)
        try:
            process_result = await self._runner.run(
                command,
                cwd=workspace.path,
                timeout=self._timeout,
            )
            if getattr(process_result, "returncode", 0) != 0:
                raise UrlDownloadFailure("yt-dlp exited unsuccessfully")
            try:
                produced_path = output_path.resolve(strict=True)
                produced_path.relative_to(workspace.path)
            except (OSError, ValueError) as exc:
                raise UrlDownloadFailure(
                    "yt-dlp produced no in-workspace video"
                ) from exc
            if not produced_path.is_file() or produced_path.stat().st_size <= 0:
                raise UrlDownloadFailure("yt-dlp produced no non-empty video")
            return UrlDownloadResult(path=produced_path, filename=output_filename)
        except asyncio.CancelledError:
            await asyncio.to_thread(output_path.unlink, True)
            raise
        except UrlDownloadFailure:
            await asyncio.to_thread(output_path.unlink, True)
            raise
        except Exception as exc:
            await asyncio.to_thread(output_path.unlink, True)
            raise UrlDownloadFailure("yt-dlp download failed") from exc

    download_video = download

    def _command(self, url: PublicUrl, output_path: Path) -> tuple[str, ...]:
        args: list[str] = [
            self._executable,
            "--no-playlist",
            "--socket-timeout",
            str(YTDLP_SOCKET_TIMEOUT_SECONDS),
            "--retries",
            str(YTDLP_RETRIES),
            "--max-filesize",
            YTDLP_MAX_FILESIZE,
            "-f",
            YTDLP_FORMAT,
            "--merge-output-format",
            "mp4",
            "--recode-video",
            "mp4",
        ]
        if self._ffmpeg_location is not None and self._ffmpeg_location.strip():
            args.extend(("--ffmpeg-location", self._ffmpeg_location))
        args.extend(("-o", os.fspath(output_path), url.value))
        return tuple(args)


YtDlpUrlDownloader = YtDlpDownloader
UrlDownloader = YtDlpDownloader


def is_disallowed_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return true for local, private, reserved, multicast, or CGN addresses."""

    if (
        address.is_loopback
        or address.is_unspecified
        or address.is_link_local
        or address.is_private
        or address.is_reserved
        or address.is_multicast
    ):
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return address in ipaddress.IPv4Network("100.64.0.0/10")
    return address in ipaddress.IPv6Network("fc00::/7")


def _parse_address(
    value: str | ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        return ipaddress.ip_address(value)
    except ValueError as exc:
        raise UrlResolutionError("DNS returned an invalid address") from exc


def _tool_name(value: str | PathLike[str]) -> str:
    result = os.fspath(value)
    if isinstance(result, bytes):
        result = os.fsdecode(result)
    if not isinstance(result, str) or not result or "\x00" in result:
        raise ValueError("tool path must be non-empty text without NUL")
    return result


__all__ = [
    "PublicUrl",
    "PublicUrlValidator",
    "SystemDnsResolver",
    "UrlDownloader",
    "YTDLP_FORMAT",
    "YTDLP_MAX_FILESIZE",
    "YTDLP_RETRIES",
    "YTDLP_SOCKET_TIMEOUT_SECONDS",
    "YTDLP_TIMEOUT_SECONDS",
    "YtDlpDownloader",
    "YtDlpUrlDownloader",
    "is_disallowed_address",
]
