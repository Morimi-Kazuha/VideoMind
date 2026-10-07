"""SiliconFlow text /rerank contract; not an OpenAI API extension.

Contract: https://docs.siliconflow.cn/docs/api/rerank-post
The application only sees candidate IDs and finite relevance scores.
"""

from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass, field
from collections.abc import Mapping
from inspect import isawaitable
from urllib.parse import urlsplit

from dovideo.application.errors import BudgetExceededError
from dovideo.application.ports.retrieval import RerankerDocument, RerankerResult

from .config import ProviderConfigurationError
from .errors import (ProviderAuthenticationError, ProviderRequestError,
                     ProviderResponseError, ProviderTransientError, ProviderTransportError)
from .http import StdlibAsyncJsonPostClient, post_json, response_parts


@dataclass(frozen=True, slots=True)
class RerankerConfig:
    enabled: bool = False
    url: str = "https://api.siliconflow.cn/v1/rerank"
    api_key: str = field(default="", repr=False)
    model: str = "BAAI/bge-reranker-v2-m3"
    timeout_seconds: float = 20.0
    max_attempts: int = 2
    retry_delay_seconds: float = 0.25

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (not isinstance(self.enabled, bool) or parsed.scheme not in {"http", "https"}
            or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not self.model.strip()):
            raise ProviderConfigurationError("reranker configuration is invalid")
        if self.enabled and not self.api_key.strip():
            raise ProviderConfigurationError("enabled reranker requires an API key")
        for value, positive in ((self.timeout_seconds, True), (self.retry_delay_seconds, False)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (positive and value == 0):
                raise ProviderConfigurationError("reranker numeric setting is invalid")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 5:
            raise ProviderConfigurationError("reranker max attempts must be in [1, 5]")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None):
        values = os.environ if environ is None else environ
        flag = values.get("DOVIDEO_RERANKER_ENABLED", "false").strip().lower()
        if flag not in {"true", "false", "1", "0", "yes", "no"}:
            raise ProviderConfigurationError("reranker enabled flag is invalid")
        if flag in {"false", "0", "no"}:
            return cls()  # Disabled configuration imposes no remote requirement.
        try:
            return cls(
                enabled=True,
                url=values.get("DOVIDEO_RERANKER_URL", cls().url).strip(),
                api_key=values.get("DOVIDEO_RERANKER_API_KEY", "").strip(),
                model=values.get("DOVIDEO_RERANKER_MODEL", cls().model).strip(),
                timeout_seconds=float(values.get("DOVIDEO_RERANKER_TIMEOUT_SECONDS", "20")),
                max_attempts=int(values.get("DOVIDEO_RERANKER_MAX_ATTEMPTS", "2")),
                retry_delay_seconds=float(values.get("DOVIDEO_RERANKER_RETRY_DELAY_SECONDS", "0.25")),
            )
        except (ValueError, TypeError, OverflowError):
            raise ProviderConfigurationError("reranker configuration is invalid") from None


class SiliconFlowRerankerAdapter:
    def __init__(self, config: RerankerConfig, *, client=None, sleeper=None):
        if not config.enabled:
            raise ProviderConfigurationError("construct reranker only when enabled")
        self.config = config
        self._client = client if client is not None else StdlibAsyncJsonPostClient()
        self._sleeper = sleeper if sleeper is not None else asyncio.sleep

    async def rerank(self, query: str, documents: tuple[RerankerDocument, ...]) -> tuple[RerankerResult, ...]:
        if not documents:
            return ()
        if not query.strip() or len({d.candidate_id for d in documents}) != len(documents):
            raise ProviderRequestError("reranker query/candidate identity is invalid")
        payload = {"model": self.config.model, "query": query,
                   "documents": [d.text for d in documents], "top_n": len(documents),
                   "return_documents": False}
        for attempt in range(self.config.max_attempts):
            try:
                async with asyncio.timeout(self.config.timeout_seconds):
                    response = await post_json(self._client, self.config.url,
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.config.api_key}"},
                        payload=payload, timeout=self.config.timeout_seconds)
                status, body = response_parts(response)
                if status in (401, 403):
                    raise ProviderAuthenticationError("reranker authentication failed")
                if status in (408, 429) or status >= 500:
                    raise ProviderTransientError(f"reranker transient HTTP failure ({status})")
                if not 200 <= status < 300:
                    raise ProviderRequestError(f"reranker request rejected ({status})")
                return decode_reranking(body, documents)
            except (asyncio.CancelledError, BudgetExceededError, TimeoutError):
                raise
            except (ProviderAuthenticationError, ProviderRequestError, ProviderResponseError):
                raise
            except (ProviderTransientError, OSError):
                if attempt + 1 >= self.config.max_attempts:
                    raise ProviderTransientError("reranker transient failure") from None
                delay = self._sleeper(self.config.retry_delay_seconds * 2 ** attempt)
                if isawaitable(delay):
                    await delay
            except Exception:
                raise ProviderTransportError("reranker transport failed") from None
        raise ProviderTransportError("reranker returned no response")

    async def aclose(self):
        close = getattr(self._client, "aclose", None)
        if callable(close):
            result = close()
            if isawaitable(result):
                await result


def decode_reranking(body, documents):
    results = body.get("results") if isinstance(body, Mapping) else None
    if not isinstance(results, list) or len(results) != len(documents):
        raise ProviderResponseError("reranker results are incomplete")
    scores = {}
    for result in results:
        if not isinstance(result, Mapping):
            raise ProviderResponseError("reranker result shape is invalid")
        index, score = result.get("index"), result.get("relevance_score")
        if (isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(documents)
            or index in scores or isinstance(score, bool) or not isinstance(score, (int, float))
            or not math.isfinite(float(score))):
            raise ProviderResponseError("reranker result index/score is invalid")
        scores[index] = float(score)
    return tuple(RerankerResult(documents[i].candidate_id, scores[i])
                 for i in sorted(scores, key=lambda i: (-scores[i], i)))


def configured_reranker(config: RerankerConfig | None = None, *, client=None):
    selected = config if config is not None else RerankerConfig.from_environment()
    return SiliconFlowRerankerAdapter(selected, client=client) if selected.enabled else None
