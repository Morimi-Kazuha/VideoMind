"""Small dependency-free local embedding fallback.

The Java implementation delegates embeddings to its configured provider.  A
real provider is still preferred in production; this adapter is a deterministic
CPU fallback for local/offline composition.  It learns a bounded TF-IDF
vocabulary from the supplied media text and returns finite unit vectors.  It
does not claim to be a pretrained semantic model.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

from dovideo.application.ports.ai import EmbeddingPort


_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


class LocalTfidfEmbeddingAdapter(EmbeddingPort):
    """Fit a bounded TF-IDF space and expose it through ``EmbeddingPort``.

    ``fit`` must be called with the real text corpus before ``embed``.  The
    fitted state is intentionally explicit so callers cannot mistake an
    unconfigured adapter for a remote model or silently receive random
    vectors.
    """

    def __init__(self, *, max_features: int = 256) -> None:
        if isinstance(max_features, bool) or not isinstance(max_features, int):
            raise TypeError("max_features must be an integer")
        if max_features <= 0:
            raise ValueError("max_features must be positive")
        self.max_features = max_features
        self._vocabulary: dict[str, int] = {}
        self._idf: tuple[float, ...] = ()

    @property
    def dimension(self) -> int:
        """Number of coordinates in the currently fitted local space."""

        return len(self._vocabulary)

    def fit(self, documents: Iterable[str]) -> "LocalTfidfEmbeddingAdapter":
        """Fit from non-secret source documents in deterministic order."""

        normalized = tuple(self._tokens(document) for document in documents)
        if not normalized:
            raise ValueError("at least one document is required")
        document_frequency: Counter[str] = Counter()
        first_seen: dict[str, int] = {}
        sequence = 0
        for tokens in normalized:
            for token in set(tokens):
                document_frequency[token] += 1
                first_seen.setdefault(token, sequence)
                sequence += 1
        if not document_frequency:
            raise ValueError("documents contain no usable tokens")
        terms = sorted(
            document_frequency,
            key=lambda token: (-document_frequency[token], first_seen[token], token),
        )[: self.max_features]
        self._vocabulary = {term: index for index, term in enumerate(terms)}
        count = len(normalized)
        self._idf = tuple(
            math.log((1.0 + count) / (1.0 + document_frequency[term])) + 1.0
            for term in terms
        )
        return self

    async def embed(self, text: str) -> tuple[float, ...]:
        """Return a finite unit TF-IDF vector for one text value."""

        if not isinstance(text, str):
            raise TypeError("embedding text must be text")
        if not text.strip():
            return ()
        if not self._vocabulary:
            raise RuntimeError("local embedding adapter must be fitted first")
        counts = Counter(self._tokens(text))
        vector = [0.0] * len(self._vocabulary)
        for term, index in self._vocabulary.items():
            frequency = counts.get(term, 0)
            if frequency:
                vector[index] = (1.0 + math.log(float(frequency))) * self._idf[index]
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return tuple(0.0 for _ in vector)
        result = tuple(value / norm for value in vector)
        if not all(math.isfinite(value) for value in result):
            raise ValueError("local embedding produced a non-finite vector")
        return result

    @staticmethod
    def _tokens(text: str) -> tuple[str, ...]:
        if not isinstance(text, str):
            raise TypeError("embedding documents must be text")
        return tuple(match.group(0).casefold() for match in _TOKEN_RE.finditer(text))


LocalEmbeddingAdapter = LocalTfidfEmbeddingAdapter


__all__ = ["LocalEmbeddingAdapter", "LocalTfidfEmbeddingAdapter"]
