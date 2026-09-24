"""Configuration objects for optional real media/model providers.

The application and domain layers deliberately receive ports, not these
settings.  Environment variables are read only when :meth:`from_environment`
is called, so importing the package is side-effect free and local/fake
composition remains possible without credentials.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit


class ProviderConfigurationError(ValueError):
    """A provider setting is missing or outside its supported boundary."""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Small OpenAI-compatible provider setting shared by model/embedding.

    ``api_key`` is optional to support local OpenAI-compatible servers.  A
    remote deployment can require it in its composition root without making
    credentials a domain concern.  The key is excluded from ``repr`` and is
    never included in adapter errors.
    """

    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 120.0
    max_attempts: int = 3
    retry_delay_seconds: float = 0.0
    embedding_model: str | None = None
    embedding_url: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or not self.base_url.strip():
            raise ProviderConfigurationError("provider base URL is required")
        base_url = self.base_url.strip().rstrip("/")
        # Accept a fully qualified chat/embedding endpoint as a convenience,
        # while keeping one normalized base for both OpenAI-compatible paths.
        for suffix in ("/chat/completions", "/embeddings"):
            if base_url.endswith(suffix):
                base_url = base_url[: -len(suffix)]
                break
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProviderConfigurationError("provider base URL must be HTTP(S)")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ProviderConfigurationError("provider model is required")
        if self.api_key is not None:
            if not isinstance(self.api_key, str):
                raise ProviderConfigurationError("provider api key must be text")
            api_key = self.api_key.strip() or None
        else:
            api_key = None
        timeout = _finite_positive(self.timeout_seconds, "timeout_seconds")
        attempts = _positive_int(self.max_attempts, "max_attempts")
        delay = _finite_nonnegative(self.retry_delay_seconds, "retry_delay_seconds")
        embedding_model = self.embedding_model
        if embedding_model is not None:
            if not isinstance(embedding_model, str) or not embedding_model.strip():
                raise ProviderConfigurationError(
                    "embedding_model must be nonblank when provided"
                )
            embedding_model = embedding_model.strip()
        embedding_url = self.embedding_url
        if embedding_url is not None:
            if not isinstance(embedding_url, str) or not embedding_url.strip():
                raise ProviderConfigurationError(
                    "embedding_url must be nonblank when provided"
                )
            embedding_url = embedding_url.strip().rstrip("/")
            embedding_parts = urlsplit(embedding_url)
            if embedding_parts.scheme not in {"http", "https"} or not embedding_parts.netloc:
                raise ProviderConfigurationError("embedding_url must be HTTP(S)")
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "max_attempts", attempts)
        object.__setattr__(self, "retry_delay_seconds", delay)
        object.__setattr__(self, "embedding_model", embedding_model)
        object.__setattr__(self, "embedding_url", embedding_url)

    @property
    def chat_url(self) -> str:
        """OpenAI-compatible chat completion endpoint."""

        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    @property
    def embeddings_url(self) -> str:
        """OpenAI-compatible embedding endpoint."""

        return self.embedding_url or f"{self.base_url}/embeddings"

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        prefix: str = "DOVIDEO_",
        required: bool = False,
    ) -> "ProviderConfig | None":
        """Build settings from environment without reading it at import time.

        The names intentionally mirror the Java properties while using a
        project-specific prefix: ``DOVIDEO_MODEL_BASE_URL``,
        ``DOVIDEO_MODEL_API_KEY``, ``DOVIDEO_MODEL_MODEL`` and
        ``DOVIDEO_MODEL_TIMEOUT_SECONDS``.  ``required=False`` returns
        ``None`` when no endpoint is configured, which is useful for offline
        tests and local composition.
        """

        values = os.environ if environ is None else environ
        endpoint = _first_value(
            values,
            f"{prefix}MODEL_BASE_URL",
            f"{prefix}BASE_URL",
            f"{prefix}DEEPSEEK_BASE_URL",
            "SILICONFLOW_BASE_URL",
        )
        if endpoint is None:
            if required:
                raise ProviderConfigurationError("provider base URL is required")
            return None
        model = _first_value(
            values,
            f"{prefix}MODEL",
            f"{prefix}MODEL_MODEL",
            f"{prefix}MODEL_NAME",
            f"{prefix}LLM_MODEL",
            "LLM_MODEL",
        ) or "deepseek-ai/DeepSeek-V3.2"
        api_key = _first_value(
            values,
            f"{prefix}MODEL_API_KEY",
            f"{prefix}API_KEY",
            f"{prefix}DEEPSEEK_API_KEY",
            "SILICONFLOW_API_KEY",
        )
        embedding_model = _first_value(
            values,
            f"{prefix}EMBEDDING_MODEL",
            "EMBEDDING_MODEL",
        ) or "BAAI/bge-m3"
        timeout_text = _first_value(
            values,
            f"{prefix}MODEL_TIMEOUT_SECONDS",
            f"{prefix}TIMEOUT_SECONDS",
            f"{prefix}LLM_TIMEOUT_SECONDS",
            "LLM_TIMEOUT_SECONDS",
        )
        attempts_text = _first_value(values, f"{prefix}MODEL_MAX_ATTEMPTS")
        delay_text = _first_value(values, f"{prefix}MODEL_RETRY_DELAY_SECONDS")
        try:
            timeout = float(timeout_text) if timeout_text is not None else 120.0
            attempts = int(attempts_text) if attempts_text is not None else 3
            delay = float(delay_text) if delay_text is not None else 0.0
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProviderConfigurationError("provider numeric setting is invalid") from exc
        return cls(
            base_url=endpoint,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout,
            max_attempts=attempts,
            retry_delay_seconds=delay,
            embedding_model=embedding_model,
        )


# Names used by composition roots during migration; they intentionally refer
# to one setting model rather than creating provider-specific config schemas.
ModelProviderConfig = ProviderConfig
EmbeddingProviderConfig = ProviderConfig


def _first_value(values: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = values.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _finite_positive(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderConfigurationError(f"{field_name} must be finite and positive")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ProviderConfigurationError(f"{field_name} must be finite and positive")
    return result


def _finite_nonnegative(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderConfigurationError(
            f"{field_name} must be finite and non-negative"
        )
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ProviderConfigurationError(
            f"{field_name} must be finite and non-negative"
        )
    return result


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProviderConfigurationError(f"{field_name} must be a positive integer")
    return value


__all__ = [
    "EmbeddingProviderConfig",
    "ModelProviderConfig",
    "ProviderConfig",
    "ProviderConfigurationError",
]
