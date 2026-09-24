from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from dovideo.cli import build_parser
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisResult,
    CriticResult,
    VideoContext,
    VideoSegment,
)
from dovideo.presentation.composition import AnalysisRun
from dovideo.web import (
    DemoJobRegistry,
    STAGES,
    WEB_HTML,
    analysis_run_to_json,
    make_handler,
    normalize_embedding_mode,
    validate_upload_filename,
)


def _fake_runner(
    source: Path,
    goal: str,
    embedding_mode: str,
    progress: Any,
) -> AnalysisRun:
    for stage in STAGES:
        progress(stage, f"fake {stage.lower()}")
    context = VideoContext(
        source=str(source),
        user_goal=goal,
        segments=(
            VideoSegment(
                start_ms=0,
                end_ms=60_000,
                transcript="The evidence sentence is here.",
            ),
        ),
    )
    result = AnalysisResult(
        title="Demo result",
        conclusions=("The evidence sentence is here.",),
        evidence=(
            AnalysisEvidence(
                timestamp_ms=12_000,
                source="ASR",
                content="The evidence sentence is here.",
                claim="The evidence sentence is here.",
            ),
        ),
        suggestions=("Review the source window.",),
    )
    state = AgentState(
        goal=goal,
        plan=AgentPlan(understoodGoal=goal, tasks=("quote the evidence",)),
        result=result,
        critique=CriticResult(passed=True),
        round=1,
    )
    return AnalysisRun(
        source=source,
        goal=goal,
        duration_seconds=60,
        context=context,
        state=state,
        media_id=1,
        asr_span_count=1,
        ocr_observation_count=0,
        chunk_count=1,
        embedding_dimension=4,
        embedding_mode=embedding_mode,  # type: ignore[arg-type]
    )


def _request(
    address: tuple[str, int],
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(address[0], address[1], timeout=3)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    payload = response.read()
    result = (response.status, dict(response.getheaders()), payload)
    connection.close()
    return result


@pytest.fixture()
def web_server(tmp_path: Path):
    registry = DemoJobRegistry(tmp_path, runner=_fake_runner)
    server = __import__("http.server", fromlist=["ThreadingHTTPServer"]).ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(registry)
    )
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = ("127.0.0.1", int(server.server_address[1]))
    try:
        yield address, registry
    finally:
        server.shutdown()
        server.server_close()
        registry.close()
        thread.join(timeout=2)


def _upload(address: tuple[str, int], filename: str = "clip.mp4") -> dict[str, Any]:
    body = b"video fixture bytes"
    status, _, raw = _request(
        address,
        "POST",
        "/api/upload",
        body=body,
        headers={
            "Content-Length": str(len(body)),
            "Content-Type": "video/mp4",
            "X-File-Name": quote(filename, safe=""),
        },
    )
    assert status == 201
    return json.loads(raw)


def test_web_page_is_static_and_contains_no_provider_credentials() -> None:
    assert "DOVideo" in WEB_HTML
    assert "/api/upload" in WEB_HTML
    assert "DOVIDEO_MODEL_API_KEY" not in WEB_HTML
    assert "DOVIDEO_EMBEDDING_API_KEY" not in WEB_HTML


def test_upload_name_validation_is_safe_and_format_bounded() -> None:
    assert validate_upload_filename("folder/clip.MP4") == ("clip.MP4", ".mp4")
    with pytest.raises(ValueError, match="traversal"):
        validate_upload_filename("../escape.mp4")
    with pytest.raises(ValueError, match="unsupported"):
        validate_upload_filename("notes.txt")


def test_embedding_mode_and_cli_web_command_are_bounded() -> None:
    assert normalize_embedding_mode(None) == "local"
    assert normalize_embedding_mode("REMOTE") == "remote"
    with pytest.raises(ValueError):
        normalize_embedding_mode("provider-x")
    args = build_parser().parse_args(["web", "--port", "8766"])
    assert args.command == "web"
    assert args.port == 8766


def test_upload_endpoint_generates_safe_storage_name_and_rejects_traversal(web_server) -> None:
    address, registry = web_server
    status, _, _ = _request(
        address,
        "POST",
        "/api/upload",
        body=b"x",
        headers={
            "Content-Length": "1",
            "X-File-Name": quote("../../escape.mp4", safe=""),
        },
    )
    assert status == 400

    upload = _upload(address, "meeting final.mp4")
    record = registry.get_upload(upload["upload_id"])
    assert record is not None
    assert record.filename == "meeting final.mp4"
    assert record.path.parent == registry.upload_dir
    assert record.path.name.startswith(upload["upload_id"])
    assert ".." not in record.path.name


def test_video_endpoint_supports_native_player_range_requests(web_server) -> None:
    address, _ = web_server
    upload = _upload(address)
    status, headers, body = _request(
        address,
        "GET",
        upload["url"],
        headers={"Range": "bytes=2-5"},
    )
    assert status == 206
    assert body == b"deo "
    assert headers["Content-Range"].startswith("bytes 2-5/")
    assert headers["Accept-Ranges"] == "bytes"


def test_analysis_status_and_completed_result_use_existing_dto_mapping(web_server) -> None:
    address, _ = web_server
    upload = _upload(address)
    request = json.dumps(
        {
            "upload_id": upload["upload_id"],
            "goal": "find the evidence",
            "embedding_mode": "remote",
        }
    ).encode("utf-8")
    status, _, raw = _request(
        address,
        "POST",
        "/api/analysis",
        body=request,
        headers={"Content-Length": str(len(request)), "Content-Type": "application/json"},
    )
    assert status == 202
    analysis_id = json.loads(raw)["analysis_id"]

    deadline = time.monotonic() + 2
    while True:
        status, _, raw = _request(address, "GET", f"/api/analysis/{analysis_id}")
        assert status == 200
        payload = json.loads(raw)
        if payload["status"] == "completed":
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)

    assert payload["embedding_mode"] == "remote"
    assert all(item["status"] == "completed" for item in payload["progress"]["stages"])
    evidence = payload["result"]["evidence"][0]
    assert evidence["timestamp_ms"] == 12_000
    assert evidence["start_ms"] == 12_000
    assert evidence["end_ms"] == 12_000
    assert evidence["window_start_ms"] == 0
    assert evidence["window_end_ms"] == 60_000
    assert evidence["source"] == "ASR"
    assert payload["result"]["media"]["url"] == upload["url"]


def test_missing_or_invalid_analysis_input_is_user_readable(web_server) -> None:
    address, _ = web_server
    body = json.dumps({"goal": "missing upload"}).encode("utf-8")
    status, _, raw = _request(
        address,
        "POST",
        "/api/analysis",
        body=body,
        headers={"Content-Length": str(len(body)), "Content-Type": "application/json"},
    )
    assert status == 400
    response = json.loads(raw)
    assert "uploaded video" in response["error"]
    assert "Traceback" not in raw.decode("utf-8")


def test_analysis_error_endpoint_does_not_return_a_stack_trace(tmp_path: Path) -> None:
    def failing_runner(source: Path, goal: str, mode: str, progress: Any) -> AnalysisRun:
        raise RuntimeError("missing model API key")

    registry = DemoJobRegistry(tmp_path, runner=failing_runner)
    upload_body = b"video fixture bytes"
    upload = registry.save_upload("clip.mp4", len(upload_body), lambda size: upload_body)
    snapshot = registry.submit(upload.upload_id, "find evidence", "local")
    deadline = time.monotonic() + 2
    while True:
        payload = registry.snapshot(snapshot["analysis_id"])
        assert payload is not None
        if payload["status"] == "error":
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert payload["error"] == "missing model API key"
    assert "Traceback" not in payload["error"]
    registry.close()


def test_analysis_run_mapping_keeps_point_timestamp_and_context_window(tmp_path: Path) -> None:
    body = b"video fixture bytes"
    registry = DemoJobRegistry(tmp_path, runner=_fake_runner)
    upload = registry.save_upload("clip.mp4", len(body), lambda size: body)
    run = _fake_runner(upload.path, "find evidence", "local", lambda *_: None)
    payload = analysis_run_to_json(run, upload)
    assert payload["evidence"][0]["timestamp_ms"] == 12_000
    assert payload["evidence"][0]["window_start_ms"] == 0
    assert payload["evidence"][0]["window_end_ms"] == 60_000
    registry.close()
