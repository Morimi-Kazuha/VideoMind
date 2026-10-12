"""Freeze, denominator, label isolation and artifact preservation checks."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("r1_sidecar_runner", ROOT / "tools/run_retrieval_need_shadow.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def fixture(tmp_path):
    path = tmp_path / "frozen.json"
    cases = []
    for index, (question, expected, cohort, ambiguity) in enumerate([
        ("脱离视频，解释 Redis。", "DIRECT_LLM", "synthetic_contract", False),
        ("作者讲了什么？", "VIDEO_RAG", "synthetic_contract", False),
        ("Redis 是什么？", "VIDEO_RAG", "ambiguous_safety", True),
        ("作者解释正确吗？", "VIDEO_RAG", "real_video_draft", True),
    ]):
        cases.append({"id": str(index), "expected": expected, "cohort": cohort, "ambiguity": ambiguity,
                      "input": {"original_question": question, "standalone_query": question}})
    path.write_text(json.dumps({"version": "retrieval-need-r1-v1", "frozen": True, "cases": cases}), encoding="utf-8")
    path.with_suffix(".sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest(), encoding="utf-8")
    return path


def test_unreviewed_ambiguous_exclusion_input_projection_and_no_overwrite(tmp_path, monkeypatch):
    path = fixture(tmp_path)
    inputs = []
    base = runner.RetrievalNeedPolicy
    class Spy(base):
        def decide(self, value):
            inputs.append(value.model_dump())
            return super().decide(value)
    monkeypatch.setattr(runner, "RetrievalNeedPolicy", Spy)
    output = tmp_path / "run"
    summary = runner.evaluate(path, output, repeats=2)
    assert summary["total_cases"] == 4 and summary["scored_contract"]["sample_count"] == 2
    assert summary["semantic_accuracy"] == "NOT_MEASURED" and summary["additional_classifier_provider_calls"] == 0
    assert all(set(value) == {"original_question", "standalone_query", "was_rewritten", "has_discussion_context"} for value in inputs)
    rows = [json.loads(line) for line in (output / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["expected"] == "VIDEO_RAG" and rows[-1]["decision"]["need"] == "VIDEO_PLUS_KNOWLEDGE"
    before = (output / "summary.json").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        runner.evaluate(path, output)
    assert (output / "summary.json").read_bytes() == before


def test_tampered_frozen_dataset_fails_before_execution(tmp_path):
    path = fixture(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        runner.evaluate(path, tmp_path / "run")
    assert not (tmp_path / "run").exists()
