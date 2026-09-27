from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from dovideo.presentation.api import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(work_dir=tmp_path / "media")) as test_client:
        yield test_client


def auth_headers(client: TestClient) -> dict[str, str]:
    registration = client.post(
        "/user/register",
        json={"username": "r1_user", "password": "password123", "nickname": "R1"},
    )
    assert registration.status_code == 200
    login = client.post(
        "/user/login",
        json={"username": "r1_user", "password": "password123"},
    )
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['data']['token']}"}


def wait_for_completion(client: TestClient, headers: dict[str, str], media_id: int, goal: str) -> dict:
    for _ in range(100):
        response = client.get(
            "/analysis/analysis-status",
            params={"id": media_id, "goal": goal, "mode": "GENERAL"},
            headers=headers,
        )
        assert response.status_code == 200
        data = response.json()["data"]
        if data["state"] in {"COMPLETED", "FAILED"}:
            return data
        time.sleep(0.01)
    pytest.fail("R1 local analysis did not reach a terminal status")


def test_health_envelope_and_auth_error_do_not_leak_details(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"code": 0, "message": "", "data": "ok"}

    denied = client.get("/media/list")
    assert denied.status_code == 401
    body = denied.json()
    assert body["data"] is None
    assert "traceback" not in body["message"].lower()
    assert "token" not in body["message"].lower()


def test_original_media_and_resumable_chunk_routes(client: TestClient) -> None:
    headers = auth_headers(client)
    single = client.post(
        "/media/upload",
        files={"file": ("single.mp4", b"local-video", "video/mp4")},
        headers=headers,
    )
    assert single.status_code == 200
    media_id = single.json()["data"]["id"]

    init = client.post(
        "/media/init-upload",
        params={"filename": "parts.mp4", "totalChunks": 2},
        headers=headers,
    )
    assert init.status_code == 200
    upload_id = init.json()["data"]["uploadId"]
    for index, value in enumerate((b"part-a", b"part-b")):
        response = client.post(
            "/media/upload-chunk",
            data={
                "uploadId": upload_id,
                "chunkIndex": str(index),
                "totalChunks": "2",
            },
            files={"file": ("chunk", value, "application/octet-stream")},
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["data"]["uploadedChunks"] == list(range(index + 1))

    completed = client.post(
        "/media/complete-upload",
        params={"uploadId": upload_id},
        headers=headers,
    )
    assert completed.status_code == 200
    assert completed.json()["data"]["filename"] == "parts.mp4"
    assert completed.json()["data"]["id"] != media_id

    status = client.get(
        "/media/upload-status",
        params={"uploadId": upload_id},
        headers=headers,
    )
    assert status.status_code == 200
    assert status.json()["data"]["completedMediaId"] == completed.json()["data"]["id"]


def test_analysis_dispatch_worker_sse_retrieval_eval_and_trace(client: TestClient) -> None:
    headers = auth_headers(client)
    uploaded = client.post(
        "/media/upload",
        files={"file": ("analysis.mp4", b"local-video", "video/mp4")},
        headers=headers,
    )
    media_id = uploaded.json()["data"]["id"]
    goal = "find the later temporal region"

    accepted = client.post(
        "/analysis/ai",
        params={"id": media_id, "goal": goal, "mode": "GENERAL"},
        headers=headers,
    )
    assert accepted.status_code == 202
    assert accepted.json()["code"] == 0
    status = wait_for_completion(client, headers, media_id, goal)
    assert status["state"] == "COMPLETED"
    assert "300000" in status["result"]

    plan = client.get(
        "/analysis/agent-plan",
        params={"id": media_id, "goal": goal, "mode": "GENERAL"},
        headers=headers,
    )
    assert plan.status_code == 200
    assert len(plan.json()["data"]["tasks"]) >= 1

    evidence = client.get(
        "/analysis/evidence-search",
        params={"id": media_id, "query": "later temporal"},
        headers=headers,
    )
    assert evidence.status_code == 200
    candidates = evidence.json()["data"]
    assert len(candidates) >= 2
    assert candidates[0]["startMs"] == 300000

    evaluation = client.get(
        "/analysis/agent-evaluation",
        params={"id": media_id, "goal": goal, "mode": "GENERAL"},
        headers=headers,
    )
    metrics = evaluation.json()["data"]
    assert metrics["structuredValid"] is True
    assert metrics["timestampCoverageRate"] == 1.0
    assert metrics["evidenceSupportRate"] == 1.0
    assert metrics["claimEvidenceSupportRate"] == 1.0
    assert metrics["criticPassed"] is True

    citations = client.get(
        "/analysis/agent-citations",
        params={"id": media_id, "goal": goal, "mode": "GENERAL"},
        headers=headers,
    )
    assert citations.status_code == 200
    records = citations.json()["data"]
    assert len(records) == 1
    assert records[0]["sourceItemIds"]
    assert records[0]["segmentId"]
    assert records[0]["timestampMs"] == 300000

    trace = client.get(
        "/analysis/agent-trace",
        params={"id": media_id, "goal": goal, "mode": "GENERAL"},
        headers=headers,
    )
    trace_data = trace.json()["data"]
    assert trace_data["traceId"]
    assert trace_data["counters"]["PLAN_COMPLETEDCalls"] >= 1

    with client.stream(
        "GET",
        "/analysis/analysis-events",
        params={"id": media_id, "goal": goal, "mode": "GENERAL"},
        headers=headers,
    ) as stream:
        assert stream.status_code == 200
        lines = list(stream.iter_lines())
    assert any(line.startswith("data: ") for line in lines)
    assert any("COMPLETED" in line for line in lines)

    feedback = client.post(
        "/analysis/agent-feedback",
        json={"mediaId": media_id, "goal": goal, "mode": "GENERAL", "rating": 1},
        headers=headers,
    )
    assert feedback.status_code == 200
    after_feedback = client.get(
        "/analysis/agent-evaluation",
        params={"id": media_id, "goal": goal, "mode": "GENERAL"},
        headers=headers,
    ).json()["data"]
    assert after_feedback["feedbackSamples"] == 1
    assert after_feedback["userAcceptanceRate"] == 1.0


def test_r1_does_not_attempt_remote_url_import(client: TestClient) -> None:
    headers = auth_headers(client)
    response = client.post(
        "/media/upload-url",
        params={"url": "https://example.invalid/video.mp4"},
        headers=headers,
    )
    assert response.status_code == 501
    assert response.json()["data"] is None
