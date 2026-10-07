import asyncio

import pytest

from dovideo.application.ports.retrieval import RerankerDocument, RerankerResult
from dovideo.infrastructure.providers.reranker import RerankerConfig, SiliconFlowRerankerAdapter, configured_reranker
from dovideo.infrastructure.providers.errors import ProviderAuthenticationError, ProviderRequestError, ProviderResponseError, ProviderTransientError
from dovideo.infrastructure.providers.config import ProviderConfigurationError
from dovideo.infrastructure.providers.http import ProviderHttpResponse


DOCS = (RerankerDocument("a", "first"), RerankerDocument("b", "second"))
BODY = {"results": [{"index": 1, "relevance_score": 200.0}, {"index": 0, "relevance_score": -3.0}]}


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def adapter(client, **kwargs):
    return SiliconFlowRerankerAdapter(RerankerConfig(enabled=True, api_key="test-secret", **kwargs), client=client)


def test_config_disabled_default_and_separate_provider_requirements():
    assert configured_reranker(RerankerConfig()) is None
    assert not RerankerConfig.from_environment({"DOVIDEO_RERANKER_ENABLED": "false", "DOVIDEO_RERANKER_TIMEOUT_SECONDS": "invalid"}).enabled
    enabled = RerankerConfig.from_environment({"DOVIDEO_RERANKER_ENABLED": "true", "DOVIDEO_RERANKER_API_KEY": "secret"})
    assert enabled.model == "BAAI/bge-reranker-v2-m3"
    assert "secret" not in repr(enabled)
    for values in ({"DOVIDEO_RERANKER_ENABLED": "maybe"}, {"DOVIDEO_RERANKER_ENABLED": "true"},
                   {"DOVIDEO_RERANKER_ENABLED": "true", "DOVIDEO_RERANKER_API_KEY": "secret", "DOVIDEO_RERANKER_TIMEOUT_SECONDS": "nan"}):
        with pytest.raises(ProviderConfigurationError):
            RerankerConfig.from_environment(values)


@pytest.mark.asyncio
async def test_transport_payload_and_identity_mapping():
    client = Client(ProviderHttpResponse(200, BODY))
    out = await adapter(client).rerank("query", DOCS)
    assert out == (RerankerResult("b", 200), RerankerResult("a", -3))
    url, request = client.calls[0]
    assert url == "https://api.siliconflow.cn/v1/rerank"
    assert request["json"] == {"model": "BAAI/bge-reranker-v2-m3", "query": "query", "documents": ["first", "second"], "top_n": 2, "return_documents": False}
    assert request["headers"]["Authorization"] == "Bearer test-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [(401, ProviderAuthenticationError), (403, ProviderAuthenticationError), (400, ProviderRequestError), (422, ProviderRequestError)])
async def test_auth_request_errors_no_retry_or_secret(status, error):
    client = Client(ProviderHttpResponse(status, {"secret": "test-secret"}))
    with pytest.raises(error) as caught:
        await adapter(client).rerank("query", DOCS)
    assert "test-secret" not in str(caught.value)
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [ProviderHttpResponse(429, {}), ProviderHttpResponse(503, {}), OSError("secret")])
async def test_transient_retry_is_bounded(first):
    client = Client(first, ProviderHttpResponse(200, BODY))
    assert await adapter(client, retry_delay_seconds=0).rerank("query", DOCS)
    assert len(client.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, {}, {"results": []}, {"results": [{"index": 0, "relevance_score": 1}]},
    {"results": [{"index": 0, "relevance_score": 1}, {"index": 0, "relevance_score": 2}]},
    {"results": [{"index": True, "relevance_score": 1}, {"index": 1, "relevance_score": 2}]},
    {"results": [{"index": 0, "relevance_score": float("nan")}, {"index": 1, "relevance_score": 2}]},
    {"results": [{"index": 0, "relevance_score": 1}, {"index": 2, "relevance_score": 2}]}])
async def test_malformed_incomplete_duplicate_or_nonfinite_response(body):
    with pytest.raises(ProviderResponseError):
        await adapter(Client(ProviderHttpResponse(200, body))).rerank("query", DOCS)


@pytest.mark.asyncio
async def test_timeout_cancel_and_transport_safe_errors():
    for error in (asyncio.CancelledError(), TimeoutError()):
        with pytest.raises(type(error)):
            await adapter(Client(error)).rerank("query", DOCS)
    class Hanging:
        async def post(self, *args, **kwargs):
            await asyncio.sleep(1)
    with pytest.raises(TimeoutError):
        await adapter(Hanging(), timeout_seconds=.01).rerank("query", DOCS)
    with pytest.raises(ProviderTransientError) as caught:
        await adapter(Client(OSError("test-secret")), max_attempts=1).rerank("query", DOCS)
    assert "test-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_base_enabled_success_disabled_failure_and_strict_failure():
    from dovideo.application.retrieval import VideoEvidenceRetrievalService
    from dovideo.infrastructure.r4_runtime import _StrictRetrievalService, R4ProviderFallbackError
    from dovideo.presentation.composition import InMemoryVectorIndex
    from tests.application.test_hybrid_candidates import Planner, Embedding, Metrics, chunk
    for service_type in (VideoEvidenceRetrievalService, _StrictRetrievalService):
        for reranker in (None, adapter(Client(ProviderHttpResponse(200, BODY))), adapter(Client(ProviderHttpResponse(401, {})))):
            metrics = Metrics()
            service = service_type(Planner(), Embedding(), InMemoryVectorIndex(), telemetry=metrics, reranker=reranker)
            if service_type is _StrictRetrievalService:
                service._embed = Embedding().embed
            chunks = (chunk(0, "QKV"), chunk(300_000, "QKV"))
            if service_type is _StrictRetrievalService and reranker is not None and reranker._client.responses[0].status_code == 401:
                with pytest.raises(R4ProviderFallbackError):
                    await service.search(None, "QKV", chunks)
                assert metrics.counts["rerankerFallbacks"] == 1
            else:
                hits = await service.search(None, "QKV", chunks)
                assert hits[0].start_ms == (300_000 if metrics.values["rerankedCandidates"] == 2 else 0)
                assert metrics.counts["rerankerFallbacks"] == (1 if reranker is not None and metrics.values["rerankedCandidates"] == 0 else 0)


@pytest.mark.asyncio
async def test_production_wiring_and_close_are_configured_without_network(monkeypatch):
    import dovideo.infrastructure.r4_runtime as runtime
    from dovideo.infrastructure.providers.config import ProviderConfig
    from dovideo.infrastructure.model_routing import ModelRoutingProductionSettings
    from dovideo.infrastructure.x1_config import X1ToolCallingSettings
    monkeypatch.setattr(runtime.ProviderConfig, "from_environment", classmethod(
        lambda cls, *args, **kwargs: ProviderConfig(base_url="https://chat.invalid/v1", model="chat")))
    monkeypatch.setattr(runtime, "embedding_provider_config_from_environment", lambda **kwargs:
        ProviderConfig(base_url="https://embedding.invalid/v1", model="BAAI/bge-m3", embedding_model="BAAI/bge-m3"))
    class ClosingClient(Client):
        closed = False
        async def aclose(self):
            self.closed = True
    client = ClosingClient()
    stack = runtime.create_r4_provider_stack(object(), object(), runtime.R4AgentTelemetry(object()), None,
        routing_settings=ModelRoutingProductionSettings(enabled=False), tool_settings=X1ToolCallingSettings(enabled=False),
        reranker_config=RerankerConfig(enabled=True, api_key="test-secret"), reranker_http_client=client)
    assert stack.long_context._retrieval._reranker is stack.reranker_adapter
    assert client.calls == []
    await stack.close()
    assert client.closed
