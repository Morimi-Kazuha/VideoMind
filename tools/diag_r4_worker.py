"""Bounded worker-start diagnostic with sanitized output only."""

from __future__ import annotations

import os
import re
import runpy
import subprocess
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(REPO / "src"),
    str(REPO / "work" / "r1-venv" / "Lib" / "site-packages"),
    str(REPO / "tools" / "asr" / "python-packages"),
]
runpy.run_path(str(REPO / "tools" / "run_r4_live.py"))["load_local_environment"]()

from dovideo.infrastructure.celery_transport import (  # noqa: E402
    CeleryTransportSettings,
)


def _safe(line: str) -> str:
    value = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", line)
    value = re.sub(
        r"(?i)(api[_-]?key|password|secret|token|authorization)(\s*[=:]\s*)[^\s,;]+",
        r"\1=[redacted]",
        value,
    )
    value = re.sub(r"(?i)(amqps?|https?)://[^/@\s]+:[^/@\s]+@", r"\1://[redacted]@", value)
    return "".join(ch if ord(ch) >= 32 else " " for ch in value)[:800]


def main() -> int:
    settings = CeleryTransportSettings.from_environment(require_production=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(REPO / "src"),
            str(REPO / "work" / "r1-venv" / "Lib" / "site-packages"),
            str(REPO / "tools" / "asr" / "python-packages"),
        ]
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "dovideo.infrastructure.celery_worker:celery_app",
            "worker",
            "--loglevel=ERROR",
            "--pool=solo",
            "--concurrency=1",
            "-Q",
            settings.queue,
        ],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(20.0)
    if process.poll() is None:
        print("WORKER_START=ALIVE")
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=15.0)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=10.0)
    else:
        stdout, stderr = process.communicate(timeout=10.0)
        print("WORKER_START=EXITED")
        print("WORKER_EXIT_CODE=" + str(process.returncode))
    lines = [line for line in (stdout + "\n" + stderr).splitlines() if line.strip()]
    interesting = [
        _safe(line)
        for line in lines
        if re.search(r"(?i)error|critical|fatal|traceback|exception|memory|failed|crash", line)
    ]
    for line in interesting[-20:]:
        print("WORKER_LOG=" + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
