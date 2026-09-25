"""Segmented ASR orchestration and an HTTP multipart ASR adapter."""

from __future__ import annotations

import asyncio
import json
import math
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from dovideo.application import AsrBranchOutcome, TranscriptSpan
from dovideo.application.ports.ai import AudioSegmentTranscriptionPort
from dovideo.application.ports.observability import TelemetryPort

from .audio import AUDIO_SEGMENT_MILLISECONDS, AudioSegment
from .errors import (
    AllAsrSegmentsFailed,
    AsrAudioMissing,
    AsrRequestRejected,
    AsrResponseError,
    AsrTransientFailure,
)

MAX_ASR_ATTEMPTS = 3
ASR_RETRY_BASE_SECONDS = 1.0
ASR_CONTENT_TYPE = "application/octet-stream"


class AsyncHttpClient(Protocol):
    """Minimal async client shape shared by stdlib and httpx clients."""

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        files: Mapping[str, tuple[str, bytes, str]],
        data: Mapping[str, str],
    ) -> object:
        ...


class AsrHttpResponse(Protocol):
    """Response shape consumed from httpx or the stdlib fallback client."""

    status_code: int

    def json(self) -> object:
        ...


class _StdlibResponse:
    __slots__ = ("status_code", "_body")

    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._body = body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    def json(self) -> object:
        return json.loads(self.text)


class StdlibAsyncHttpClient:
    """Small no-dependency multipart client used when httpx is not injected."""

    def __init__(self, *, timeout: float = 180.0) -> None:
        if timeout <= 0:
            raise ValueError("HTTP timeout must be positive")
        self._timeout = timeout

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        files: Mapping[str, tuple[str, bytes, str]],
        data: Mapping[str, str],
    ) -> _StdlibResponse:
        return await asyncio.to_thread(
            _post_multipart,
            url,
            headers,
            files,
            data,
            self._timeout,
        )


def _post_multipart(
    url: str,
    headers: Mapping[str, str],
    files: Mapping[str, tuple[str, bytes, str]],
    data: Mapping[str, str],
    timeout: float,
) -> _StdlibResponse:
    boundary = "----dovideo-asr-boundary"
    body = bytearray()
    for name, value in data.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")
    for name, (filename, content, content_type) in files.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
        )
        body.extend(content)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    request_headers = dict(headers)
    request_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    request = urllib.request.Request(url, data=bytes(body), headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _StdlibResponse(response.status, response.read())
    except urllib.error.HTTPError as exc:
        return _StdlibResponse(exc.code, exc.read())


class HttpAsrAdapter:
    """Call an OpenAI-compatible multipart transcription endpoint.

    The adapter accepts an injected ``httpx.AsyncClient`` (including
    ``MockTransport``) or any client matching :class:`AsyncHttpClient`.  Its
    default client uses only the standard library and is intentionally not
    exercised by unit tests against a live endpoint.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        model: str,
        *,
        client: AsyncHttpClient | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        max_attempts: int = MAX_ASR_ATTEMPTS,
        retry_base_seconds: float = ASR_RETRY_BASE_SECONDS,
    ) -> None:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("ASR URL is required")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("ASR API key is required")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("ASR model is required")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        retry_base_is_finite = False
        if isinstance(retry_base_seconds, (int, float)) and not isinstance(
            retry_base_seconds, bool
        ):
            try:
                retry_base_is_finite = math.isfinite(retry_base_seconds)
            except (OverflowError, ValueError):
                retry_base_is_finite = False
        if (
            not retry_base_is_finite
            or retry_base_seconds < 0  # type: ignore[operator]
        ):
            raise ValueError("retry_base_seconds must be finite and non-negative")
        self._url = url.strip()
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._client = client if client is not None else StdlibAsyncHttpClient()
        self._sleeper = sleeper or asyncio.sleep
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds

    async def transcribe_segment(
        self,
        audio_path: Path,
        *,
        trace_id: str | None = None,
    ) -> str:
        del trace_id  # opaque tracing is owned by the caller; never log secrets
        path = Path(audio_path)
        if not path.is_file():
            raise AsrAudioMissing("ASR audio file does not exist")
        content = await asyncio.to_thread(path.read_bytes)
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                response = await self._client.post(
                    self._url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    files={"file": (path.name, content, ASR_CONTENT_TYPE)},
                    data={"model": self._model},
                )
                status_code = int(getattr(response, "status_code"))
                if status_code == 429 or status_code >= 500:
                    transient = AsrTransientFailure(
                        f"ASR transient HTTP failure ({status_code})"
                    )
                    last_error = transient
                    if attempt + 1 < self._max_attempts:
                        await self._sleep_before_retry(attempt)
                        continue
                    raise transient
                if 400 <= status_code < 500:
                    raise AsrRequestRejected(status_code)
                if status_code < 200 or status_code >= 300:
                    raise AsrResponseError(
                        f"ASR unexpected HTTP status ({status_code})"
                    )
                try:
                    payload = response.json()  # type: ignore[union-attr]
                except Exception as exc:
                    raise AsrResponseError("ASR response was not valid JSON") from exc
                if not isinstance(payload, Mapping):
                    raise AsrResponseError("ASR response JSON was not an object")
                text = payload.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise AsrResponseError("ASR response contained empty text")
                return text.strip()
            except (AsrRequestRejected, AsrResponseError):
                raise
            except asyncio.CancelledError:
                raise
            except AsrTransientFailure:
                raise
            except Exception as exc:
                # Transport exceptions (including httpx network/timeout
                # errors) are retryable, but never expose request headers or
                # response bodies in the resulting error.
                last_error = exc
                if attempt + 1 >= self._max_attempts:
                    failure = AsrTransientFailure(
                        "ASR transport failed after maximum retries"
                    )
                    raise failure from exc
                await self._sleep_before_retry(attempt)
        failure = AsrTransientFailure("ASR failed after maximum retries")
        if last_error is None:
            raise failure
        raise failure from last_error

    async def _sleep_before_retry(self, attempt: int) -> None:
        await self._sleeper(self._retry_base_seconds * (2**attempt))

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    audio_to_text = transcribe_segment


class SegmentedTranscriptionService:
    """Sequentially transcribe 3A audio artifacts into timestamped spans."""

    def __init__(
        self,
        transcriber: AudioSegmentTranscriptionPort,
        telemetry: TelemetryPort | None = None,
    ) -> None:
        self._transcriber = transcriber
        self._telemetry = telemetry

    async def transcribe(
        self,
        segments: Sequence[AudioSegment],
        *,
        trace_id: str | None = None,
    ) -> AsrBranchOutcome:
        observations: list[TranscriptSpan] = []
        causes: list[Exception] = []
        normalized = tuple(segments)
        for segment in normalized:
            self._increment("asrCalls")
            try:
                text = await self._transcriber.transcribe_segment(
                    segment.path,
                    trace_id=trace_id,
                )
                if isinstance(text, tuple):
                    timed_observations: list[TranscriptSpan] = []
                    for span in text:
                        if not isinstance(span, TranscriptSpan):
                            raise TypeError("timed ASR observations must be TranscriptSpan values")
                        if span.end_ms > AUDIO_SEGMENT_MILLISECONDS:
                            raise ValueError("timed ASR observation exceeds audio segment")
                        if span.text.strip():
                            timed_observations.append(
                                TranscriptSpan(
                                    start_ms=segment.start_ms + span.start_ms,
                                    end_ms=segment.start_ms + span.end_ms,
                                    text=span.text,
                                )
                            )
                    observations.extend(timed_observations)
                elif isinstance(text, str) and text.strip():
                    start_ms = segment.index * AUDIO_SEGMENT_MILLISECONDS
                    observations.append(
                        TranscriptSpan(
                            start_ms=start_ms,
                            end_ms=start_ms + AUDIO_SEGMENT_MILLISECONDS,
                            text=text,
                        )
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                causes.append(exc)
                self._increment("asrSegmentFailures")
        if not observations and causes:
            failure = AllAsrSegmentsFailed(causes, attempted=len(normalized))
            raise failure from failure.last_cause
        return AsrBranchOutcome(
            observations=tuple(observations),
            attempted=len(normalized),
            failed=len(causes),
            causes=tuple(causes),
        )

    transcribe_segments = transcribe

    async def transcribe_spans(
        self,
        segments: Sequence[AudioSegment],
        *,
        trace_id: str | None = None,
    ) -> tuple[TranscriptSpan, ...]:
        return (await self.transcribe(segments, trace_id=trace_id)).observations

    def _increment(self, metric: str) -> None:
        if self._telemetry is not None:
            self._telemetry.increment(metric)


SegmentedAsrService = SegmentedTranscriptionService
AsrHttpClientAdapter = HttpAsrAdapter
AsrHttpAdapter = HttpAsrAdapter
AliyunAsrAdapter = HttpAsrAdapter


__all__ = [
    "ASR_CONTENT_TYPE",
    "ASR_RETRY_BASE_SECONDS",
    "AsyncHttpClient",
    "AsrHttpAdapter",
    "AsrHttpClientAdapter",
    "AliyunAsrAdapter",
    "HttpAsrAdapter",
    "SegmentedAsrService",
    "SegmentedTranscriptionService",
    "StdlibAsyncHttpClient",
]
