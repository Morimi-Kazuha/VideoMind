"""Driver-neutral asynchronous JSON HTTP boundary for provider adapters."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class ProviderHttpResponse:
    """Minimal response value understood by model and embedding adapters."""

    status_code: int
    body: Any = None


class AsyncJsonPostClient(Protocol):
    """The only transport surface required by this provider package."""

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Any,
        timeout: float,
    ) -> object:
        ...


class StdlibAsyncJsonPostClient:
    """No-dependency JSON client; network I/O is deferred to invocation."""

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Any,
        timeout: float,
    ) -> ProviderHttpResponse:
        return await asyncio.to_thread(
            _post_json_sync,
            url,
            headers,
            json,
            timeout,
        )


def _post_json_sync(
    url: str,
    headers: Mapping[str, str],
    payload: Any,
    timeout: float,
) -> ProviderHttpResponse:
    request_headers = dict(headers)
    request_headers.setdefault("Content-Type", "application/json")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    request = Request(
        url,
        data=encoded,
        headers=request_headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return ProviderHttpResponse(
                status_code=int(response.status),
                body=_decode_body(response.read()),
            )
    except HTTPError as exc:
        # An HTTP response is data, not an exception boundary.  The provider
        # adapter maps the status to a safe typed error without exposing body.
        return ProviderHttpResponse(
            status_code=int(exc.code),
            body=_decode_body(exc.read()),
        )


def _decode_body(value: bytes) -> Any:
    text = value.decode("utf-8", errors="replace")
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # The caller will produce a typed malformed-response error.  Keeping
        # the bounded text here helps injected response compatibility but is
        # never interpolated into a public error message.
        return text


async def post_json(
    client: object,
    url: str,
    *,
    headers: Mapping[str, str],
    payload: Any,
    timeout: float,
) -> object:
    """Call either a ``post`` fake/client or the existing qdrant-style API.

    Supporting the existing ``request`` shape makes the adapter easy to wire
    with the repository's standard-library test transport while keeping HTTP
    concepts out of application ports.
    """

    post = getattr(client, "post", None)
    if callable(post):
        value = post(url, headers=headers, json=payload, timeout=timeout)
    else:
        request = getattr(client, "request", None)
        if not callable(request):
            raise TypeError("provider HTTP client must provide post()")
        value = request(
            "POST",
            url,
            headers=headers,
            json=payload,
            timeout=timeout,
        )
    if isawaitable(value):
        return await value
    return value


def response_parts(response: object) -> tuple[int, Any]:
    """Extract status/body from stdlib, httpx-like, or direct fake values."""

    if isinstance(response, Mapping):
        if "status_code" in response:
            status = int(response["status_code"])
            body = response.get("body")
            if body is None:
                body = response.get("json")
            if callable(body):
                body = body()
            return status, body
        return 200, response
    status_value = getattr(response, "status_code", None)
    if status_value is None:
        # A direct decoded response is convenient for deterministic local
        # fakes and is equivalent to a successful HTTP response.
        return 200, response
    status = int(status_value)
    body = getattr(response, "body", None)
    if body is None:
        json_method = getattr(response, "json", None)
        if callable(json_method):
            body = json_method()
        else:
            body = getattr(response, "text", None)
    return status, body


__all__ = [
    "AsyncJsonPostClient",
    "ProviderHttpResponse",
    "StdlibAsyncJsonPostClient",
    "post_json",
    "response_parts",
]
