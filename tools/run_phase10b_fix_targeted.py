"""Run the Phase 10B-FIX targeted regression with a local test namespace."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
TEMP = ROOT / "work" / "pytest-temp" / "phase10b-fix-targeted-v2"
TARGETS = [
    "tests/infrastructure/test_asr_ocr.py",
    "tests/infrastructure/test_media.py",
    "tests/infrastructure/test_local_adapters_10b.py",
    "tests/infrastructure/test_provider_adapters_10a.py",
    "tests/application/test_context.py",
    "tests/application/test_chunking.py",
    "tests/application/test_retrieval.py",
    "tests/application/test_long_context.py",
    "tests/application/test_agent_policy.py",
    "tests/application/test_agent_loop_7bc.py",
    "tests/application/test_agent_loop_7d.py",
    "tests/application/test_agent_loop_7e.py",
    "tests/application/test_agent_loop_7f.py",
    "tests/application/test_evidence.py",
    "tests/application/test_phase5_exports.py",
    "tests/domain/test_agent.py",
    "tests/domain/test_analysis.py",
    "tests/domain/test_budget.py",
    "tests/domain/test_video.py",
]


def _install_local_test_namespace() -> None:
    """Avoid the host's unrelated site-packages ``tests`` package."""

    package = types.ModuleType("tests")
    package.__path__ = [str(TESTS)]  # type: ignore[attr-defined]
    application = types.ModuleType("tests.application")
    application.__path__ = [str(TESTS / "application")]  # type: ignore[attr-defined]
    sys.modules["tests"] = package
    sys.modules["tests.application"] = application


if __name__ == "__main__":
    TEMP.mkdir(parents=True, exist_ok=True)
    _install_local_test_namespace()
    raise SystemExit(
        pytest.main([*TARGETS, "--basetemp", str(TEMP)])
    )
