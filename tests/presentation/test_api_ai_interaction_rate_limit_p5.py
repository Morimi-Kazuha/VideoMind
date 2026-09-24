from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from dovideo.application import (
    AiInteractionLimitDecision,
    DispatchDisposition,
    TaskKey,
)
from dovideo.domain import AnalysisMode, TaskStatus
from dovideo.presentation.api import create_app
from dovideo.presentation.api.runtime import R1ServiceError


class _Auth:
    def require(self, authorization):
        if authorization == "Bearer user-1":
            return {"id": 1}
        if authorization == "Bearer user-2":
            return {"id": 2}
        raise R1ServiceError("请先登录", status_code=401)


class _Limiter:
    def __init__(self, decision: AiInteractionLimitDecision) -> None:
        self.decision = decision
        self.calls = []

    async def try_acquire(self, user_id, endpoint):
        self.calls.append((user_id, endpoint))
        return self.decision


class _Media:
    def __init__(self) -> None:
        self.owned_calls = []

    async def require_owned(self, media_id, user_id):
        self.owned_calls.append((media_id, user_id))
        return SimpleNamespace()


class _Checkpoint:
    async def load_result(self, _key):
        return None


class _Services:
    def __init__(self, decision: AiInteractionLimitDecision) -> None:
        self.auth = _Auth()
        self.ai_interaction_limiter = _Limiter(decision)
        self.media = _Media()
        self.checkpoint = _Checkpoint()
        self.route_calls = []
        self.submit_calls = []
        self.follow_up_calls = []
        self.evidence_calls = []

    async def startup(self):
        return None

    async def shutdown(self):
        return None

    async def route(self, goal):
        self.route_calls.append(goal)
        return AnalysisMode.GENERAL, "local route"

    def key(self, media_id, goal, mode):
        return TaskKey(media_id, goal, mode)

    async def submit_analysis(self, media_id, user_id, goal, mode):
        self.submit_calls.append((media_id, user_id, goal, mode))
        return DispatchDisposition.ACCEPTED

    async def follow_up(self, media_id, question, goal, mode):
        self.follow_up_calls.append((media_id, question, goal, mode))
        return "answer"

    async def evidence_search(self, media_id, query):
        self.evidence_calls.append((media_id, query))
        return ()

    async def status(self, media_id, goal, mode):
        return TaskStatus.of("COMPLETED", "done")


def _client(decision: AiInteractionLimitDecision):
    services = _Services(decision)
    return TestClient(create_app(services=services)), services


def test_all_four_ai_surfaces_return_429_before_expensive_work() -> None:
    client, services = _client(AiInteractionLimitDecision(False, "USER_LIMIT", 17))
    headers = {"Authorization": "Bearer user-1"}
    try:
        responses = [
            client.post("/analysis/route", json={"goal": "route this"}, headers=headers),
            client.post("/analysis/ai?id=7&goal=analyze&mode=GENERAL", headers=headers),
            client.post(
                "/analysis/follow-up?id=7&question=why&mode=GENERAL",
                headers=headers,
            ),
            client.get("/analysis/evidence-search?id=7&query=topic", headers=headers),
        ]
        assert [response.status_code for response in responses] == [429, 429, 429, 429]
        assert all(response.headers["Retry-After"] == "17" for response in responses)
        assert services.route_calls == []
        assert services.submit_calls == []
        assert services.follow_up_calls == []
        assert services.evidence_calls == []
        assert services.media.owned_calls == []
        assert [endpoint for _, endpoint in services.ai_interaction_limiter.calls] == [
            "route",
            "analysis",
            "follow-up",
            "evidence-search",
        ]
    finally:
        client.close()


def test_authenticated_requests_are_allowed_and_each_endpoint_uses_shared_limiter() -> None:
    client, services = _client(AiInteractionLimitDecision(True, "ALLOWED"))
    headers = {"Authorization": "Bearer user-2"}
    try:
        assert client.post("/analysis/route", json={"goal": "route this"}, headers=headers).status_code == 200
        assert client.post("/analysis/ai?id=7&goal=analyze&mode=GENERAL", headers=headers).status_code == 202
        assert client.post(
            "/analysis/follow-up?id=7&question=why&mode=GENERAL",
            headers=headers,
        ).status_code == 200
        assert client.get("/analysis/evidence-search?id=7&query=topic", headers=headers).status_code == 200
        assert len(services.route_calls) == 1
        assert len(services.submit_calls) == 1
        assert len(services.follow_up_calls) == 1
        assert len(services.evidence_calls) == 1
    finally:
        client.close()


def test_authentication_precedes_limiter_and_backend_failure_fails_closed() -> None:
    client, services = _client(AiInteractionLimitDecision(True, "ALLOWED"))
    try:
        unauthenticated = client.post(
            "/analysis/route",
            json={"goal": "route this"},
        )
        assert unauthenticated.status_code == 401
        assert services.ai_interaction_limiter.calls == []
    finally:
        client.close()

    client, services = _client(AiInteractionLimitDecision(True, "ALLOWED"))

    async def backend_failure(_user_id, _endpoint):
        raise ConnectionError("private redis detail")

    services.ai_interaction_limiter.try_acquire = backend_failure
    try:
        response = client.post(
            "/analysis/route",
            json={"goal": "route this"},
            headers={"Authorization": "Bearer user-1"},
        )
        assert response.status_code == 503
        assert "private redis" not in response.text
        assert services.route_calls == []
    finally:
        client.close()


def test_non_ai_status_surface_is_not_rate_limited() -> None:
    client, services = _client(AiInteractionLimitDecision(False, "GLOBAL_LIMIT", 12))
    try:
        response = client.get(
            "/analysis/analysis-status?id=7&goal=analyze&mode=GENERAL",
            headers={"Authorization": "Bearer user-1"},
        )
        assert response.status_code == 200
        assert services.ai_interaction_limiter.calls == []
    finally:
        client.close()


def test_obviously_invalid_ai_input_is_rejected_before_consuming_interaction() -> None:
    client, services = _client(AiInteractionLimitDecision(False, "USER_LIMIT", 12))
    try:
        response = client.post(
            "/analysis/ai?id=7&goal=&mode=GENERAL",
            headers={"Authorization": "Bearer user-1"},
        )
        assert response.status_code == 400
        assert services.ai_interaction_limiter.calls == []
    finally:
        client.close()
