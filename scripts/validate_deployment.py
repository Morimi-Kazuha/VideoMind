"""Offline env-contract and real production composition smoke; never connects."""
from __future__ import annotations

import argparse
import ast
import asyncio
import os
from pathlib import Path
import re
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values:
            raise ValueError("Environment example contains invalid or duplicate assignments")
        values[name] = value.strip().strip('"').strip("'")
    return values


def validate_example(path, root=ROOT):
    values = read_env(path)
    compose = (root / "docker-compose.r2.yml").read_text(encoding="utf-8")
    known = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose))
    for source in (root / "src").rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                known.update(re.findall(r"\bDOVIDEO_[A-Z0-9_]+\b", node.value))
            elif isinstance(node, ast.JoinedStr):
                # ProviderConfig supports a DOVIDEO_ prefix; execution profiles
                # are constructed with FAST/BALANCED/DEEP prefixes at runtime.
                suffix = "".join(part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str))
                if re.fullmatch(r"[A-Z][A-Z0-9_]+", suffix):
                    known.add("DOVIDEO_" + suffix)
                if re.fullmatch(r"_[A-Z0-9_]+", suffix):
                    known.update("DOVIDEO_" + lane + suffix for lane in ("FAST", "BALANCED", "DEEP"))
    documented = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", path.read_text(encoding="utf-8"), re.M))
    unknown = documented - known
    if unknown:
        raise ValueError("Unknown environment example names: " + ", ".join(sorted(unknown)))
    for name, value in values.items():
        if any(token in name for token in ("PASSWORD", "_PASS", "_SECRET", "_API_KEY", "_ACCESS_KEY")) and value != "replace-me":
            raise ValueError("Example credentials must use replace-me placeholders")
        if name.endswith("_URL") and urlsplit(value).password not in {None, "replace-me"}:
            raise ValueError("Example URL credentials must use replace-me placeholders")
    return values


def validate_settings(values):
    from sqlalchemy.engine import make_url
    from dovideo.infrastructure.r2_config import R2Settings, _minio_host
    from dovideo.infrastructure.celery_transport import CeleryTransportSettings
    from dovideo.infrastructure.providers import ProviderConfig
    from dovideo.infrastructure.model_routing import ModelRoutingProductionSettings
    from dovideo.infrastructure.x1_config import X1ToolCallingSettings
    from dovideo.infrastructure.content_context import pipeline_contract
    from dovideo.presentation.composition import AnalysisSettings, embedding_provider_config_from_environment

    settings = R2Settings.from_environment(values)
    url = make_url(settings.database_url)
    if url.drivername != "mysql+pymysql" or not url.host or not url.database or not url.username or not url.password:
        raise ValueError("Production database URL requires configured MySQL/PyMySQL credentials and database")
    redis = urlsplit(settings.redis_url)
    if redis.scheme not in {"redis", "rediss"} or not redis.hostname or not redis.password:
        raise ValueError("Production Redis requires a redis(s) URL with credentials")
    broker = CeleryTransportSettings.from_environment(values)
    parsed = urlsplit(broker.broker_url)
    if not parsed.hostname or not parsed.username or not parsed.password:
        raise ValueError("Production broker requires credentials")
    _minio_host(settings.minio_endpoint, settings.minio_secure)
    qdrant = urlsplit(settings.qdrant_url)
    if qdrant.scheme not in {"http", "https"} or not qdrant.hostname:
        raise ValueError("Qdrant URL must be HTTP(S)")
    model = ProviderConfig.from_environment(values, required=True)
    if not model.api_key:
        raise ValueError("Production model credential is required")
    embedding = embedding_provider_config_from_environment(values, required=True)
    if (embedding.embedding_model or embedding.model) != "BAAI/bge-m3":
        raise ValueError("R4 embedding contract requires BAAI/bge-m3")
    X1ToolCallingSettings.from_environment(values)
    ModelRoutingProductionSettings.from_environment(values)
    pipeline_contract(AnalysisSettings.from_environment(environ=values, embedding_mode="remote"), values)


async def production_smoke(values):
    from dovideo.infrastructure.r2_config import R2Settings, create_r2_infrastructure
    from dovideo.infrastructure.r4_runtime import R4WorkerRuntime, R4RequestContextCheckpoint
    from dovideo.presentation.api.r4_runtime import ProductionR4Services

    def forbid_network(*args, **kwargs):
        raise AssertionError("Production configuration smoke attempted network access")

    validate_settings(values)
    with patch.dict(os.environ, values, clear=True), patch("socket.socket.connect", forbid_network), patch("socket.create_connection", forbid_network):
        infrastructure = create_r2_infrastructure(R2Settings.from_environment(values))
        api = worker = None
        try:
            api = ProductionR4Services(infrastructure)
            worker = R4WorkerRuntime.from_environment(infrastructure=infrastructure)
            assert isinstance(worker.worker._context, R4RequestContextCheckpoint)
            assert worker.worker._context.pipeline.context_preparation is not None
        finally:
            if worker is not None:
                await worker.provider.close()
            if api is not None:
                await api.providers.close()
            infrastructure.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.example")
    args = parser.parse_args()
    values = validate_example(args.env_file)
    asyncio.run(production_smoke(values))
    print("PASS: environment contract and offline R4 API/worker composition")


if __name__ == "__main__":
    main()
