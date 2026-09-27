from __future__ import annotations

from fastapi.testclient import TestClient

from dovideo.presentation.api import create_app


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
