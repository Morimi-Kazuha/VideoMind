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


MODEL_REASONING_EFFORTS = frozenset({"none", "low", "high", "max"})
MAX_MODEL_OUTPUT_TOKENS = 393_216


class ProviderConfigurationError(ValueError):
    """A provider setting is missing or outside its supported boundary."""


@dataclass(frozen=True, slots=True)
class ModelRequestSettings:
    """Optional provider-neutral request controls owned by a model profile.

    ``None`` leaves a field out of the HTTP request, preserving the provider's
    existing default.  The settings contain no model credentials.
    """

    reasoning_effort: str | None = None
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        effort = self.reasoning_effort
        if effort is not None:
            if not isinstance(effort, str) or effort not in MODEL_REASONING_EFFORTS:
                raise ProviderConfigurationError(
                    "reasoning_effort must be one of none, low, high, or max"
                )
        max_tokens = self.max_tokens
        if max_tokens is not None:
            if (
                isinstance(max_tokens, bool)
                or not isinstance(max_tokens, int)
                or not 1 <= max_tokens <= MAX_MODEL_OUTPUT_TOKENS
            ):
                raise ProviderConfigurationError(
                    f"max_tokens must be an integer in [1, {MAX_MODEL_OUTPUT_TOKENS}]"
                )

    def request_fields(self) -> dict[str, str | int]:
        """Return only explicitly configured Chat Completions fields."""

        fields: dict[str, str | int] = {}
        if self.reasoning_effort is not None:
            fields["reasoning_effort"] = self.reasoning_effort
        if self.max_tokens is not None:
            fields["max_tokens"] = self.max_tokens
        return fields


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
    transport: str = "openai-compatible"
    provider_only: tuple[str, ...] = ()
    provider_data_collection: str | None = None
    provider_zdr: bool | None = None

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
        if self.transport not in {"openai-compatible", "openrouter"}:
            raise ProviderConfigurationError("unsupported model transport")
        if self.transport == "openrouter":
            if base_url != "https://openrouter.ai/api/v1":
                raise ProviderConfigurationError("OpenRouter transport requires its API base URL")
            if not api_key:
                raise ProviderConfigurationError("OpenRouter API key is required")
            if not self.model.strip().startswith("deepseek/"):
                raise ProviderConfigurationError("DeepSeek execution requires an OpenRouter DeepSeek model slug")
            if not self.provider_only or any(not item.strip() for item in self.provider_only):
                raise ProviderConfigurationError("OpenRouter execution provider pin is required")
            if self.provider_data_collection != "deny" or self.provider_zdr is not True:
                raise ProviderConfigurationError("OpenRouter execution requires deny collection and ZDR")
        elif self.provider_only or self.provider_data_collection is not None or self.provider_zdr is not None:
            raise ProviderConfigurationError("provider policy requires OpenRouter transport")
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
        transport = _first_value(values, f"{prefix}MODEL_TRANSPORT") or "openai-compatible"
        if transport == "openrouter":
            key = _first_value(values, f"{prefix}OPENROUTER_API_KEY")
            provider_tag = _first_value(values, f"{prefix}MODEL_PROVIDER_TAG")
            if provider_tag is None:
                raise ProviderConfigurationError("OpenRouter execution provider tag is required")
            try:
                timeout = float(_first_value(values, f"{prefix}MODEL_TIMEOUT_SECONDS") or 120)
                attempts = int(_first_value(values, f"{prefix}MODEL_MAX_ATTEMPTS") or 3)
                delay = float(_first_value(values, f"{prefix}MODEL_RETRY_DELAY_SECONDS") or 0)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ProviderConfigurationError("provider numeric setting is invalid") from exc
            return cls(
                base_url="https://openrouter.ai/api/v1",
                model=_first_value(values, f"{prefix}BALANCED_MODEL", f"{prefix}MODEL_MODEL")
                or "deepseek/deepseek-v4.1-flash",
                api_key=key,
                transport="openrouter",
                provider_only=(provider_tag,),
                provider_data_collection="deny",
                provider_zdr=True,
                timeout_seconds=timeout,
                max_attempts=attempts,
                retry_delay_seconds=delay,
            )
        if transport != "openai-compatible":
            raise ProviderConfigurationError("unsupported model transport")
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
    "MAX_MODEL_OUTPUT_TOKENS",
    "MODEL_REASONING_EFFORTS",
    "ModelRequestSettings",
    "ModelProviderConfig",
    "ProviderConfig",
    "ProviderConfigurationError",
]
