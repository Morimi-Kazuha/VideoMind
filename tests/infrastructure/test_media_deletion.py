from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from dovideo.presentation.api.r2_runtime import ProductionMediaStore, R1ServiceError
from dovideo.application.media import MediaRecord


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["object", "vector", "checkpoint", "cache", "memory", "record"])
async def test_delete_retries_every_failure_without_losing_owner(monkeypatch, stage):
    calls = []
    fail = True
    exists = True
    record = MediaRecord(media_id=1, user_id=2, filename="a.mp4", source="minio://media/a")
    def action(name):
        calls.append(name)
        if fail and name == stage: raise OSError("injected")
    def async_action(name):
        async def call(*args): action(name)
        return call
    async def get(media_id): return record if exists else None
    async def delete(media_id):
        nonlocal exists
        action("record")
        exists = False
    repository = SimpleNamespace(get=get, delete=delete)
    infra = SimpleNamespace(media_repository=repository,
        object_storage=SimpleNamespace(delete_object=async_action("object")),
        vector_index=SimpleNamespace(delete_media=async_action("vector")),
        checkpoint_repository=object(),
        checkpoint_cache=SimpleNamespace(delete_media=lambda media_id: action("cache")))
    monkeypatch.setattr("dovideo.presentation.api.r2_runtime.AgentCheckpointService",
        lambda repo: SimpleNamespace(delete_media=async_action("checkpoint")))
    store = ProductionMediaStore(infra, memory=SimpleNamespace(delete_media=async_action("memory")))
    with pytest.raises(R1ServiceError): await store.delete_owned(1, 2)
    assert exists
    fail = False
    calls.clear()
    await store.delete_owned(1, 2)
    assert not exists
    assert calls == ["object", "vector", "checkpoint", "cache", "memory", "record"]
