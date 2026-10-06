import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("validate_deployment", ROOT / "scripts/validate_deployment.py")
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


@pytest.mark.asyncio
async def test_example_and_offline_production_api_worker_composition():
    values = validation.validate_example(ROOT / ".env.example")
    await validation.production_smoke(values)


@pytest.mark.parametrize("name", [
    "DOVIDEO_DATABASE_URL", "DOVIDEO_REDIS_URL", "DOVIDEO_MINIO_ENDPOINT",
    "DOVIDEO_MINIO_ACCESS_KEY", "DOVIDEO_MINIO_SECRET_KEY", "DOVIDEO_MINIO_BUCKET",
    "DOVIDEO_QDRANT_URL", "DOVIDEO_QDRANT_COLLECTION", "DOVIDEO_BROKER_URL",
    "DOVIDEO_MODEL_BASE_URL", "DOVIDEO_MODEL_API_KEY",
    "DOVIDEO_EMBEDDING_API_KEY",
])
def test_missing_production_configuration_fails_closed(name):
    values = validation.read_env(ROOT / ".env.example")
    values.pop(name)
    with pytest.raises((ValueError, RuntimeError)):
        validation.validate_settings(values)


@pytest.mark.parametrize("name,value", [
    ("DOVIDEO_DATABASE_URL", "mysql+pymysql://host/"),
    ("DOVIDEO_REDIS_URL", "http://host/0"),
    ("DOVIDEO_BROKER_URL", "amqp://host/"),
    ("DOVIDEO_QDRANT_URL", "ftp://host"),
    ("DOVIDEO_CONTEXT_PIPELINE_VERSION", " "),
])
def test_invalid_production_shapes_rejected(name, value):
    values = validation.read_env(ROOT / ".env.example")
    values[name] = value
    with pytest.raises((ValueError, RuntimeError)):
        validation.validate_settings(values)


@pytest.mark.parametrize("extra", ["DOVIDEO_REMOVED_SETTING=old", "DOVIDEO_MODEL_API_KEY=accidental-secret"])
def test_example_rejects_unknown_names_and_duplicate_credentials(tmp_path, extra):
    path = tmp_path / ".env.example"
    path.write_text((ROOT / ".env.example").read_text(encoding="utf-8") + "\n" + extra + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        validation.validate_example(path)


def test_example_rejects_non_placeholder_secret(tmp_path):
    path = tmp_path / ".env.example"
    path.write_text((ROOT / ".env.example").read_text(encoding="utf-8").replace("DOVIDEO_MODEL_API_KEY=replace-me", "DOVIDEO_MODEL_API_KEY=accidental-secret"), encoding="utf-8")
    with pytest.raises(ValueError, match="placeholders"):
        validation.validate_example(path)
