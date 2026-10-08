from __future__ import annotations

from fastapi.testclient import TestClient

from dovideo.presentation.api import create_app
from dovideo.domain.agent import AgentState
from dovideo.domain.analysis import AnalysisEvidence, AnalysisResult
from dovideo.domain.modes import AnalysisMode


def _register(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/user/register",
        json={"username": username, "password": "password123"},
    )
    assert response.status_code == 200
    login = client.post(
        "/user/login",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {login.json()['data']['token']}"}


def test_temporal_windows_are_paged_and_media_owned(tmp_path) -> None:
    with TestClient(create_app(work_dir=tmp_path / "media")) as client:
        owner = _register(client, "temporal_owner")
        stranger = _register(client, "temporal_other")
        uploaded = client.post(
            "/media/upload",
            files={"file": ("temporal.mp4", b"local-video", "video/mp4")},
            headers=owner,
        )
        media_id = uploaded.json()["data"]["id"]

        denied = client.get(
            "/analysis/temporal-windows", params={"id": media_id}, headers=stranger
        )
        assert denied.status_code == 403
        page = client.get(
            "/analysis/temporal-windows",
            params={"id": media_id, "limit": 1, "offset": 0},
            headers=owner,
        )
        assert page.status_code == 200
        data = page.json()["data"]
        assert data["available"] is True
        assert data["granularity"] == "context-window-60s"
        assert data["total"] >= 2
        assert len(data["items"]) == 1
        assert data["items"][0]["startMs"] == 0
        assert data["items"][0]["transcript"]
        assert data["items"][0]["endMs"] > data["items"][0]["startMs"]
        next_page = client.get(
            "/analysis/temporal-windows",
            params={"id": media_id, "limit": 1, "offset": 1},
            headers=owner,
        ).json()["data"]
        assert next_page["items"][0]["startMs"] > data["items"][0]["startMs"]
        assert client.get(
            "/analysis/temporal-windows",
            params={"id": media_id, "limit": 201},
            headers=owner,
        ).status_code == 400


def test_citations_are_empty_before_analysis_and_owned(tmp_path) -> None:
    with TestClient(create_app(work_dir=tmp_path / "media")) as client:
        owner = _register(client, "citation_owner")
        stranger = _register(client, "citation_other")
        media_id = client.post(
            "/media/upload",
            files={"file": ("citation.mp4", b"local-video", "video/mp4")},
            headers=owner,
        ).json()["data"]["id"]
        params = {"id": media_id, "goal": "summarize", "mode": "GENERAL"}
        assert client.get("/analysis/agent-citations", params=params, headers=stranger).status_code == 403
        assert client.get("/analysis/agent-citations", params=params, headers=owner).json()["data"] == []
        params["includeConclusions"] = "true"
        assert client.get("/analysis/agent-citations", params=params, headers=stranger).status_code == 403
        page = client.get("/analysis/agent-citations", params=params, headers=owner).json()["data"]
        assert page["conclusions"] == []
        assert page["citations"] == []
        revision = client.get("/analysis/temporal-observations", params={"id": media_id}, headers=owner).json()["data"]["sourceRevision"]
        assert page["sourceRevision"] == revision


def test_observation_api_is_owned_paged_and_keeps_source_identity(tmp_path) -> None:
    with TestClient(create_app(work_dir=tmp_path / "media")) as client:
        owner = _register(client, "observation_owner")
        stranger = _register(client, "observation_other")
        media_id = client.post(
            "/media/upload",
            files={"file": ("sample.mp4", b"local-video", "video/mp4")},
            headers=owner,
        ).json()["data"]["id"]
        path = "/analysis/temporal-observations"
        assert client.get(path, params={"id": media_id}, headers=stranger).status_code == 403
        response = client.get(
            path, params={"id": media_id, "limit": 1, "offset": 0}, headers=owner
        )
        assert response.status_code == 200
        page = response.json()["data"]
        assert page["available"] is True
        assert page["granularity"] == "source-observation"
        assert page["total"] >= 2
        assert len(page["items"]) == 1
        assert page["items"][0]["id"].startswith("item_")
        assert page["items"][0]["sourceRevision"] == page["sourceRevision"]
        assert page["items"][0]["kind"] == "ASR"
        assert "frameRef" not in page["items"][0]
        assert client.get(
            path, params={"id": media_id, "limit": 1, "offset": 1}, headers=owner
        ).json()["data"]["items"][0]["startMs"] > page["items"][0]["startMs"]
        assert client.get(
            path, params={"id": media_id, "limit": 201}, headers=owner
        ).status_code == 400


def test_claim_projection_uses_same_owned_result_snapshot_and_legacy_array(tmp_path) -> None:
    app = create_app(work_dir=tmp_path / "media")
    with TestClient(app) as client:
        owner = _register(client, "claim_owner")
        media_id = client.post("/media/upload",
            files={"file": ("claims.mp4", b"local-video", "video/mp4")}, headers=owner,
        ).json()["data"]["id"]
        services = app.state.services
        context = client.portal.call(services.checkpoint.load_context, media_id)
        segment = context.segments[0]
        result = AnalysisResult(conclusions=["结论 A", "缺失引用"], evidence=[
            AnalysisEvidence(timestampMs=segment.source_items[0].timestamp_ms,
                source="ASR", content=segment.transcript.split("\n")[0], claim="结论 A"),
        ])
        key = services.key(media_id, "summarize", AnalysisMode.GENERAL)
        client.portal.call(services.checkpoint.save_result, key, AgentState(goal="summarize", result=result))
        params = {"id": media_id, "goal": "summarize", "mode": "GENERAL"}
        legacy = client.get("/analysis/agent-citations", params=params, headers=owner).json()["data"]
        page = client.get("/analysis/agent-citations", params={**params, "includeConclusions": True}, headers=owner).json()["data"]
        assert len(legacy) == 1
        assert page["citations"] == legacy
        assert page["conclusions"] == ["结论 A", "缺失引用"]
        assert page["sourceRevision"] == legacy[0]["sourceRevision"]
        other = client.get("/analysis/agent-citations", params={**params, "goal": "other", "includeConclusions": True}, headers=owner).json()["data"]
        assert other["conclusions"] == [] and other["citations"] == []
        client.portal.call(services.checkpoint.save_context, media_id,
            context.model_copy(update={"source_revision": "changed"}))
        changed = client.get("/analysis/agent-citations", params={**params, "includeConclusions": True}, headers=owner).json()["data"]
        assert changed["sourceRevision"] == "changed" and changed["citations"] == []
