import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from dovideo.infrastructure.celery_tasks import register_analysis_task
from dovideo.infrastructure.celery_transport import CeleryTransportSettings, create_celery_app
from dovideo.infrastructure.r4_providers import R4ProviderStack


def example_environment(monkeypatch, tmp_path):
    for line in Path('.env.example').read_text(encoding='utf-8').splitlines():
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            monkeypatch.setenv(key, value)
    monkeypatch.setenv('DOVIDEO_MEDIA_WORKSPACE', str(tmp_path))
    monkeypatch.setattr('socket.create_connection', lambda *args: pytest.fail('factory attempted network'))


@pytest.mark.asyncio
async def test_full_production_factory_and_shutdown_without_network(monkeypatch, tmp_path):
    example_environment(monkeypatch, tmp_path)
    from dovideo.presentation.api.r4_runtime import create_production_services
    service = create_production_services()
    assert service.dispatcher._transport is service.transport
    assert service.providers.long_context is not None
    assert service.playback_grants.client is service.infrastructure.redis_client
    await service.shutdown()


def test_api_factory_closes_infrastructure_on_provider_configuration_failure(monkeypatch, tmp_path):
    example_environment(monkeypatch, tmp_path)
    import dovideo.presentation.api.r4_runtime as runtime
    from dovideo.infrastructure.r2_config import create_r2_infrastructure
    infra = create_r2_infrastructure()
    close = Mock(wraps=infra.close)
    monkeypatch.setattr(runtime, 'create_r2_infrastructure', lambda: infra)
    monkeypatch.setattr(type(infra), 'close', lambda self: close())
    monkeypatch.setattr(runtime, 'create_r4_provider_stack', Mock(side_effect=ValueError('bad config')))
    with pytest.raises(ValueError): runtime.create_production_services()
    close.assert_called_once()


@pytest.mark.asyncio
async def test_provider_cleanup_continues_after_first_failure():
    first = SimpleNamespace(aclose=AsyncMock(side_effect=OSError('close failure')))
    second = SimpleNamespace(aclose=AsyncMock())
    stack = SimpleNamespace(chat_client=first,additional_chat_clients=(first,second),reranker_adapter=None)
    with pytest.raises(OSError): await R4ProviderStack.close(stack)
    first.aclose.assert_awaited_once(); second.aclose.assert_awaited_once()


def test_worker_shutdown_closes_cached_runtime_once():
    runtime = SimpleNamespace(record_poison=AsyncMock(), close=AsyncMock())
    app = create_celery_app(CeleryTransportSettings('amqp://user:pass@localhost:5672/%2F'))
    task = register_analysis_task(app, runtime_factory=lambda: runtime)
    task.run({})
    app._dovideo_close_runtime(); app._dovideo_close_runtime()
    runtime.close.assert_awaited_once()
    app.close()
