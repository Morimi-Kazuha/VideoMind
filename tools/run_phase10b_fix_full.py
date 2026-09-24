"""Run the single full Phase 10B-FIX pytest collection locally."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
TEMP = ROOT / "work" / "pytest-temp" / "phase10b-fix-full"


def _install_local_test_namespace() -> None:
    package = types.ModuleType("tests")
    package.__path__ = [str(TESTS)]  # type: ignore[attr-defined]
    application = types.ModuleType("tests.application")
    application.__path__ = [str(TESTS / "application")]  # type: ignore[attr-defined]
    sys.modules["tests"] = package
    sys.modules["tests.application"] = application


if __name__ == "__main__":
    TEMP.mkdir(parents=True, exist_ok=True)
    _install_local_test_namespace()
    raise SystemExit(pytest.main(["tests", "--basetemp", str(TEMP)]))
