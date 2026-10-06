"""Load an explicitly supplied ignored infrastructure env and run upload tests.

Credentials stay in process environment, never printed or copied into source.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True, type=Path)
    args = parser.parse_args()
    allowed = {
        "DOVIDEO_DATABASE_URL", "DOVIDEO_REDIS_URL", "DOVIDEO_MINIO_ENDPOINT",
        "DOVIDEO_MINIO_ACCESS_KEY", "DOVIDEO_MINIO_SECRET_KEY", "DOVIDEO_MINIO_BUCKET",
        "DOVIDEO_MINIO_SECURE", "DOVIDEO_QDRANT_URL", "DOVIDEO_QDRANT_API_KEY",
        "DOVIDEO_QDRANT_COLLECTION",
    }
    for raw in args.env_file.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = raw.strip().partition("=")
        if separator and name in allowed:
            os.environ[name] = value.strip().strip('"').strip("'")
    os.environ["DOVIDEO_PROFILE"] = "production"
    os.environ["DOVIDEO_UPLOAD_LIVE"] = "1"
    import pytest
    return pytest.main([str(ROOT / "tests/infrastructure/test_upload_redis_minio_live.py"), "-q", "--tb=short"])


if __name__ == "__main__":
    raise SystemExit(main())
