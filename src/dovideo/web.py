"""Thin local Web Demo presentation adapter for VideoMind.

The web layer owns only browser transport, upload staging, process-local demo
jobs, progress projection, and safe DTO mapping.  Analysis is delegated to
the same :class:`VideoAnalysisApplication` composition root used by the CLI.
It is intentionally a standard-library server so the optional demo does not
add a Node toolchain or a production task infrastructure.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from dovideo.application import VIDEO_SUFFIXES
from dovideo.presentation.composition import (
    AnalysisRun,
    AnalysisSettings,
    VideoAnalysisApplication,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8765
DEFAULT_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
MAX_GOAL_LENGTH = 4_000
UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
ANALYSIS_ID_PATTERN = UPLOAD_ID_PATTERN
STAGES = (
    "MEDIA",
    "ASR",
    "OCR",
    "CONTEXT",
    "CHUNK",
    "EMBEDDING",
    "RETRIEVAL",
    "PLANNER",
    "EXECUTOR",
    "CRITIC",
    "EVIDENCE",
    "DONE",
)


def _new_stage_state() -> dict[str, str]:
    return {stage: "pending" for stage in STAGES}


def _decode_filename(value: str) -> str:
    """Decode the browser's percent-encoded name without trusting its path."""

    return unquote(value).strip()


def validate_upload_filename(value: str) -> tuple[str, str]:
    """Return a display name/suffix or reject unsafe/unsupported input.

    The returned name is never used as a storage path.  The server always
    generates the actual filename from a random upload id.
    """

    decoded = _decode_filename(value)
    if not decoded or "\x00" in decoded:
        raise ValueError("upload filename is required")
    normalized = decoded.replace("\\", "/")
    parts = normalized.split("/")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("upload filename contains a path traversal component")
    name = parts[-1].strip()
    if not name or name in {".", ".."}:
        raise ValueError("upload filename is invalid")
    suffix = Path(name).suffix.lower()
    if suffix not in VIDEO_SUFFIXES:
        allowed = ", ".join(sorted(VIDEO_SUFFIXES))
        raise ValueError(f"unsupported video format; use one of: {allowed}")
    return name, suffix


def normalize_embedding_mode(value: Any) -> str:
    """Validate the two presentation choices without touching providers."""

    mode = str(value or "local").strip().lower()
    if mode not in {"local", "remote"}:
        raise ValueError("embedding mode must be local or remote")
    return mode


@dataclass(frozen=True, slots=True)
class DemoUpload:
    upload_id: str
    filename: str
    path: Path
    size: int

    @property
    def url(self) -> str:
        return f"/api/uploads/{self.upload_id}/file"


@dataclass(slots=True)
class DemoJob:
    analysis_id: str
    upload_id: str
    filename: str
    goal: str
    embedding_mode: str
    status: str = "queued"
    current_stage: str | None = None
    stages: dict[str, str] = field(default_factory=_new_stage_state)
    messages: dict[str, str] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None


AnalysisRunner = Callable[
    [Path, str, str, Callable[[str, str], None]],
    AnalysisRun,
]


def _safe_error_message(error: BaseException) -> str:
    """Return a concise UI error with credentials and traces removed."""

    message = str(error).strip() or "analysis failed"
    message = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [redacted]", message)
    message = re.sub(r"(?i)sk-[a-z0-9_-]{12,}", "[redacted]", message)
    message = message.replace("\r", " ").replace("\n", " ")
    return message[:500]


def _segment_for_timestamp(run: AnalysisRun, timestamp_ms: int) -> Any:
    return next(
        (
            segment
            for segment in run.context.segments
            if segment.start_ms <= timestamp_ms < segment.end_ms
        ),
        None,
    )


def analysis_run_to_json(run: AnalysisRun, upload: DemoUpload) -> dict[str, Any]:
    """Map the existing result DTO to a provider-neutral browser payload."""

    result = run.result
    if result is None:
        raise ValueError("analysis produced no result")
    evidence: list[dict[str, Any]] = []
    for item in result.evidence:
        segment = _segment_for_timestamp(run, item.timestamp_ms)
        evidence.append(
            {
                "timestamp_ms": item.timestamp_ms,
                # AnalysisEvidence is a point timestamp in the frozen DTO.
                # Keep the point exact and expose the covered context window
                # separately instead of inventing a narrower evidence range.
                "start_ms": item.timestamp_ms,
                "end_ms": item.timestamp_ms,
                "window_start_ms": segment.start_ms if segment is not None else None,
                "window_end_ms": segment.end_ms if segment is not None else None,
                "source": item.source,
                "text": item.content,
                "claim": item.claim,
            }
        )
    return {
        "title": result.title,
        "conclusions": list(result.conclusions),
        "evidence": evidence,
        "suggestions": list(result.suggestions),
        "sections": [
            {"key": section.key, "title": section.title, "items": list(section.items)}
            for section in result.sections
        ],
        "media": {
            "filename": upload.filename,
            "url": upload.url,
            "duration_seconds": run.duration_seconds,
            "asr_span_count": run.asr_span_count,
            "ocr_observation_count": run.ocr_observation_count,
            "chunk_count": run.chunk_count,
            "embedding_dimension": run.embedding_dimension,
            "embedding_mode": run.embedding_mode,
        },
    }


class DemoJobRegistry:
    """Process-local browser state, deliberately separate from task storage."""

    def __init__(
        self,
        project_root: Path = PROJECT_ROOT,
        *,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        runner: AnalysisRunner | None = None,
    ) -> None:
        if max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        self.project_root = project_root.resolve()
        self.upload_dir = self.project_root / "work" / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.max_upload_bytes = max_upload_bytes
        self._uploads: dict[str, DemoUpload] = {}
        self._jobs: dict[str, DemoJob] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="dovideo-demo",
        )
        self._runner = runner or self._run_real_analysis

    def save_upload(
        self,
        filename: str,
        content_length: int,
        reader: Callable[[int], bytes],
    ) -> DemoUpload:
        display_name, suffix = validate_upload_filename(filename)
        if content_length <= 0:
            raise ValueError("uploaded video is empty")
        if content_length > self.max_upload_bytes:
            raise ValueError(
                f"uploaded video exceeds the local demo limit of {self.max_upload_bytes} bytes"
            )
        upload_id = uuid.uuid4().hex
        destination = self.upload_dir / f"{upload_id}{suffix}"
        written = 0
        try:
            with destination.open("xb") as handle:
                remaining = content_length
                while remaining:
                    chunk = reader(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("upload ended before Content-Length")
                    handle.write(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        if written <= 0:
            destination.unlink(missing_ok=True)
            raise ValueError("uploaded video is empty")
        upload = DemoUpload(upload_id, display_name, destination, written)
        with self._lock:
            self._uploads[upload_id] = upload
        return upload

    def get_upload(self, upload_id: str) -> DemoUpload | None:
        if not UPLOAD_ID_PATTERN.fullmatch(upload_id):
            return None
        with self._lock:
            upload = self._uploads.get(upload_id)
            if upload is None or not upload.path.is_file():
                return None
            return upload

    def submit(self, upload_id: str, goal: str, embedding_mode: Any) -> dict[str, Any]:
        upload = self.get_upload(upload_id)
        if upload is None:
            raise ValueError("uploaded video was not found; upload it again")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("analysis goal is required")
        goal = goal.strip()
        if len(goal) > MAX_GOAL_LENGTH:
            raise ValueError(f"analysis goal is limited to {MAX_GOAL_LENGTH} characters")
        mode = normalize_embedding_mode(embedding_mode)
        analysis_id = uuid.uuid4().hex
        job = DemoJob(analysis_id, upload_id, upload.filename, goal, mode)
        with self._lock:
            self._jobs[analysis_id] = job
        self._executor.submit(self._execute, job, upload)
        return self.snapshot(analysis_id) or {}

    def snapshot(self, analysis_id: str) -> dict[str, Any] | None:
        if not ANALYSIS_ID_PATTERN.fullmatch(analysis_id):
            return None
        with self._lock:
            job = self._jobs.get(analysis_id)
            if job is None:
                return None
            return {
                "analysis_id": job.analysis_id,
                "status": job.status,
                "filename": job.filename,
                "embedding_mode": job.embedding_mode,
                "progress": {
                    "current_stage": job.current_stage,
                    "stages": [
                        {
                            "name": stage,
                            "status": job.stages[stage],
                            "message": job.messages.get(stage, ""),
                        }
                        for stage in STAGES
                    ],
                },
                "result": job.result,
                "error": job.error,
            }

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _report(self, analysis_id: str, stage: str, message: str) -> None:
        stage = stage.strip().upper()
        if stage not in STAGES:
            return
        with self._lock:
            job = self._jobs.get(analysis_id)
            if job is None or job.status == "error":
                return
            job.messages[stage] = message.strip()[:240]
            if stage == "DONE":
                for name in STAGES:
                    job.stages[name] = "completed"
                job.current_stage = None
                return
            current_index = STAGES.index(stage)
            for name in STAGES[:current_index]:
                if job.stages[name] in {"pending", "running"}:
                    job.stages[name] = "completed"
            job.current_stage = stage
            job.stages[stage] = "running"

    def _execute(self, job: DemoJob, upload: DemoUpload) -> None:
        with self._lock:
            job.status = "running"
        try:
            run = self._runner(
                upload.path,
                job.goal,
                job.embedding_mode,
                lambda stage, message: self._report(job.analysis_id, stage, message),
            )
            result = analysis_run_to_json(run, upload)
        except Exception as error:
            with self._lock:
                job.status = "error"
                job.error = _safe_error_message(error)
                if job.current_stage is not None:
                    job.stages[job.current_stage] = "error"
            return
        with self._lock:
            job.status = "completed"
            job.result = result
            for name in STAGES:
                job.stages[name] = "completed"
            job.current_stage = None

    def _run_real_analysis(
        self,
        source: Path,
        goal: str,
        embedding_mode: str,
        progress: Callable[[str, str], None],
    ) -> AnalysisRun:
        settings = AnalysisSettings.from_environment(
            embedding_mode=embedding_mode,  # type: ignore[arg-type]
            project_root=self.project_root,
        )
        application = VideoAnalysisApplication(settings, progress=progress)
        return asyncio.run(application.analyze(source, goal))


def make_handler(registry: DemoJobRegistry) -> type[BaseHTTPRequestHandler]:
    """Create a handler bound to one process-local demo registry."""

    class DemoRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "VideoMindDemo/1.0"

        def log_message(self, _format: str, *args: Any) -> None:
            # Avoid default request logging of user-controlled paths in the
            # demo console.  No provider payloads or credentials are logged.
            return

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            path = urlsplit(self.path).path
            try:
                if path in {"/", "/index.html"}:
                    self._send_bytes(HTTPStatus.OK, WEB_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if path == "/api/health":
                    self._send_json(HTTPStatus.OK, {"status": "ok", "demo": True})
                    return
                analysis_match = re.fullmatch(r"/api/analysis/([0-9a-f]{32})", path)
                if analysis_match:
                    snapshot = registry.snapshot(analysis_match.group(1))
                    if snapshot is None:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "analysis not found"})
                    else:
                        self._send_json(HTTPStatus.OK, snapshot)
                    return
                upload_match = re.fullmatch(r"/api/uploads/([0-9a-f]{32})/file", path)
                if upload_match:
                    self._send_video(upload_match.group(1))
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "request failed"})

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
            path = urlsplit(self.path).path
            upload_match = re.fullmatch(r"/api/uploads/([0-9a-f]{32})/file", path)
            if upload_match:
                try:
                    self._send_video(upload_match.group(1), head_only=True)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, head_only=True)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            path = urlsplit(self.path).path
            try:
                if path == "/api/upload":
                    self._upload()
                    return
                if path == "/api/analysis":
                    self._submit_analysis()
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except (BrokenPipeError, ConnectionResetError):
                return
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": _safe_error_message(error)})
            except Exception:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "request failed"})

        def _content_length(self) -> int:
            value = self.headers.get("Content-Length")
            if value is None:
                raise ValueError("Content-Length is required")
            try:
                length = int(value)
            except ValueError as error:
                raise ValueError("Content-Length is invalid") from error
            if length < 0:
                raise ValueError("Content-Length is invalid")
            return length

        def _upload(self) -> None:
            filename = self.headers.get("X-File-Name", "")
            content_length = self._content_length()
            upload = registry.save_upload(filename, content_length, self.rfile.read)
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "upload_id": upload.upload_id,
                    "filename": upload.filename,
                    "size": upload.size,
                    "url": upload.url,
                },
            )

        def _submit_analysis(self) -> None:
            length = self._content_length()
            if length > MAX_JSON_BYTES:
                raise ValueError("analysis request is too large")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("analysis request ended early")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("analysis request must be valid JSON") from error
            if not isinstance(payload, Mapping):
                raise ValueError("analysis request must be a JSON object")
            snapshot = registry.submit(
                str(payload.get("upload_id", "")),
                payload.get("goal", ""),
                payload.get("embedding_mode", "local"),
            )
            self._send_json(HTTPStatus.ACCEPTED, snapshot)

        def _send_video(self, upload_id: str, *, head_only: bool = False) -> None:
            upload = registry.get_upload(upload_id)
            if upload is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "video not found"}, head_only=head_only)
                return
            size = upload.path.stat().st_size
            start = 0
            end = size - 1
            status = HTTPStatus.OK
            range_header = self.headers.get("Range")
            if range_header is not None:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
                if match is None or (not match.group(1) and not match.group(2)):
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                if match.group(1):
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) else size - 1
                else:
                    suffix_length = int(match.group(2))
                    start = max(0, size - suffix_length)
                    end = size - 1
                if start >= size or start > end:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = HTTPStatus.PARTIAL_CONTENT
            length = end - start + 1
            content_type = mimetypes.guess_type(upload.filename)[0] or "application/octet-stream"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head_only:
                return
            with upload.path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def _send_json(
            self,
            status: HTTPStatus,
            payload: Mapping[str, Any],
            *,
            head_only: bool = False,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._send_bytes(status, body, "application/json; charset=utf-8", head_only=head_only)

        def _send_bytes(
            self,
            status: HTTPStatus,
            body: bytes,
            content_type: str,
            *,
            head_only: bool = False,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

    return DemoRequestHandler


def run_web(
    host: str = DEFAULT_WEB_HOST,
    port: int = DEFAULT_WEB_PORT,
    *,
    project_root: Path = PROJECT_ROOT,
) -> int:
    """Start the local demo server until interrupted."""

    registry = DemoJobRegistry(project_root)
    handler = make_handler(registry)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    address_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    print(
        f"VideoMind Web Demo: http://{address_host}:{server.server_address[1]}",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        registry.close()
    return 0


WEB_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VideoMind — 视频智能分析</title>
  <style>
    :root { --ink:#14213d; --muted:#64748b; --line:#d9e2ef; --paper:#f5f7fb;
      --card:#ffffff; --blue:#2f6fed; --blue-soft:#e8f0ff; --green:#16845b;
      --amber:#b56b00; --red:#b42318; --shadow:0 18px 50px rgba(20,33,61,.08); }
    * { box-sizing:border-box; }
    body { margin:0; color:var(--ink); background:var(--paper); font:15px/1.55 Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; }
    .shell { max-width:1180px; margin:0 auto; padding:34px 22px 60px; }
    .masthead { display:flex; justify-content:space-between; align-items:flex-end; gap:24px; margin-bottom:26px; }
    .eyebrow { color:var(--blue); font-size:12px; font-weight:750; letter-spacing:.13em; text-transform:uppercase; }
    h1 { margin:5px 0 3px; font-size:clamp(27px,4vw,42px); letter-spacing:-.035em; }
    .subtitle { margin:0; color:var(--muted); font-size:17px; }
    .badge { border:1px solid #cbd8ee; border-radius:999px; background:#fff; color:var(--muted); padding:7px 12px; white-space:nowrap; font-size:12px; }
    .layout { display:grid; grid-template-columns:minmax(0,1.25fr) minmax(310px,.75fr); gap:18px; align-items:start; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:18px; box-shadow:var(--shadow); padding:20px; }
    .card h2 { margin:0 0 14px; font-size:18px; letter-spacing:-.015em; }
    .input-card { grid-row:span 2; }
    .drop { display:block; border:1.5px dashed #aebed7; border-radius:14px; padding:24px; background:#fbfcff; cursor:pointer; transition:.15s ease; }
    .drop:hover, .drop:focus-within { border-color:var(--blue); background:var(--blue-soft); }
    .drop strong { display:block; margin-bottom:4px; }
    .drop span { color:var(--muted); font-size:13px; }
    input[type=file] { position:absolute; width:1px; height:1px; opacity:0; pointer-events:none; }
    label.field-label { display:block; margin:18px 0 7px; font-size:13px; font-weight:700; }
    textarea { width:100%; min-height:96px; resize:vertical; border:1px solid var(--line); border-radius:10px; padding:11px 12px; color:var(--ink); background:#fff; font:inherit; }
    textarea:focus { outline:3px solid #dce8ff; border-color:var(--blue); }
    .mode-row { display:flex; gap:10px; flex-wrap:wrap; }
    .mode { display:flex; align-items:center; gap:8px; border:1px solid var(--line); border-radius:10px; padding:9px 11px; cursor:pointer; }
    .mode:has(input:checked) { border-color:var(--blue); background:var(--blue-soft); }
    .mode input { accent-color:var(--blue); }
    .actions { display:flex; align-items:center; gap:12px; margin-top:18px; }
    button { border:0; border-radius:10px; padding:10px 15px; color:#fff; background:var(--blue); font:700 14px inherit; cursor:pointer; }
    button:hover { filter:brightness(.96); } button:disabled { cursor:wait; opacity:.55; }
    .hint { color:var(--muted); font-size:12px; }
    video { width:100%; max-height:410px; border-radius:12px; background:#0d1728; margin-top:16px; }
    .empty-video { display:grid; place-items:center; min-height:170px; border-radius:12px; background:#0d1728; color:#aebbd0; margin-top:16px; }
    .progress-list { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }
    .stage { display:flex; align-items:center; gap:8px; border:1px solid #e5ebf3; border-radius:10px; padding:8px 9px; font-size:12px; color:var(--muted); }
    .stage .dot { width:9px; height:9px; border-radius:50%; background:#c8d2e1; flex:none; }
    .stage.running { color:var(--blue); border-color:#b9cffb; background:#f7faff; }
    .stage.running .dot { background:var(--blue); box-shadow:0 0 0 4px #dce8ff; }
    .stage.completed { color:var(--green); } .stage.completed .dot { background:var(--green); }
    .stage.error { color:var(--red); border-color:#f2c2be; } .stage.error .dot { background:var(--red); }
    .stage-name { font-weight:700; } .stage-message { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .result-card { grid-column:1 / -1; }
    .result-card.hidden, .error-box.hidden { display:none; }
    .result-title { margin:0 0 12px; font-size:25px; letter-spacing:-.02em; }
    .conclusion { border-left:3px solid var(--blue); padding:7px 0 7px 12px; margin:8px 0; }
    .evidence { display:grid; grid-template-columns:auto 1fr auto; gap:12px; align-items:start; border:1px solid var(--line); border-radius:12px; padding:12px; margin:9px 0; }
    .time { color:var(--blue); font:750 13px ui-monospace, SFMono-Regular, Menlo, monospace; white-space:nowrap; }
    .evidence-source { color:var(--muted); font-size:11px; font-weight:800; letter-spacing:.08em; text-transform:uppercase; }
    .evidence-text { margin:3px 0 0; } .claim { color:var(--muted); font-size:12px; margin-top:4px; }
    .jump { padding:7px 9px; font-size:12px; white-space:nowrap; }
    .error-box { margin-top:12px; border:1px solid #f2c2be; border-radius:10px; color:var(--red); background:#fff7f6; padding:10px 12px; }
    .meta { color:var(--muted); font-size:12px; margin-top:12px; }
    .section { margin-top:19px; } .section h3 { margin:0 0 7px; font-size:15px; }
    ul { margin:7px 0 0; padding-left:20px; } li { margin:3px 0; }
    footer { margin-top:18px; color:var(--muted); font-size:12px; }
    @media (max-width:800px) { .masthead { align-items:flex-start; flex-direction:column; } .layout { grid-template-columns:1fr; } .input-card { grid-row:auto; } .result-card { grid-column:auto; } }
    @media (max-width:530px) { .evidence { grid-template-columns:1fr; } .jump { justify-self:start; } }
  </style>
</head>
<body>
  <div class="shell">
    <header class="masthead">
      <div><div class="eyebrow">VideoMind</div><h1>视频智能分析</h1><p class="subtitle">从时间化证据追溯视频内容。</p></div>
      <div class="badge">本地演示 · 非生产服务</div>
    </header>
    <main class="layout">
      <section class="card input-card">
        <h2>分析视频</h2>
        <label class="drop" for="video-file"><strong id="file-label">选择本地视频</strong><span>支持 MP4、MOV、MKV、AVI、WEBM、M4V · 仅在本地演示环境处理</span><input id="video-file" type="file" accept=".mp4,.mov,.mkv,.avi,.webm,.m4v,video/*"></label>
        <div id="video-preview" class="empty-video">视频预览将在这里显示</div>
        <video id="player" controls hidden></video>
        <label class="field-label" for="goal">分析目标</label>
        <textarea id="goal" placeholder="例如：视频后半段如何解释海洋与潮汐？"></textarea>
        <label class="field-label">嵌入模式</label>
        <div class="mode-row"><label class="mode"><input type="radio" name="embedding" value="local" checked> Local TF-IDF</label><label class="mode"><input type="radio" name="embedding" value="remote"> Remote BGE-M3</label></div>
        <div class="actions"><button id="analyze" type="button">开始分析</button><span id="action-hint" class="hint">模型凭据仅保留在服务端。</span></div>
        <div id="error" class="error-box hidden" role="alert"></div>
      </section>
      <section class="card">
        <h2>分析进度</h2>
        <div id="progress-list" class="progress-list"></div>
        <div id="progress-note" class="meta">等待选择视频。</div>
      </section>
      <section class="card">
        <h2>本地演示说明</h2>
        <p class="meta">浏览器 → Web 层 → 应用服务 → AgentLoop → 证据校验。</p>
        <p class="meta">浏览器只负责上传、查询状态、显示结果和视频定位；模型调用与检索在服务端执行。</p>
      </section>
      <section id="result-card" class="card result-card hidden"><h2>分析结果</h2><div id="result"></div></section>
    </main>
    <footer>本地演示状态仅保存在内存中；生产任务与检查点以持久化基础设施为准。</footer>
  </div>
  <script>
    const STAGES = ["MEDIA","ASR","OCR","CONTEXT","CHUNK","EMBEDDING","RETRIEVAL","PLANNER","EXECUTOR","CRITIC","EVIDENCE","DONE"];
    const fileInput = document.getElementById("video-file");
    const fileLabel = document.getElementById("file-label");
    const preview = document.getElementById("video-preview");
    const player = document.getElementById("player");
    const goalInput = document.getElementById("goal");
    const analyzeButton = document.getElementById("analyze");
    const errorBox = document.getElementById("error");
    const progressList = document.getElementById("progress-list");
    const progressNote = document.getElementById("progress-note");
    const resultCard = document.getElementById("result-card");
    const resultBox = document.getElementById("result");
    let selectedFile = null;

    function textNode(tag, value, className) { const element = document.createElement(tag); element.textContent = value || ""; if (className) element.className = className; return element; }
    function formatTime(ms) { const total = Math.max(0, Math.round(Number(ms || 0) / 1000)); const h = Math.floor(total / 3600); const m = Math.floor((total % 3600) / 60); const s = total % 60; return h ? `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}` : `${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`; }
    function formatDuration(seconds) { return formatTime(Number(seconds || 0) * 1000); }
    function showError(message) { errorBox.textContent = message || "请求失败"; errorBox.classList.remove("hidden"); }
    function clearError() { errorBox.textContent = ""; errorBox.classList.add("hidden"); }
    function selectedMode() { return document.querySelector('input[name="embedding"]:checked').value; }
    function renderProgress(progress) {
      const items = (progress && progress.stages) || STAGES.map(name => ({name, status:"pending", message:""}));
      progressList.textContent = "";
      items.forEach(item => { const row = document.createElement("div"); row.className = `stage ${item.status || "pending"}`; row.appendChild(document.createElement("span")).className = "dot"; const copy = document.createElement("div"); copy.appendChild(textNode("div", item.name, "stage-name")); if (item.message) copy.appendChild(textNode("span", item.message, "stage-message")); row.appendChild(copy); progressList.appendChild(row); });
      progressNote.textContent = progress && progress.current_stage ? `当前阶段：${progress.current_stage}` : "等待分析。";
    }
    function renderEvidence(item) {
      const row = document.createElement("div"); row.className = "evidence";
      const time = document.createElement("div"); time.className = "time"; time.textContent = item.window_start_ms != null ? `${formatTime(item.timestamp_ms)} · ${formatTime(item.window_start_ms)}–${formatTime(item.window_end_ms)}` : formatTime(item.timestamp_ms); row.appendChild(time);
      const copy = document.createElement("div"); copy.appendChild(textNode("div", item.source || "未知", "evidence-source")); copy.appendChild(textNode("div", item.text || "", "evidence-text")); if (item.claim) copy.appendChild(textNode("div", `结论：${item.claim}`, "claim")); row.appendChild(copy);
      const jump = document.createElement("button"); jump.type = "button"; jump.className = "jump"; jump.textContent = "跳转证据"; jump.addEventListener("click", () => { player.currentTime = Math.max(0, Number(item.start_ms || item.timestamp_ms || 0) / 1000); player.classList.add("selected"); player.play().catch(() => {}); player.scrollIntoView({behavior:"smooth", block:"center"}); }); row.appendChild(jump); return row;
    }
    function renderResult(result) {
      resultBox.textContent = ""; resultBox.appendChild(textNode("h3", result.title || "分析结果", "result-title"));
      if ((result.conclusions || []).length) { resultBox.appendChild(textNode("h3", "核心结论")); result.conclusions.forEach(value => resultBox.appendChild(textNode("div", value, "conclusion"))); }
      if ((result.evidence || []).length) { const section = document.createElement("div"); section.className = "section"; section.appendChild(textNode("h3", "视频证据")); result.evidence.forEach(item => section.appendChild(renderEvidence(item))); resultBox.appendChild(section); }
      if ((result.suggestions || []).length) { const section = document.createElement("div"); section.className = "section"; section.appendChild(textNode("h3", "建议")); const list = document.createElement("ul"); result.suggestions.forEach(value => list.appendChild(textNode("li", value))); section.appendChild(list); resultBox.appendChild(section); }
      (result.sections || []).forEach(sectionData => { const section = document.createElement("div"); section.className = "section"; section.appendChild(textNode("h3", sectionData.title || sectionData.key || "详情")); const list = document.createElement("ul"); (sectionData.items || []).forEach(value => list.appendChild(textNode("li", value))); section.appendChild(list); resultBox.appendChild(section); });
      if (result.media) { resultBox.appendChild(textNode("div", `${result.media.filename} · ${formatDuration(result.media.duration_seconds)} · ${result.media.chunk_count} 个片段 · ${result.media.embedding_mode}`, "meta")); }
      resultCard.classList.remove("hidden");
    }
    async function jsonResponse(response) { const payload = await response.json().catch(() => ({})); if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`); return payload; }
    async function uploadFile(file) { const response = await fetch("/api/upload", {method:"POST", headers:{"X-File-Name":encodeURIComponent(file.name), "Content-Type":file.type || "application/octet-stream"}, body:file}); return jsonResponse(response); }
    async function pollAnalysis(id) { while (true) { const payload = await jsonResponse(await fetch(`/api/analysis/${id}`, {cache:"no-store"})); renderProgress(payload.progress); if (payload.status === "completed") { renderResult(payload.result); progressNote.textContent = "分析完成；证据核验结果已就绪。"; return; } if (payload.status === "error") throw new Error(payload.error || "分析失败"); await new Promise(resolve => setTimeout(resolve, 800)); } }
    fileInput.addEventListener("change", () => { selectedFile = fileInput.files && fileInput.files[0]; if (!selectedFile) return; clearError(); fileLabel.textContent = selectedFile.name; preview.hidden = true; player.hidden = false; player.src = URL.createObjectURL(selectedFile); player.load(); });
    analyzeButton.addEventListener("click", async () => { clearError(); resultCard.classList.add("hidden"); resultBox.textContent = ""; if (!selectedFile) { showError("请先选择视频文件。"); return; } if (!goalInput.value.trim()) { showError("请先输入分析目标。"); goalInput.focus(); return; } analyzeButton.disabled = true; progressNote.textContent = "正在上传视频…"; try { const upload = await uploadFile(selectedFile); player.src = upload.url; player.load(); progressNote.textContent = "正在启动分析…"; const response = await fetch("/api/analysis", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({upload_id:upload.upload_id, goal:goalInput.value.trim(), embedding_mode:selectedMode()})}); const job = await jsonResponse(response); renderProgress(job.progress); await pollAnalysis(job.analysis_id); } catch (error) { showError(error.message || "分析失败"); progressNote.textContent = "本地演示请求已结束，存在错误。"; } finally { analyzeButton.disabled = false; } });
    renderProgress(null);
  </script>
</body>
</html>'''


__all__ = [
    "DEFAULT_MAX_UPLOAD_BYTES",
    "DEFAULT_WEB_HOST",
    "DEFAULT_WEB_PORT",
    "DemoJobRegistry",
    "DemoJob",
    "DemoUpload",
    "STAGES",
    "WEB_HTML",
    "analysis_run_to_json",
    "make_handler",
    "normalize_embedding_mode",
    "run_web",
    "validate_upload_filename",
]


if __name__ == "__main__":
    raise SystemExit(run_web())
