from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from dovideo.application import (
    FailedTaskAdminNotFound,
    FailedTaskAdminPage,
    FailedTaskAdminView,
    FailedTaskReplayResult,
)
from dovideo.presentation.api import create_app
from dovideo.presentation.api.runtime import R1ServiceError


class _Auth:
    def require(self, authorization):
        if authorization == "Bearer admin-token":
            return {"id": 1, "role": "ADMIN"}
        if authorization == "Bearer operator-token":
            return {"id": 2, "role": "OPERATOR"}
        if authorization == "Bearer user-token":
            return {"id": 3, "role": "USER"}
        raise R1ServiceError("请先登录", status_code=401)


class _AdminServices:
    def __init__(self) -> None:
        self.auth = _Auth()
        now = datetime(2026, 9, 21, tzinfo=timezone.utc)
        self.record = SimpleNamespace(
            task_id=88,
            media_id=41,
            action="START_ANALYSIS",
            mode="REVIEW",
            attempt_count=3,
            status="DEAD_LETTER_PENDING",
            error_type="ProviderResponseError",
            error_message="Bearer sk-sensitive-value C:\\private\\transcript.txt OCR payload",
            created_at=now,
            updated_at=now,
            replay_attempt_count=0,
            user_goal="review this same video",
        )
        self.view = FailedTaskAdminView(
            record=self.record,
            replay_attempts=(),
            replay_status="NEVER_REPLAYED",
            lifecycle=None,
            replay_eligible=True,
        )
        self.list_calls = []
        self.inspect_calls = []
        self.replay_calls = []

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def list_failed_tasks(self, *, limit=50, offset=0):
        self.list_calls.append((limit, offset))
        return FailedTaskAdminPage((self.view,), 1, limit, offset)

    async def inspect_failed_task(self, task_id):
        self.inspect_calls.append(task_id)
        if task_id == 999:
            raise FailedTaskAdminNotFound("失败任务不存在")
        return self.view

    async def replay_failed_task(self, task_id, idempotency_key):
        self.replay_calls.append((task_id, idempotency_key))
        attempt = SimpleNamespace(
            attempt_id="d3b6800f-d0bb-49df-941d-428e023384dc",
            attempt_number=1,
            status="DISPATCHED",
            error_type=None,
            created_at=self.record.created_at,
            updated_at=self.record.updated_at,
        )
        view = FailedTaskAdminView(
            record=self.record,
            replay_attempts=(attempt,),
            replay_status="RUNNING",
            lifecycle=None,
            replay_eligible=False,
        )
        return FailedTaskReplayResult(view, accepted=True)


def test_admin_failed_task_routes_require_existing_operator_role_and_sanitize_fields():
    services = _AdminServices()
    with TestClient(create_app(services=services)) as client:
        assert client.get("/admin/failed-tasks").status_code == 401
        assert client.get(
            "/admin/failed-tasks",
            headers={"Authorization": "Bearer user-token"},
        ).status_code == 403

        headers = {"Authorization": "Bearer operator-token"}
        forbidden_replay = client.post(
            "/admin/failed-tasks/88/replay",
            headers={**headers, "Authorization": "Bearer user-token", "Idempotency-Key": "ordinary-user-key-0001"},
        )
        assert forbidden_replay.status_code == 403
        assert services.replay_calls == []

        listing = client.get("/admin/failed-tasks?limit=10&offset=0", headers=headers)
        assert listing.status_code == 200
        assert services.list_calls == [(10, 0)]
        listing_body = listing.json()["data"]
        assert listing_body["total"] == 1
        assert listing_body["items"][0]["failedTaskId"] == 88
        assert "goal" not in listing_body["items"][0]
        assert "sk-sensitive-value" not in listing.text
        assert "private" not in listing.text
        assert "transcript" not in listing.text

        detail = client.get("/admin/failed-tasks/88", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["data"]["goal"] == "review this same video"
        assert detail.json()["data"]["replayEligible"] is True
        assert "sk-sensitive-value" not in detail.text
        assert services.inspect_calls == [88]

        missing = client.get("/admin/failed-tasks/999", headers=headers)
        assert missing.status_code == 404

        missing_key = client.post("/admin/failed-tasks/88/replay", headers=headers)
        assert missing_key.status_code == 400
        replay = client.post(
            "/admin/failed-tasks/88/replay",
            headers={**headers, "Idempotency-Key": "operator-request-0001"},
        )
        assert replay.status_code == 202
        assert replay.json()["data"]["status"] == "RUNNING"
        assert replay.json()["data"]["failedTask"]["replayAttempts"][0]["status"] == "DISPATCHED"
        assert services.replay_calls == [(88, "operator-request-0001")]


def test_admin_api_bounds_failed_task_page():
    with TestClient(create_app(services=_AdminServices())) as client:
        response = client.get(
            "/admin/failed-tasks?limit=1000",
            headers={"Authorization": "Bearer admin-token"},
        )
    assert response.status_code == 400
