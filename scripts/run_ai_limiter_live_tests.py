"""Run isolated real Redis AI limiter tests with an explicitly supplied env.

Only the Redis URL is loaded; credentials are never printed or copied.
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
    for raw in args.env_file.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = raw.strip().partition("=")
        if separator and name == "DOVIDEO_REDIS_URL":
            os.environ[name] = value.strip().strip('\"').strip("'")
    if not os.environ.get("DOVIDEO_REDIS_URL"):
        parser.error("DOVIDEO_REDIS_URL is required")
    os.environ["DOVIDEO_AI_LIMITER_LIVE"] = "1"
    import pytest
    return pytest.main([str(ROOT / "tests/infrastructure/test_ai_interaction_limiter_live.py"), "-q", "--tb=short"])


if __name__ == "__main__":
    raise SystemExit(main())
