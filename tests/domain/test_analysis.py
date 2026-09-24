from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

from pydantic import ValidationError

from dovideo.domain import AnalysisEvidence, AnalysisResult, AnalysisSection


def test_analysis_defaults_and_markdown_match_java_shape() -> None:
    result = AnalysisResult(
        title=None,
        conclusions=["结论"],
        evidence=[
            AnalysisEvidence(
                timestamp_ms=125_000,
                source=" ASR ",
                content=" 原文 ",
                claim=" 结论 ",
            )
        ],
        suggestions=["建议"],
    )
    assert result.title == "未命名分析"
    assert result.evidence[0].source == "ASR"
    assert result.evidence[0].content == "原文"
    assert result.to_markdown() == (
        "## 未命名分析\n\n"
        "## 核心结论\n"
        "- 结论\n\n"
        "## 视频证据\n"
        "- [02:05] ASR：原文\n\n"
        "## 建议\n"
        "- 建议\n"
    )
    assert result.toMarkdown() == result.to_markdown()


def test_mode_sections_are_rendered_after_common_sections() -> None:
    result = AnalysisResult(
        title="学习",
        sections=[AnalysisSection(key="outline", title="大纲", items=["一", "二"])],
    )
    assert result.sections[0].key == "outline"
    assert result.to_markdown().endswith("\n## 大纲\n- 一\n- 二\n")


def test_analysis_json_alias_round_trip_and_nested_record_aliases() -> None:
    result = AnalysisResult(
        title="T",
        conclusions=["C"],
        evidence=[AnalysisResult.Evidence(timestampMs=1, source="OCR", content="x", claim="C")],
        sections=[AnalysisResult.Section(key="k", title="K", items=["i"])],
    )
    payload = json.loads(result.model_dump_json(by_alias=True))
    assert payload["evidence"][0]["timestampMs"] == 1
    assert payload["sections"][0]["key"] == "k"
    assert AnalysisResult.model_validate(payload) == result


def test_negative_evidence_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AnalysisEvidence(timestampMs=-1)


def test_analysis_collection_is_defensively_copied_and_frozen() -> None:
    conclusions = ["C"]
    result = AnalysisResult(conclusions=conclusions)
    conclusions.append("later")
    assert result.conclusions == ("C",)
    with pytest.raises(ValidationError):
        result.title = "changed"

