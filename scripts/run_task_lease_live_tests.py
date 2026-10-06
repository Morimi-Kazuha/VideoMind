"""Explicit Redis/RabbitMQ/MySQL lease proof, without paid provider calls.

Credentials are read from an ignored env file into an allow-listed environment.
The smoke worker deliberately selects the R3 deterministic runtime; production
continues to select R4. Generated RabbitMQ topology isolates broker writes.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--queue", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        from dovideo.infrastructure.celery_runtime import R3WorkerRuntime
        from dovideo.infrastructure.celery_tasks import create_worker_app
        from dovideo.infrastructure.celery_transport import CeleryTransportSettings
        settings = CeleryTransportSettings.from_environment()
        app = create_worker_app(settings, runtime_factory=lambda: R3WorkerRuntime.from_environment(settings=settings))
        app.worker_main(["worker", "--loglevel=WARNING", "--pool=solo", "--concurrency=1", "-Q", args.queue])
        return 0
    if args.env_file is None:
        parser.error("--env-file is required")

    import r3_live_smoke as smoke
    for raw in args.env_file.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = raw.strip().partition("=")
        if separator and name in smoke._ALLOWED_ENV:
            os.environ[name] = value.strip().strip('"').strip("'")
    os.environ["DOVIDEO_PROFILE"] = "production"
    os.environ["DOVIDEO_TASK_LEASE_LIVE"] = "1"
    import pytest
    code = pytest.main([str(ROOT / "tests/infrastructure/test_task_lease_redis_live.py"), "-q", "--tb=short"])
    if code:
        return int(code)

    # Reuse the established durable lifecycle/result/DLQ/restart smoke proof,
    # selecting its test runtime explicitly because the canonical worker is R4.
    smoke.load_local_infrastructure_environment = lambda root: None
    def start_worker(root, settings):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", "--queue", settings.queue],
            cwd=root, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    smoke._start_worker = start_worker
    original = smoke._processing_started
    async def processing_with_renewal(client, lifecycle, request):
        if not await original(client, lifecycle, request):
            return False
        from dovideo.infrastructure.redis import RedisTaskLock
        lock = RedisTaskLock(client, ttl_ms=3000)
        owner = client.get(lock.redis_key(request.task_key))
        await asyncio.sleep(4.2)  # Exceed the original short lease before crash.
        assert owner is not None and client.get(lock.redis_key(request.task_key)) == owner
        assert client.pttl(lock.redis_key(request.task_key)) > 0
        assert await lock.acquire(request.task_key) is None
        print("R3_LEASE_RENEWAL=YES alive_beyond_original_ttl=YES duplicate_excluded=YES")
        return True
    smoke._processing_started = processing_with_renewal
    asyncio.run(smoke._run(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
