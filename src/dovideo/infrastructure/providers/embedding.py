"""OpenAI-compatible embedding adapter for the existing ``EmbeddingPort``."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from inspect import isawaitable
from typing import Any

from dovideo.application.ports.ai import EmbeddingPort

from .config import ProviderConfig
from .errors import (
    EmbeddingResponseError,
    ProviderAuthenticationError,
    ProviderError,
    ProviderRequestError,
    ProviderTransportError,
    ProviderTransientError,
)
from .http import AsyncJsonPostClient, StdlibAsyncJsonPostClient, post_json, response_parts


class OpenAICompatibleEmbeddingAdapter(EmbeddingPort):
    """Map ``POST /embeddings`` JSON to an immutable finite float vector.

    Blank input follows the Java utility's empty-vector behavior.  Provider
    failures are deliberately not converted to an empty vector; the existing
    chunking/retrieval application services own their documented fallbacks.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        client: AsyncJsonPostClient | object | None = None,
        http_client: AsyncJsonPostClient | object | None = None,
        sleeper: Any | None = None,
    ) -> None:
        if not isinstance(config, ProviderConfig):
            raise TypeError("config must be a ProviderConfig")
        self.config = config
        self._client = client if client is not None else http_client
        if self._client is None:
            self._client = StdlibAsyncJsonPostClient()
        self._sleeper = sleeper or asyncio.sleep

    async def embed(self, text: str) -> tuple[float, ...]:
        if not isinstance(text, str):
            raise TypeError("embedding text must be text")
        if not text.strip():
            return ()
        payload = {
            "model": self.config.embedding_model or self.config.model,
            "input": text,
        }
        last_error: ProviderTransientError | None = None
        for attempt in range(self.config.max_attempts):
            try:
                response = await post_json(
                    self._client,
                    self.config.embeddings_url,
                    headers=self._headers(),
                    payload=payload,
                    timeout=self.config.timeout_seconds,
                )
                status, body = response_parts(response)
                if status in (401, 403):
                    raise ProviderAuthenticationError(
                        "embedding provider authentication failed"
                    )
                if status in (408, 429) or status >= 500:
                    raise ProviderTransientError(
                        f"embedding provider transient HTTP failure ({status})"
                    )
                if status < 200 or status >= 300:
                    raise ProviderRequestError(
                        f"embedding provider rejected request ({status})"
                    )
                return decode_embedding(body)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                # Preserve provider-local timeout identity.  AgentLoop's
                # outer deadline distinguishes its own elapsed wait.
                raise
            except (ProviderAuthenticationError, ProviderRequestError, EmbeddingResponseError):
                raise
            except ProviderTransientError as exc:
                last_error = exc
                if attempt + 1 >= self.config.max_attempts:
                    raise
                await self._sleep_before_retry(attempt)
            except OSError as exc:
                last_error = ProviderTransientError("embedding provider transport failed")
                if attempt + 1 >= self.config.max_attempts:
                    raise last_error from exc
                await self._sleep_before_retry(attempt)
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderTransportError(
                    "embedding provider transport failed"
                ) from exc
        if last_error is not None:
            raise last_error
        raise EmbeddingResponseError("embedding provider returned no response")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def _sleep_before_retry(self, attempt: int) -> None:
        value = self._sleeper(self.config.retry_delay_seconds * (2**attempt))
        if isawaitable(value):
            await value

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None)
        if callable(close):
            value = close()
            if isawaitable(value):
                await value


EmbeddingAdapter = OpenAICompatibleEmbeddingAdapter


def decode_embedding(body: Any) -> tuple[float, ...]:
    """Decode OpenAI ``data[0].embedding`` or a direct local vector."""

    values: Any = None
    if isinstance(body, Mapping):
        values = body.get("embedding")
        if values is None:
            data = body.get("data")
            if isinstance(data, Sequence) and not isinstance(data, (str, bytes)) and data:
                first = data[0]
                if isinstance(first, Mapping):
                    values = first.get("embedding")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise EmbeddingResponseError("embedding response vector is missing")
    if not values:
        raise EmbeddingResponseError("embedding response vector is empty")
    vector: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EmbeddingResponseError("embedding response contains a non-number")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise EmbeddingResponseError("embedding response contains a non-finite value")
        vector.append(numeric)
    return tuple(vector)


__all__ = [
    "EmbeddingAdapter",
    "OpenAICompatibleEmbeddingAdapter",
    "decode_embedding",
]
