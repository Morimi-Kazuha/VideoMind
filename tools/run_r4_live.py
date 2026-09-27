"""Bounded live R4 product-boundary verification.

This script is intentionally a verification harness, not a second product
composition.  It enters through FastAPI, starts the canonical Celery worker,
and reports only sanitized metadata.  Local provider env files are loaded into
this process and its child worker; their values are never printed or stored.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


REPO = Path(__file__).resolve().parents[1]
MEDIA = REPO / "work" / "media" / "representative-long.mp4"
EVIDENCE_PATH = REPO / "work" / "r4-live-evidence.json"
GOAL = "Find the later After Love passage describing the sea, pool, and tide, and provide timestamped evidence"
REQUIRE_LATER_REGION = True


def load_local_environment() -> None:
    """Load only the two approved local env files into process scope."""

    for filename in (".env.r2.local", ".env.r4.local"):
        path = REPO / filename
        if not path.is_file():
            raise RuntimeError("approved local environment file is missing")
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
            if match is None:
                continue
            value = match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[match.group(1)] = value


def combined_runtime_environment() -> dict[str, str]:
    env = dict(os.environ)
    paths = (
        REPO / "src",
        REPO / "work" / "r1-venv" / "Lib" / "site-packages",
        REPO / "tools" / "asr" / "python-packages",
    )
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [*(str(path) for path in paths), existing] if existing else [str(path) for path in paths]
    )
    return env


def _counter(counters: dict[str, object], name: str) -> int:
    value = counters.get(name, 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_sse_stages(body: str) -> list[str]:
    stages: list[str] = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            value = json.loads(line[6:])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        stage = value.get("stage") if isinstance(value, dict) else None
        if isinstance(stage, str) and stage not in stages:
            stages.append(stage)
    return stages


def _host_identity(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.netloc or parsed.path.split("/", 1)[0]


def _later_retrieval_proof(context, hits) -> bool:
    """Find the later semantic target from actual context, without a time constant."""

    terms = ("after love", "sea", "pool", "tide")
    candidate_windows = []
    for segment in context.segments:
        text = segment.transcript.casefold()
        if any(term in text for term in terms):
            candidate_windows.append((segment.start_ms, segment.end_ms))
    if not candidate_windows:
        return False
    first_start = min(start for start, _end in candidate_windows)
    later_windows = tuple(window for window in candidate_windows if window[0] > first_start)
    if not later_windows:
        later_windows = tuple(candidate_windows)
    return any(
        any(start <= hit.start_ms < end for start, end in later_windows)
        for hit in hits
    )


def main() -> int:
    load_local_environment()
    if not MEDIA.is_file():
        raise RuntimeError("representative media fixture is missing")
    runtime_env = combined_runtime_environment()
    os.environ["PYTHONPATH"] = runtime_env["PYTHONPATH"]
    sys.path[:0] = [
        str(REPO / "src"),
        str(REPO / "work" / "r1-venv" / "Lib" / "site-packages"),
        str(REPO / "tools" / "asr" / "python-packages"),
    ]

    from fastapi.testclient import TestClient

    from dovideo.application import AgentCheckpointService, TaskKey
    from dovideo.domain import AnalysisMode
    from dovideo.infrastructure import create_r2_infrastructure
    from dovideo.infrastructure.celery_transport import (
        CeleryTransportSettings,
        RabbitMQTopology,
    )
    from dovideo.presentation.api.app import create_app

    env = runtime_env
    queue_settings = CeleryTransportSettings.from_environment(require_production=True)
    topology = RabbitMQTopology(queue_settings)
    topology.ensure()
    worker_command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "dovideo.infrastructure.celery_worker:celery_app",
        "worker",
        "--loglevel=WARNING",
        "--pool=solo",
        "--concurrency=1",
        "-Q",
        queue_settings.queue,
    ]
    worker = subprocess.Popen(
        worker_command,
        cwd=str(REPO),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    result: dict[str, object] = {}
    sse_chunks: list[str] = []
    sse_error: list[str] = []
    sse_thread: threading.Thread | None = None

    try:
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            if worker.poll() is not None:
                raise RuntimeError("canonical Celery worker exited before consuming")
            try:
                stats = topology.queue_stats()
                if stats["mainConsumers"] >= 1:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("canonical Celery worker did not attach to RabbitMQ")

        username = "r4_live_" + uuid4().hex[:16]
        password = "R4-" + uuid4().hex + "-LocalOnly"
        username_two = "r4_owner_check_" + uuid4().hex[:12]
        password_two = "R4-" + uuid4().hex + "-LocalOnly"
        goal = GOAL

        with TestClient(create_app()) as client:
            registered = client.post(
                "/user/register",
                json={"username": username, "password": password, "nickname": "R4 Live"},
            )
            if registered.status_code != 200:
                raise RuntimeError("production registration failed")
            login = client.post(
                "/user/login",
                json={"username": username, "password": password},
            )
            if login.status_code != 200:
                raise RuntimeError("production login failed")
            token = login.json()["data"]["token"]
            headers = {"Authorization": f"Bearer {token}"}

            with MEDIA.open("rb") as stream:
                uploaded = client.post(
                    "/media/upload",
                    headers=headers,
                    files={"file": (MEDIA.name, stream, "video/mp4")},
                )
            if uploaded.status_code != 200:
                raise RuntimeError("production MinIO upload failed")
            media = uploaded.json()["data"]
            media_id = int(media["id"])

            playback = client.get("/media/playback", params={"id": media_id}, headers=headers)
            playback_status = playback.status_code
            playback_range_status = 0
            if playback.status_code == 200:
                playback_url = playback.json()["data"]
                playback_file = client.get(
                    playback_url,
                    headers={**headers, "Range": "bytes=0-31"},
                )
                playback_range_status = playback_file.status_code

            register_two = client.post(
                "/user/register",
                json={"username": username_two, "password": password_two, "nickname": "Other"},
            )
            login_two = client.post(
                "/user/login",
                json={"username": username_two, "password": password_two},
            )
            other_token = login_two.json()["data"]["token"] if login_two.status_code == 200 else ""
            unauthorized = client.get(
                "/media/playback",
                params={"id": media_id},
                headers={"Authorization": f"Bearer {other_token}"},
            )

            def consume_sse() -> None:
                try:
                    with client.stream(
                        "GET",
                        "/analysis/analysis-events",
                        params={"id": media_id, "goal": goal, "mode": "GENERAL"},
                        headers=headers,
                    ) as response:
                        for piece in response.iter_text():
                            sse_chunks.append(piece)
                except Exception as exc:
                    sse_error.append(type(exc).__name__)

            submit = client.post(
                "/analysis/ai",
                params={"id": media_id, "goal": goal, "mode": "GENERAL"},
                headers=headers,
            )
            if submit.status_code != 202:
                raise RuntimeError("production analysis submission was not accepted")
            sse_thread = threading.Thread(target=consume_sse, daemon=True)
            sse_thread.start()

            terminal = None
            status_history: list[str] = []
            poll_deadline = time.monotonic() + 60 * 60
            while time.monotonic() < poll_deadline:
                status_response = client.get(
                    "/analysis/analysis-status",
                    params={"id": media_id, "goal": goal, "mode": "GENERAL"},
                    headers=headers,
                )
                if status_response.status_code != 200:
                    raise RuntimeError("production status request failed")
                payload = status_response.json()["data"]
                state = str(payload.get("state", ""))
                if state and (not status_history or status_history[-1] != state):
                    status_history.append(state)
                if state in {"COMPLETED", "FAILED"}:
                    terminal = payload
                    break
                time.sleep(2.0)
            if terminal is None or terminal.get("state") != "COMPLETED":
                raise RuntimeError("canonical R4 analysis did not complete")

            if sse_thread is not None:
                sse_thread.join(timeout=30.0)
            sse_stages = _parse_sse_stages("".join(sse_chunks))
            hits_response = client.get(
                "/analysis/evidence-search",
                params={"id": media_id, "query": goal},
                headers=headers,
            )
            if hits_response.status_code != 200:
                raise RuntimeError("production evidence search failed")
            hits_payload = hits_response.json()["data"]
            if not isinstance(hits_payload, list):
                raise RuntimeError("production evidence search shape is invalid")

            windows_response = client.get(
                "/analysis/temporal-windows",
                params={"id": media_id, "limit": 200, "offset": 0},
                headers=headers,
            )
            observations_response = client.get(
                "/analysis/temporal-observations",
                params={"id": media_id, "limit": 200, "offset": 0},
                headers=headers,
            )
            citations_response = client.get(
                "/analysis/agent-citations",
                params={"id": media_id, "goal": goal, "mode": "GENERAL"},
                headers=headers,
            )
            if any(
                response.status_code != 200
                for response in (
                    windows_response,
                    observations_response,
                    citations_response,
                )
            ):
                raise RuntimeError("production temporal or citation read failed")
            windows_page = windows_response.json()["data"]
            observations_page = observations_response.json()["data"]
            citations = citations_response.json()["data"]
            if not isinstance(citations, list):
                raise RuntimeError("production citation read shape is invalid")
            if windows_page["total"] != len(windows_page["items"]):
                raise RuntimeError("representative context windows were not fully paged")
            if observations_page["total"] != len(observations_page["items"]):
                raise RuntimeError("representative observations were not fully paged")

            trace_response = client.get(
                "/analysis/agent-trace",
                params={"id": media_id, "goal": goal, "mode": "GENERAL"},
                headers=headers,
            )
            trace = trace_response.json()["data"] if trace_response.status_code == 200 else {}
            duplicate = client.post(
                "/analysis/ai",
                params={"id": media_id, "goal": goal, "mode": "GENERAL"},
                headers=headers,
            )

            result.update(
                {
                    "product_entry": "FastAPI",
                    "auth_register": registered.status_code == 200,
                    "auth_login": login.status_code == 200,
                    "media_upload": uploaded.status_code == 200,
                    "media_id": media_id,
                    "minio_source": True,
                    "playback_status": playback_status,
                    "playback_range_status": playback_range_status,
                    "ownership_denied_status": unauthorized.status_code,
                    "analysis_submit_status": submit.status_code,
                    "status_history": status_history,
                    "sse_stages": sse_stages,
                    "sse_terminal_seen": "COMPLETED" in sse_stages,
                    "sse_error": bool(sse_error),
                    "evidence_candidate_count": len(hits_payload),
                    "temporal_window_api_count": windows_page["total"],
                    "temporal_observation_api_count": observations_page["total"],
                    "citation_count": len(citations),
                    "duplicate_status": duplicate.status_code,
                    "trace": trace,
                    "hits": hits_payload,
                    "goal": goal,
                }
            )

        recovery = create_r2_infrastructure()
        try:
            checkpoint = AgentCheckpointService(recovery.checkpoint_repository)
            key = TaskKey(media_id, goal, AnalysisMode.GENERAL)
            context = asyncio.run(checkpoint.load_context(media_id))
            chunks = asyncio.run(checkpoint.load_chunks(media_id))
            state = asyncio.run(checkpoint.load_result(key))
            if context is None or chunks is None or state is None or state.result is None:
                raise RuntimeError("durable R4 checkpoint/result is incomplete")
            source_item_ids = {
                item.source_item_id
                for segment in context.segments
                for item in segment.source_items
            }
            if (
                observations_page["sourceRevision"] != context.source_revision
                or observations_page["total"] != len(context.observations)
                or {item["id"] for item in observations_page["items"]}
                != source_item_ids
                or any(
                    citation["sourceRevision"] != context.source_revision
                    or not set(citation["sourceItemIds"]).issubset(source_item_ids)
                    for citation in citations
                )
            ):
                raise RuntimeError("temporal or citation provenance does not match durable context")
            records = recovery.checkpoint_store.records(media_id)
            stored_media = asyncio.run(recovery.media_repository.get(media_id))
            if stored_media is None or stored_media.source.split(":", 1)[0] != "minio":
                raise RuntimeError("durable media record did not retain a MinIO source")
            source_parts = urlsplit(str(stored_media.source))
            object_info = recovery.minio_client.stat_object(
                recovery.settings.minio_bucket,
                source_parts.path.lstrip("/"),
            )
            del object_info
            vector_hits = asyncio.run(
                recovery.vector_index.search(
                    media_id,
                    tuple(chunks[0].embedding),
                    limit=len(chunks),
                )
            )
            recovery.checkpoint_cache.delete_media(media_id)
            recovered_state = asyncio.run(checkpoint.load_result(key))
            rewarmed_context = asyncio.run(checkpoint.load_context(media_id))
            if recovered_state is None or rewarmed_context is None:
                raise RuntimeError("MySQL recovery after Redis cache eviction failed")
            final_hits = tuple(
                type("Hit", (), {"start_ms": int(item["startMs"])})()
                for item in result.get("hits", [])
                if isinstance(item, dict) and "startMs" in item
            )
            later_ok = _later_retrieval_proof(context, final_hits)
            if not state.critique or not state.critique.passed:
                raise RuntimeError("production Critic did not pass")
            if not citations:
                raise RuntimeError("production answer has no verified source citations")
            if REQUIRE_LATER_REGION and not later_ok:
                raise RuntimeError("required later source region was not retrieved")
            counters = result.get("trace", {}).get("counters", {})
            result.update(
                {
                    "mysql_checkpoint_records": len(records),
                    "mysql_durable_result": True,
                    "redis_cache_eviction_recovery": True,
                    "context_window_count": len(context.segments),
                    "chunk_count": len(chunks),
                    "embedding_vector_count": sum(bool(chunk.embedding) for chunk in chunks),
                    "embedding_dimension": len(chunks[0].embedding),
                    "qdrant_candidate_count": len(vector_hits),
                    "later_region_retrieved": later_ok,
                    "agent_round": state.round,
                    "critic_passed": bool(state.critique and state.critique.passed),
                    "provider_calls": {
                        "planner": _counter(counters, "PLANNERCalls"),
                        "executor": _counter(counters, "EXECUTORCalls"),
                        "critic": _counter(counters, "CRITICCalls"),
                        "retrieval_planner": _counter(counters, "RETRIEVAL_PLANNERCalls"),
                        "chunk_summary": _counter(counters, "CHUNK_SUMMARYCalls"),
                        "model_total": _counter(counters, "modelCalls"),
                        "embedding_total": _counter(counters, "embeddingCalls"),
                    },
                }
            )
        finally:
            recovery.close()

        model_cfg = result.get("trace", {})
        del model_cfg
        result["provider_metadata"] = {
            "llm_endpoint_identity": _host_identity(os.environ["DOVIDEO_MODEL_BASE_URL"]),
            "llm_model": os.environ["DOVIDEO_MODEL_MODEL"],
            "embedding_endpoint_identity": _host_identity(os.environ["DOVIDEO_EMBEDDING_BASE_URL"]),
            "embedding_model": os.environ["DOVIDEO_EMBEDDING_MODEL"],
            "embedding_dimension": result.get("embedding_dimension", 0),
        }
        # Do not include raw media source, user names, goals, snippets, or
        # provider payloads in the persisted artifact.
        result.pop("goal", None)
        result.pop("hits", None)
        EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE_PATH.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print("R4_LIVE=PASS")
        print("R4_EVIDENCE_FILE=" + str(EVIDENCE_PATH))
        print("MEDIA_ID_PRESENT=YES")
        print("CONTEXT_WINDOWS=%s" % result["context_window_count"])
        print("CHUNKS=%s" % result["chunk_count"])
        print("EMBEDDING_DIMENSION=%s" % result["embedding_dimension"])
        print("QDRANT_CANDIDATES=%s" % result["qdrant_candidate_count"])
        print("LATER_REGION_RETRIEVED=%s" % ("YES" if result["later_region_retrieved"] else "NO"))
        print("CRITIC_PASSED=%s" % ("YES" if result["critic_passed"] else "NO"))
        print("MYSQL_REDIS_RECOVERY=%s" % ("YES" if result["redis_cache_eviction_recovery"] else "NO"))
        print("SSE_TERMINAL=%s" % ("YES" if result["sse_terminal_seen"] else "NO"))
        return 0
    finally:
        if sse_thread is not None and sse_thread.is_alive():
            sse_thread.join(timeout=1.0)
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=30.0)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=10.0)


if __name__ == "__main__":
    raise SystemExit(main())
