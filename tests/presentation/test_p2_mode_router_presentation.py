from __future__ import annotations

import json

import pytest

from dovideo.application import DispatchDisposition, TaskKey
from dovideo.domain import AnalysisMode
from dovideo.presentation.api.app import create_app
from dovideo.presentation.api.r4_runtime import ProductionR4Services
from dovideo.presentation.api.runtime import R1ServiceError
from dovideo.presentation.api.schemas import RouteRequest


class _FakeModeRouter:
    def __init__(self, mode: AnalysisMode) -> None:
        self.mode = mode
        self.goals: list[str] = []

    async def route(self, goal: str) -> AnalysisMode:
        self.goals.append(goal)
        return self.mode


class _FakeMedia:
    def __init__(self) -> None:
        self.owned_calls: list[tuple[int, int]] = []

    async def require_owned(self, media_id: int, user_id: int):
        self.owned_calls.append((media_id, user_id))
        return object()


class _FakeCheckpoint:
    def __init__(self) -> None:
        self.keys: list[TaskKey] = []

    async def load_result(self, key: TaskKey):
        self.keys.append(key)
        return None


def _endpoint(app, path: str):
    route = next(item for item in app.routes if getattr(item, "path", None) == path)
    return route.endpoint


@pytest.mark.asyncio
async def test_production_route_uses_r4_model_router_then_submits_its_concrete_mode(
    monkeypatch,
) -> None:
    services = ProductionR4Services.__new__(ProductionR4Services)
    model_router = _FakeModeRouter(AnalysisMode.CREATION)
    services.mode_router = model_router
    services.media = _FakeMedia()
    services.checkpoint = _FakeCheckpoint()
    submissions: list[tuple[int, int, str, AnalysisMode]] = []

    async def submit_analysis(
        media_id: int,
        user_id: int,
        goal: str,
        mode: AnalysisMode,
    ) -> DispatchDisposition:
        submissions.append((media_id, user_id, goal, mode))
        return DispatchDisposition.ACCEPTED

    services.submit_analysis = submit_analysis
    monkeypatch.setenv("DOVIDEO_PROFILE", "production")
    monkeypatch.setattr(
        "dovideo.presentation.api.r4_runtime.create_production_services",
        lambda: services,
    )

    app = create_app()
    assert app.state.services is services
    assert isinstance(app.state.services, ProductionR4Services)
    route_endpoint = _endpoint(app, "/analysis/route")
    analysis_endpoint = _endpoint(app, "/analysis/ai")
    goal = "请学习这段视频，然后改编为适合短视频发布的脚本"

    route_response = await route_endpoint(
        payload=RouteRequest(goal=goal),
        _user={"id": 19},
    )
    route_data = json.loads(route_response.body)["data"]

    assert model_router.goals == [goal]
    assert route_data["mode"] == AnalysisMode.CREATION.value
    assert route_data["reason"] == "面向后续内容创作"

    analysis_response = await analysis_endpoint(
        id=7,
        goal=goal,
        mode=route_data["mode"],
        user={"id": 19},
    )

    expected_key = TaskKey(7, goal, AnalysisMode.CREATION)
    assert analysis_response.status_code == 202
    assert services.checkpoint.keys == [expected_key]
    assert submissions == [(7, 19, goal, AnalysisMode.CREATION)]
    assert services.media.owned_calls == [(7, 19)]

    with pytest.raises(R1ServiceError) as error:
        await analysis_endpoint(
            id=7,
            goal=goal,
            mode="AUTO",
            user={"id": 19},
        )
    assert error.value.status_code == 400
    assert len(submissions) == 1
