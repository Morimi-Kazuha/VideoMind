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

    if address == ipaddress.ip_address("168.63.129.16"):
        return True  # Azure platform virtual address; never a media source.
    if isinstance(address, ipaddress.IPv6Address):
        if (address in ipaddress.IPv6Network("64:ff9b::/96")
            or address in ipaddress.IPv6Network("64:ff9b:1::/48")):
            return True  # NAT64 can translate an apparently public IPv6 to private IPv4.
        if address.ipv4_mapped is not None:
            return is_disallowed_address(address.ipv4_mapped)
        if address.sixtofour is not None or address.teredo is not None:
            return True
    if not address.is_global:
        return True
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
    "DirectMp4Downloader",
    "validate_direct_url",
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


# UI-R2 direct-file boundary. This intentionally does not use yt-dlp.
DIRECT_MAX_BYTES = 100 * 1024 * 1024
DIRECT_TIMEOUT_SECONDS = 45


def validate_direct_url(value: str) -> str:
    """Only credential-free HTTPS MP4 URLs on the standard TLS port."""
    from urllib.parse import unquote
    try:
        parsed = urlsplit(value.strip())
        if (len(value) > 4096 or any(ord(c) < 33 for c in value.strip())
            or parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or parsed.fragment
            or not unquote(parsed.path).lower().endswith(".mp4")
            or "%" in parsed.hostname):
            raise ValueError()
    except (ValueError, AttributeError):
        raise UrlValidationError("当前仅支持公开 MP4 视频直链（HTTPS，无需登录）") from None
    return value.strip()


class _PinnedResolver:
    def __init__(self, host, addresses):
        self.host, self.addresses = host, addresses

    async def resolve(self, host, port=443, family=socket.AF_UNSPEC):
        if host != self.host:
            raise UrlValidationError("远程地址不匹配")
        return [dict(hostname=host, host=address, port=port,
                     family=socket.AF_INET6 if ":" in address else socket.AF_INET,
                     proto=socket.IPPROTO_TCP, flags=socket.AI_NUMERICHOST)
                for address in self.addresses]

    async def close(self):
        pass


class DirectMp4Downloader:
    """Bounded async download, pinned public DNS per hop, no proxy or cookies."""
    def __init__(self, resolver=None, *, max_bytes=DIRECT_MAX_BYTES,
                 timeout=DIRECT_TIMEOUT_SECONDS):
        self.resolver = resolver or SystemDnsResolver()
        self.max_bytes, self.timeout = max_bytes, timeout

    async def download(self, url: str, workspace: MediaWorkspace) -> UrlDownloadResult:
        import aiohttp
        from urllib.parse import urljoin
        target = (await workspace.directory("url")) / f"WEB_{uuid4().hex}.mp4"
        completed = False
        try:
            async with asyncio.timeout(self.timeout):
                current = url
                for hop in range(4):
                    current = validate_direct_url(current)
                    host = urlsplit(current).hostname
                    addresses = await self.resolver.resolve(host)
                    if not addresses or any(is_disallowed_address(_parse_address(a)) for a in addresses):
                        raise UrlValidationError("链接地址不可访问：禁止内网、回环或保留地址")
                    connector = aiohttp.TCPConnector(
                        resolver=_PinnedResolver(host, addresses), use_dns_cache=False, force_close=True)
                    async with aiohttp.ClientSession(
                        connector=connector, cookie_jar=aiohttp.DummyCookieJar(),
                        trust_env=False, auto_decompress=False,
                        timeout=aiohttp.ClientTimeout(total=None, sock_connect=8, sock_read=10),
                    ) as session:
                        async with session.get(current, allow_redirects=False,
                                               headers={"Accept-Encoding": "identity"}) as response:
                            if response.status in (301, 302, 303, 307, 308):
                                if hop == 3 or not response.headers.get("Location"):
                                    raise UrlDownloadFailure("重定向过多或无效")
                                current = urljoin(current, response.headers["Location"])
                                continue
                            if response.status != 200:
                                raise UrlDownloadFailure("远程服务器拒绝下载，请检查链接是否公开有效")
                            mime = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                            if mime not in ("video/mp4", "application/mp4", "application/octet-stream"):
                                raise UrlValidationError("当前仅支持公开 MP4 视频直链")
                            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                                raise UrlDownloadFailure("不支持压缩的远程文件")
                            length = response.content_length
                            if length is not None and length > self.max_bytes:
                                raise UrlValidationError("视频超过 100 MiB 导入限制")
                            size, header = 0, bytearray()
                            with target.open("xb") as output:
                                async for chunk in response.content.iter_chunked(64 * 1024):
                                    size += len(chunk)
                                    if size > self.max_bytes:
                                        raise UrlValidationError("视频超过 100 MiB 导入限制")
                                    if len(header) < 32:
                                        header.extend(chunk[:32-len(header)])
                                    output.write(chunk)
                            if not size or (length is not None and size != length):
                                raise UrlDownloadFailure("视频下载中断或文件为空")
                            # ISO BMFF signature plus an MP4-compatible major brand.
                            if (len(header) < 16 or header[4:8] != b"ftyp"
                                or header[8:12] not in (b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"MSNV", b"iso5", b"iso6")):
                                raise UrlValidationError("文件内容不是受支持的 MP4 视频")
                            completed = True
                            return UrlDownloadResult(path=target, filename=target.name)
        except TimeoutError as exc:
            raise UrlDownloadFailure("下载超时（最多 45 秒），请使用本地上传") from exc
        except (UrlValidationError, UrlResolutionError, UrlDownloadFailure):
            raise
        except (aiohttp.ClientError, OSError, ValueError) as exc:
            raise UrlDownloadFailure("远程下载失败或连接中断，请检查公开 MP4 直链") from exc
        finally:
            # Keep only a completed file; MediaWorkspace owns success cleanup.
            if not completed:
                target.unlink(missing_ok=True)
