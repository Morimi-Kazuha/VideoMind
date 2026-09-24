"""Typed, secret-safe failures at the real provider boundary."""

from __future__ import annotations


class ProviderError(RuntimeError):
    """Base class for provider adapter failures."""


class ProviderTransportError(ProviderError):
    """A network or transport operation failed."""


class ProviderTransientError(ProviderTransportError):
    """A status/transport failure that may be retried by the adapter."""


class ProviderAuthenticationError(ProviderError):
    """The provider rejected credentials or authentication headers."""


class ProviderRequestError(ProviderError):
    """The provider rejected a request as non-retryable."""


class ProviderResponseError(ProviderError):
    """A successful response could not be decoded into the requested DTO."""


class EmbeddingProviderError(ProviderError):
    """Base class for embedding-specific adapter failures."""


class EmbeddingResponseError(EmbeddingProviderError):
    """An embedding response was malformed or contained invalid values."""


class ModelProviderError(ProviderError):
    """Base class for structured model-role adapter failures."""


class ModelResponseError(ModelProviderError):
    """A model response was not valid JSON or did not validate as its DTO."""

    def __init__(
        self,
        message: str = "",
        *,
        diagnostic: str | None = None,
    ) -> None:
        """Keep an optional already-bounded structural diagnostic with the error.

        The diagnostic is deliberately separate from the exception class and
        remains optional so all existing provider failures keep their current
        classification and construction contract.
        """

        self.diagnostic = diagnostic if isinstance(diagnostic, str) else None
        super().__init__(message)


__all__ = [
    "EmbeddingProviderError",
    "EmbeddingResponseError",
    "ModelProviderError",
    "ModelResponseError",
    "ProviderAuthenticationError",
    "ProviderError",
    "ProviderRequestError",
    "ProviderResponseError",
    "ProviderTransportError",
    "ProviderTransientError",
]
