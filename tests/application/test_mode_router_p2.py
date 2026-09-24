from __future__ import annotations

import pytest

from dovideo.application import (
    ModeRouter,
    TaskKey,
    decode_concrete_mode,
    goal_digest,
    mode_profile_for,
)
from dovideo.domain import AnalysisMode
from dovideo.infrastructure.providers import (
    ProviderRequestError,
    ProviderTransientError,
)


class _FakeModel:
    def __init__(self, response: object) -> None:
        self.response = response
        self.goals: list[str] = []

    async def classify(self, goal: str):
        self.goals.append(goal)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class _Observer:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, str | None]] = []

    def record_mode_router_event(
        self,
        event: str,
        *,
        mode: str | None = None,
        category: str | None = None,
    ) -> None:
        self.events.append((event, mode, category))


@pytest.mark.parametrize(
    ("goal", "mode", "response"),
    [
        ("概括视频中的主要信息", AnalysisMode.GENERAL, '{"mode":"GENERAL"}'),
        ("解释核心概念并整理复习材料", AnalysisMode.LEARNING, '{"mode":"LEARNING"}'),
        ("评估论证中的优点、风险和遗漏", AnalysisMode.REVIEW, '{"mode":"REVIEW"}'),
        ("把素材改编成短视频脚本和标题", AnalysisMode.CREATION, '{"mode":"CREATION"}'),
    ],
)
@pytest.mark.asyncio
async def test_successful_router_decodes_one_concrete_mode(goal, mode, response) -> None:
    model = _FakeModel(response)
    observer = _Observer()

    routed = await ModeRouter(model, observer=observer).route(goal)

    assert routed is mode
    assert model.goals == [goal]
    assert [event[0] for event in observer.events] == [
        "requested",
        "provider_call_attempted",
        "provider_call_succeeded",
        "decode_succeeded",
        "selected_concrete_mode",
    ]
    assert observer.events[-1][1] == mode.value


@pytest.mark.parametrize(
    ("response", "category"),
    [
        ('{"mode":"AUTO"}', "invalid_mode"),
        ('{"mode":"UNKNOWN"}', "invalid_mode"),
        ("not json", "invalid_json"),
        ("", "empty_response"),
        ('{"mode":"LEARNING","reason":"extra"}', "invalid_field_set"),
        ('{"mode":"LEARNING","mode":"REVIEW"}', "duplicate_field"),
        ('{"mode":5}', "invalid_mode"),
        ('"LEARNING"', "invalid_object"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_router_output_falls_back_to_general(response, category) -> None:
    observer = _Observer()
    routed = await ModeRouter(_FakeModel(response), observer=observer).route("goal")

    assert routed is AnalysisMode.GENERAL
    assert ("decode_failed", None, category) in observer.events
    assert ("general_fallback_triggered", None, category) in observer.events
    assert observer.events[-1] == (
        "selected_concrete_mode",
        AnalysisMode.GENERAL.value,
        None,
    )


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (TimeoutError("sensitive timeout detail"), "timeout"),
        (ProviderTransientError("sensitive transport detail"), "transport"),
        (ProviderRequestError("sensitive provider response"), "provider_error"),
        (RuntimeError("unexpected sensitive value"), "unexpected"),
    ],
)
@pytest.mark.asyncio
async def test_provider_and_unexpected_failures_fall_back_without_decode(error, category) -> None:
    observer = _Observer()
    routed = await ModeRouter(_FakeModel(error), observer=observer).route("goal")

    assert routed is AnalysisMode.GENERAL
    assert ("provider_call_attempted", None, None) in observer.events
    assert ("provider_call_failed", None, category) in observer.events
    assert ("general_fallback_triggered", None, category) in observer.events
    assert all(event[0] != "decode_failed" for event in observer.events)
    assert "sensitive" not in repr(observer.events)


def test_strict_mode_decoder_accepts_only_concrete_enum_members() -> None:
    assert decode_concrete_mode('{"mode":"REVIEW"}') is AnalysisMode.REVIEW
    assert "AUTO" not in AnalysisMode.__members__
    with pytest.raises(ValueError):
        AnalysisMode.from_request("AUTO")


def test_concrete_routes_preserve_task_identity_and_p1_profiles() -> None:
    modes = tuple(AnalysisMode)
    keys = {TaskKey(7, "same goal", mode) for mode in modes}
    assert len(keys) == len(modes)
    assert goal_digest("same goal", None) == goal_digest(
        "same goal", AnalysisMode.GENERAL
    )
    assert len({goal_digest("same goal", mode) for mode in modes}) == len(modes)

    for mode in (
        AnalysisMode.LEARNING,
        AnalysisMode.REVIEW,
        AnalysisMode.CREATION,
    ):
        profile = mode_profile_for(mode)
        assert profile.mode is mode
        assert profile.required_section_keys
