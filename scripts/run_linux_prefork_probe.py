"""Reproducible Linux child-SIGKILL proof using the established R3 harness.

Use an already migrated, disposable local database and the infrastructure env.
This is an explicit deterministic test composition; no provider calls occur.
"""
import argparse
import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--queue", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != "linux":
        print("NOT RUN: Linux /proc and Celery prefork are required")
        return 2
    if args.worker:
        from dovideo.infrastructure.celery_runtime import R3WorkerRuntime
        from dovideo.infrastructure.celery_tasks import create_worker_app
        from dovideo.infrastructure.celery_transport import CeleryTransportSettings
        settings = CeleryTransportSettings.from_environment()
        def factory():
            runtime = R3WorkerRuntime.from_environment(settings=settings)
            original = runtime.dead_letter.publish
            async def fail_first_handoff(request, error, attempt):
                marker = f"prefork-proof:dlq:{settings.queue}"
                if "__R3_PERMANENT__" in request.goal and runtime.infrastructure.redis_client.set(marker, "injected", nx=True, ex=600):
                    raise ConnectionError("Injected first DLQ publication outage")
                await original(request, error=error, attempt=attempt)
                if "__R3_PERMANENT__" in request.goal:
                    runtime.infrastructure.redis_client.set(marker, "recovered", ex=600)
            runtime.dead_letter.publish = fail_first_handoff
            return runtime
        app = create_worker_app(settings, runtime_factory=factory)
        app.worker_main(["worker", "--loglevel=WARNING", "--pool=prefork", "--concurrency=1", "-Q", args.queue])
        return 0
    if args.env_file is None:
        parser.error("--env-file is required")
    import r3_live_smoke as smoke
    from validate_deployment import read_env
    for name, value in read_env(args.env_file).items():
        if name in smoke._ALLOWED_ENV:
            os.environ[name] = value
    os.environ["DOVIDEO_PROFILE"] = "production"
    os.environ["DOVIDEO_TASK_LEASE_LIVE"] = "1"
    import pytest
    code = pytest.main([str(ROOT / "tests/infrastructure/test_task_lease_redis_live.py"), "-q"])
    if code:
        return int(code)
    smoke.load_local_infrastructure_environment = lambda root: None
    def start_worker(root, settings):
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", "--queue", settings.queue],
                                cwd=root, env=dict(os.environ), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    smoke._start_worker = start_worker
    def kill_child(process):
        # Stop only the process created by this harness before identifying its
        # single pool child, so its supervisor cannot replace the killed child.
        os.kill(process.pid, signal.SIGSTOP)
        children = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[1]) == process.pid:
                    children.append(int(entry.name))
            except (OSError, ValueError, IndexError):
                continue
        try:
            if len(children) != 1:
                raise AssertionError("Cannot uniquely identify harness prefork child")
            os.kill(children[0], signal.SIGKILL)
            print("PREFORK_CHILD_SIGKILL=YES")
        finally:
            process.kill()
            process.wait(timeout=10)
    smoke._crash_worker = kill_child
    original_started = smoke._processing_started
    async def wait_with_renewal(client, lifecycle, request):
        if not await original_started(client, lifecycle, request):
            return False
        from dovideo.infrastructure.redis import RedisTaskLock
        lock = RedisTaskLock(client, ttl_ms=3000)
        token = client.get(lock.redis_key(request.task_key))
        await asyncio.sleep(4.2)
        assert token and client.get(lock.redis_key(request.task_key)) == token
        assert await lock.acquire(request.task_key) is None
        state = await lifecycle.load_lifecycle(request.task_key)
        assert state.attempt == 1  # Lock contention itself never counts an attempt.
        return True
    smoke._processing_started = wait_with_renewal
    asyncio.run(smoke._run(ROOT))
    from redis import Redis
    client = Redis.from_url(os.environ["DOVIDEO_REDIS_URL"], decode_responses=True)
    marker = f"prefork-proof:dlq:{os.environ['DOVIDEO_CELERY_QUEUE']}"
    try:
        assert client.get(marker) == "recovered"
    finally:
        client.delete(marker)
        client.close()
    print("PREFORK_PROBE=PASS child_loss_redelivery=YES completed_recovery=YES pending_dlq_recovery=YES old_token_safe=YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
