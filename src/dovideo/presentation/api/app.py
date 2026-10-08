"""FastAPI application factory for the R1 public product surface."""

from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from inspect import isawaitable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from dovideo.application import (
    AgentFeedback,
    AiInteractionLimiterUnavailable,
    DispatchDisposition,
    FailedTaskAdminError,
    HistoricalReplayError,
    ReplayAccessError,
    ReplayAccessForbiddenError,
    ReplayAccessNotFoundError,
    ReplayAccessUnavailableError,
    ReplayArtifactUnavailableError,
    ReplayIncompleteError,
    ReplayIncompatibleVersionError,
    ReplayIntegrityError,
    ReplayLegacyUnavailableError,
    ReplayNotFoundError,
    ReplayPersistenceError,
    TaskKey,
    TaskLifecycleEvent,
)
from dovideo.domain import AnalysisMode, TaskEvent
from dovideo.application.errors import (
    InvalidMediaInput, MediaUnauthorized, UploadNotFoundOrExpired, UploadConflict,
    MediaStorageFailure, MediaRecordFailure, UnsupportedVideoFormat, MediaPayloadTooLarge,
)
from dovideo.application.media import normalize_video_filename
from dovideo.application.temporal_read import (
    temporal_observation_page,
    temporal_window_page,
    verified_answer_citations,
    verified_answer_presentation,
)

from .runtime import LocalR1Services, R1ServiceError, media_summary
from .schemas import (
    AuthData,
    LoginRequest,
    RegisterRequest,
    RouteDecision,
    RouteRequest,
    UploadInitData,
    UploadStatusData,
)


@dataclass(frozen=True, slots=True)
class ApiSettings:
    """Small environment-driven presentation configuration."""

    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )
    title: str = "VideoMind API"

    @classmethod
    def from_environment(cls) -> "ApiSettings":
        raw = os.environ.get("DOVIDEO_CORS_ORIGINS", "")
        if not raw.strip():
            return cls()
        origins = tuple(item.strip() for item in raw.split(",") if item.strip())
        return cls(cors_origins=origins or cls().cors_origins)


def create_app(
    services: Any | None = None,
    *,
    settings: ApiSettings | None = None,
    work_dir: Path | None = None,
) -> FastAPI:
    """Create one side-effect-free FastAPI app instance.

    ``services`` is injectable so contract tests can use the same application
    boundary with deterministic local adapters.  The default composition is
    process-local and clearly development-only.
    """

    selected_settings = settings or ApiSettings.from_environment()
    if services is not None:
        selected_services = services
    elif os.environ.get("DOVIDEO_PROFILE", "local").strip().casefold() == "production":
        # Explicit production selection is fail-fast. The factory below only
        # constructs SQLAlchemy/MySQL, Redis, MinIO, and Qdrant adapters; it
        # never silently substitutes LocalR1Services.
        from .r4_runtime import create_production_services

        selected_services = create_production_services()
    else:
        selected_services = LocalR1Services(work_dir=work_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            startup = getattr(selected_services, "startup", None)
            if callable(startup):
                await startup()
            yield
        finally:
            await selected_services.shutdown()

    app = FastAPI(title=selected_settings.title, version="0.1.0", lifespan=lifespan)
    app.state.services = selected_services
    app.state.settings = selected_settings
    allow_credentials = "*" not in selected_settings.cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(selected_settings.cors_origins),
        allow_credentials=allow_credentials,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "Idempotency-Key"],
    )

    @app.exception_handler(R1ServiceError)
    async def r1_error_handler(_request: Request, exc: R1ServiceError) -> JSONResponse:
        return _error(exc.message, exc.status_code, exc.code, headers=exc.headers)

    @app.exception_handler(InvalidMediaInput)
    async def input_error(_request: Request, exc: InvalidMediaInput) -> JSONResponse:
        status = 415 if isinstance(exc, UnsupportedVideoFormat) else 413 if isinstance(exc, MediaPayloadTooLarge) else 400
        message = {415: "不支持的视频格式，请选择 MP4、MOV、MKV、AVI、WEBM 或 M4V", 413: "上传超过大小限制"}.get(status, "上传参数无效")
        return _error(message, status, status)

    @app.exception_handler(MediaUnauthorized)
    async def owner_error(_request: Request, _exc: MediaUnauthorized) -> JSONResponse:
        return _error("无权访问该媒体或上传会话", 403, 403)

    @app.exception_handler(UploadNotFoundOrExpired)
    async def expired_upload(_request: Request, _exc: UploadNotFoundOrExpired) -> JSONResponse:
        return _error("上传会话不存在或已过期", 404, 404)

    @app.exception_handler(UploadConflict)
    async def conflict_upload(_request: Request, _exc: UploadConflict) -> JSONResponse:
        return _error("上传会话正在合并、已完成或分片不完整，请查询进度后重试", 409, 409)

    @app.exception_handler(MediaStorageFailure)
    @app.exception_handler(MediaRecordFailure)
    async def storage_error(_request: Request, _exc: Exception) -> JSONResponse:
        return _error("上传存储暂不可用，续传进度已保留", 503, 503)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        # Validation details may contain input values; the public API keeps a
        # stable, non-sensitive message instead of echoing them.
        return _error("请求参数无效", 400, 400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = {
            404: "资源不存在",
            405: "请求方法不支持",
        }.get(exc.status_code, "请求失败")
        return _error(message, exc.status_code, exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
        # Never expose stack traces, filesystem paths, provider payloads, or
        # configuration values through the HTTP error contract.
        return _error("服务暂不可用", 500, 500)

    def services_for(request: Request) -> Any:
        return request.app.state.services

    def require_user(request: Request) -> dict[str, Any]:
        return services_for(request).auth.require(request.headers.get("authorization"))

    def require_operator(request: Request) -> dict[str, Any]:
        user = require_user(request)
        role = str(user.get("role", "USER")).strip().upper()
        if role not in {"ADMIN", "OPERATOR"}:
            raise R1ServiceError("无权访问管理员任务接口", status_code=403)
        return user

    async def replay_access_call(
        operation: str,
        execution_id: str,
        principal: dict[str, Any],
    ) -> Any:
        access = getattr(selected_services, "replay_access", None)
        method = getattr(access, operation, None) if access is not None else None
        if not callable(method):
            raise R1ServiceError("历史回放接口未启用", status_code=503)
        try:
            value = method(execution_id, principal)
            if isawaitable(value):
                value = await value
            return value
        except ReplayAccessNotFoundError as exc:
            raise R1ServiceError("历史执行不存在", status_code=404, code=404) from exc
        except ReplayAccessForbiddenError as exc:
            raise R1ServiceError("无权访问该历史执行", status_code=403, code=403) from exc
        except ReplayAccessUnavailableError as exc:
            raise R1ServiceError("历史回放服务暂不可用", status_code=503, code=503) from exc
        except ReplayAccessError as exc:
            raise R1ServiceError("历史回放服务暂不可用", status_code=503, code=503) from exc
        except ReplayNotFoundError as exc:
            raise R1ServiceError("历史执行不存在", status_code=404, code=404) from exc
        except ReplayLegacyUnavailableError as exc:
            raise R1ServiceError("该执行不支持历史回放", status_code=410, code=410) from exc
        except ReplayIncompatibleVersionError as exc:
            raise R1ServiceError("历史执行契约不兼容", status_code=409, code=409) from exc
        except ReplayIncompleteError as exc:
            raise R1ServiceError("历史执行记录不完整", status_code=409, code=409) from exc
        except ReplayArtifactUnavailableError as exc:
            raise R1ServiceError("历史回放依赖的历史产物不可用", status_code=409, code=409) from exc
        except ReplayIntegrityError as exc:
            raise R1ServiceError("历史执行记录校验失败", status_code=409, code=409) from exc
        except ReplayPersistenceError as exc:
            raise R1ServiceError("历史回放服务暂不可用", status_code=503, code=503) from exc
        except HistoricalReplayError as exc:
            # Keep the public contract closed even if X2-C adds a new typed
            # failure in a future version.
            raise R1ServiceError("历史回放服务暂不可用", status_code=503, code=503) from exc

    async def enforce_ai_interaction(
        user: dict[str, Any],
        endpoint: str,
    ) -> None:
        limiter = getattr(selected_services, "ai_interaction_limiter", None)
        # Older injected contract-test compositions may not model the new
        # optional boundary. Production/local composition always installs it.
        if limiter is None:
            if hasattr(selected_services, "infrastructure"):
                raise R1ServiceError(
                    "AI 交互限流服务未配置",
                    status_code=503,
                )
            return
        try:
            decision = await limiter.try_acquire(int(user["id"]), endpoint)
        except AiInteractionLimiterUnavailable as exc:
            raise R1ServiceError(
                "AI 交互限流服务暂不可用",
                status_code=503,
            ) from exc
        except Exception as exc:
            raise R1ServiceError(
                "AI 交互限流服务暂不可用",
                status_code=503,
            ) from exc
        if bool(getattr(decision, "allowed", decision)):
            return
        retry_after = getattr(decision, "retry_after_seconds", None)
        headers = {} if retry_after is None else {"Retry-After": str(max(1, int(retry_after)))}
        raise R1ServiceError(
            "AI 交互请求过于频繁，请稍后再试",
            status_code=429,
            headers=headers,
        )

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return _ok({"service": "VideoMind", "api": "R1"})

    @app.get("/health")
    async def health() -> JSONResponse:
        return _ok("ok")

    # ----------------------------------------------- failed-task admin API
    @app.get("/admin/failed-tasks")
    async def list_failed_tasks(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=1_000_000),
        _operator: dict[str, Any] = Depends(require_operator),
    ) -> JSONResponse:
        operation = getattr(selected_services, "list_failed_tasks", None)
        if not callable(operation):
            raise R1ServiceError("管理员失败任务接口未启用", status_code=503)
        try:
            page = await operation(limit=limit, offset=offset)
        except FailedTaskAdminError as exc:
            raise R1ServiceError(str(exc), status_code=exc.status_code) from exc
        return _ok(
            {
                "items": [_failed_task_payload(item) for item in page.items],
                "total": page.total,
                "limit": page.limit,
                "offset": page.offset,
            }
        )

    @app.get("/admin/failed-tasks/{task_id}")
    async def inspect_failed_task(
        task_id: int,
        _operator: dict[str, Any] = Depends(require_operator),
    ) -> JSONResponse:
        operation = getattr(selected_services, "inspect_failed_task", None)
        if not callable(operation):
            raise R1ServiceError("管理员失败任务接口未启用", status_code=503)
        try:
            view = await operation(task_id)
        except FailedTaskAdminError as exc:
            raise R1ServiceError(str(exc), status_code=exc.status_code) from exc
        return _ok(_failed_task_payload(view, include_detail=True))

    @app.post("/admin/failed-tasks/{task_id}/replay")
    async def replay_failed_task(
        task_id: int,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        _operator: dict[str, Any] = Depends(require_operator),
    ) -> JSONResponse:
        operation = getattr(selected_services, "replay_failed_task", None)
        if not callable(operation):
            raise R1ServiceError("管理员失败任务接口未启用", status_code=503)
        if idempotency_key is None:
            raise R1ServiceError("必须提供 Idempotency-Key", status_code=400)
        try:
            result = await operation(task_id, idempotency_key)
        except FailedTaskAdminError as exc:
            raise R1ServiceError(str(exc), status_code=exc.status_code) from exc
        payload = _failed_task_payload(result.view, include_detail=True)
        return _ok(
            {
                "failedTask": payload,
                "replayAttemptId": (
                    result.view.replay_attempts[0].attempt_id
                    if result.view.replay_attempts
                    else None
                ),
                "status": result.view.replay_status,
            },
            "重放任务已受理" if result.accepted else "重放任务已完成",
            202 if result.accepted else 200,
        )

    # ------------------------------------------------------------------ auth
    @app.post("/user/register")
    async def register(payload: RegisterRequest, request: Request) -> JSONResponse:
        del request
        data = selected_services.auth.register(
            payload.username,
            payload.password,
            payload.nickname,
        )
        return _ok(data, "注册成功")

    @app.post("/user/login")
    async def login(payload: LoginRequest, request: Request) -> JSONResponse:
        del request
        data = selected_services.auth.login(payload.username, payload.password)
        return _ok(data, "登录成功")

    @app.post("/user/logout")
    async def logout(request: Request, _user: dict[str, Any] = Depends(require_user)) -> JSONResponse:
        selected_services.auth.logout(request.headers.get("authorization"))
        return _ok(None, "退出成功")

    @app.get("/user/me")
    async def current_user(user: dict[str, Any] = Depends(require_user)) -> JSONResponse:
        return _ok(user)

    # --------------------------------------------------------------- media API
    @app.post("/media/init-upload")
    async def init_upload(
        filename: str = Query(...),
        total_chunks: int = Query(..., alias="totalChunks"),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        normalize_video_filename(filename)
        upload_id = await selected_services.uploads.init(
            int(user["id"]), filename, total_chunks
        )
        return _ok(UploadInitData(uploadId=upload_id))

    @app.get("/media/upload-status")
    async def upload_status(
        upload_id: str = Query(..., alias="uploadId"),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        status = await selected_services.uploads.status(upload_id, int(user["id"]))
        return _ok(_upload_status_payload(status))

    @app.post("/media/upload-chunk")
    async def upload_chunk(
        upload_id: str = Form(..., alias="uploadId"),
        chunk_index: int = Form(..., alias="chunkIndex"),
        total_chunks: int = Form(..., alias="totalChunks"),
        file: UploadFile = File(...),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        payload = await _read_upload(file)
        status = await selected_services.uploads.put_chunk(
            upload_id,
            int(user["id"]),
            chunk_index,
            total_chunks,
            payload,
        )
        return _ok(_upload_status_payload(status))

    @app.post("/media/complete-upload")
    async def complete_upload(
        upload_id: str = Query(..., alias="uploadId"),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        record, _status = await selected_services.uploads.complete(
            upload_id, int(user["id"])
        )
        return _ok(media_summary(record), "上传完成")

    @app.post("/media/upload")
    async def upload(
        file: UploadFile = File(...),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        normalize_video_filename(file.filename or "upload.mp4")
        record = await selected_services.ingest(
            int(user["id"]),
            file.filename or "upload.mp4",
            await _read_upload(file),
            file.content_type,
        )
        return _ok(media_summary(record), "上传完成")

    @app.post("/media/upload-url")
    async def upload_url(
        url: str = Query(...),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        # R1 intentionally makes no remote downloader call.  Local file URLs
        # are accepted only when they resolve beneath the current workspace.
        from urllib.parse import unquote, urlsplit

        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"}:
            raise R1ServiceError(
                "R1 本地适配器不执行远程 URL 导入，请使用本地文件或分片上传",
                status_code=501,
            )
        if parsed.scheme != "file":
            raise R1ServiceError("仅支持受限的本地 file URL", status_code=400)
        candidate = Path(unquote(parsed.path))
        if os.name == "nt" and parsed.netloc:
            candidate = Path(f"{parsed.netloc}:{unquote(parsed.path)}")
        try:
            candidate = candidate.resolve()
            workspace = Path.cwd().resolve()
            if not candidate.is_relative_to(workspace):
                raise R1ServiceError("本地 URL 不在当前 workspace 内", status_code=403)
            payload = await _read_path(candidate)
        except FileNotFoundError as exc:
            raise R1ServiceError("本地文件不存在", status_code=404) from exc
        record = await selected_services.ingest(
            int(user["id"]), candidate.name, payload, None
        )
        return _ok(media_summary(record), "导入完成")

    @app.get("/media/list")
    async def media_list(user: dict[str, Any] = Depends(require_user)) -> JSONResponse:
        records = await selected_services.media.list_owned(int(user["id"]))
        return _ok([media_summary(record) for record in records])

    @app.get("/media/playback")
    async def playback(
        id: int = Query(...),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        return _ok(await selected_services.playback_url(id, int(user["id"])))

    @app.get("/media/playback-file/{media_id}", include_in_schema=False)
    async def playback_file(
        media_id: int,
        access_token: str = Query(...),
    ) -> FileResponse:
        path, content_type = await selected_services.playback_file(media_id, access_token)
        return FileResponse(path, media_type=content_type or "video/mp4")

    @app.delete("/media/delete")
    async def delete_media(
        id: int = Query(...),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        await selected_services.delete_media(id, int(user["id"]))
        return _ok(None, "删除成功")

    # ------------------------------------------------------------- analysis API
    @app.post("/analysis/route")
    async def route_analysis(
        payload: RouteRequest,
        _user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        normalized_goal = _goal(payload.goal, "分析目标")
        await enforce_ai_interaction(_user, "route")
        routed = selected_services.route(normalized_goal)
        if isawaitable(routed):
            routed = await routed
        mode, reason = routed
        return _ok(RouteDecision(mode=mode.value, reason=reason))

    @app.post("/analysis/ai")
    async def ai_analysis(
        id: int = Query(...),
        goal: str = Query("理解视频核心内容并生成结构化分析报告"),
        mode: str | None = Query(None),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        normalized_goal = _goal(goal, "分析目标")
        resolved_mode = _mode(mode)
        await selected_services.media.require_owned(id, int(user["id"]))
        key = selected_services.key(id, normalized_goal, resolved_mode)
        if await selected_services.checkpoint.load_result(key) is not None:
            return _ok(None, "已复用已完成结果")
        # Admission protects new work before dispatch and broker enqueue.
        await enforce_ai_interaction(user, "analysis")
        disposition = await selected_services.submit_analysis(
            id, int(user["id"]), normalized_goal, resolved_mode
        )
        return _submission(disposition)

    @app.post("/analysis/follow-up")
    async def follow_up(
        id: int = Query(...),
        question: str = Query(...),
        goal: str | None = Query(None),
        mode: str | None = Query(None),
        conversationId: str | None = Query(None, max_length=36),
        requestId: str | None = Query(None, max_length=36),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        question = _goal(question, "追问内容")
        selected_goal = None if goal is None else _goal(goal, "原始分析目标")
        resolved_mode = _mode(mode)
        await selected_services.media.require_owned(id, int(user["id"]))
        await enforce_ai_interaction(user, "follow-up")
        from dovideo.application.conversation_memory import canonical_uuid
        try:
            if conversationId is not None:
                conversationId = canonical_uuid(conversationId)
            if requestId is not None:
                requestId = canonical_uuid(requestId)
        except (ValueError, TypeError, AttributeError):
            raise R1ServiceError("会话或请求标识必须为 UUID", status_code=400) from None
        return _ok(
            await selected_services.follow_up(
                id, question, selected_goal, resolved_mode,
                **({"user_id": int(user["id"]), "conversation_id": conversationId,
                    "request_id": requestId} if conversationId is not None else {}),
            )
        )

    @app.get("/analysis/follow-up/history")
    async def follow_up_history(
        id: int = Query(...),
        conversationId: str = Query(..., max_length=36),
        goal: str | None = Query(None),
        mode: str | None = Query(None),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        from dovideo.application.conversation_memory import canonical_uuid
        selected_goal = None if goal is None else _goal(goal, "原始分析目标")
        resolved_mode = _mode(mode)
        await selected_services.media.require_owned(id, int(user["id"]))
        try:
            conversation_id = canonical_uuid(conversationId)
        except (ValueError, TypeError, AttributeError):
            raise R1ServiceError("会话标识必须为 UUID", status_code=400) from None
        return _ok(await selected_services.follow_up_history(
            id, int(user["id"]), selected_goal, resolved_mode, conversation_id))

    @app.get("/analysis/evidence-search")
    async def evidence_search(
        id: int = Query(...),
        query: str = Query(...),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        normalized_query = _goal(query, "检索问题")
        await selected_services.media.require_owned(id, int(user["id"]))
        await enforce_ai_interaction(user, "evidence-search")
        return _ok(list(await selected_services.evidence_search(id, normalized_query)))

    @app.get("/analysis/temporal-windows")
    async def temporal_windows(
        id: int = Query(...),
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        await selected_services.media.require_owned(id, int(user["id"]))
        context = await selected_services.checkpoint.load_context(id)
        return _ok(temporal_window_page(context, limit=limit, offset=offset))

    @app.get("/analysis/temporal-observations")
    async def temporal_observations(
        id: int = Query(...),
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        await selected_services.media.require_owned(id, int(user["id"]))
        context = await selected_services.checkpoint.load_context(id)
        return _ok(temporal_observation_page(context, limit=limit, offset=offset))

    @app.get("/analysis/agent-citations")
    async def agent_citations(
        id: int = Query(...),
        goal: str = Query(...),
        mode: str | None = Query(None),
        include_conclusions: bool = Query(False, alias="includeConclusions"),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        await selected_services.media.require_owned(id, int(user["id"]))
        key = selected_services.key(id, _goal(goal, "分析目标"), _mode(mode))
        context = await selected_services.checkpoint.load_context(id)
        state = await selected_services.checkpoint.load_result(key)
        if include_conclusions:
            return _ok(verified_answer_presentation(context, state))
        return _ok(verified_answer_citations(context, state))

    @app.post("/analysis/agent-feedback")
    async def agent_feedback(
        payload: AgentFeedback,
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        _validate_rating(payload)
        await selected_services.media.require_owned(payload.media_id, int(user["id"]))
        normalized = payload.normalized(mode=_mode(payload.mode))
        await selected_services.save_feedback(normalized)
        return _ok(None, "反馈已记录")

    @app.post("/analysis/agent-revise")
    async def agent_revise(
        payload: AgentFeedback,
        mode: str | None = Query(None),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        _validate_rating(payload)
        await selected_services.media.require_owned(payload.media_id, int(user["id"]))
        resolved_mode = _mode(mode if mode is not None else payload.mode)
        normalized = payload.normalized(mode=resolved_mode)
        await selected_services.save_feedback(normalized)
        disposition = await selected_services.revise_analysis(normalized, int(user["id"]))
        return _submission(disposition)

    @app.get("/analysis/agent-feedback")
    async def get_agent_feedback(
        id: int = Query(...),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        await selected_services.media.require_owned(id, int(user["id"]))
        return _ok(list(selected_services.feedback_for(id)))

    @app.get("/analysis/agent-plan")
    async def agent_plan(
        id: int = Query(...),
        goal: str = Query(...),
        mode: str | None = Query(None),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        await selected_services.media.require_owned(id, int(user["id"]))
        return _ok(await selected_services.plan(id, _goal(goal, "分析目标"), _mode(mode)))

    @app.get("/analysis/analysis-status")
    async def analysis_status(
        id: int = Query(...),
        goal: str = Query(...),
        mode: str | None = Query(None),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        await selected_services.media.require_owned(id, int(user["id"]))
        return _ok(await selected_services.status(id, _goal(goal, "分析目标"), _mode(mode)))

    @app.get("/analysis/analysis-events")
    async def analysis_events(
        id: int = Query(...),
        goal: str = Query(...),
        mode: str | None = Query(None),
        user: dict[str, Any] = Depends(require_user),
    ) -> StreamingResponse:
        await selected_services.media.require_owned(id, int(user["id"]))
        key = selected_services.key(id, _goal(goal, "分析目标"), _mode(mode))
        return _sse_response(selected_services, key)

    @app.get("/analysis/agent-evaluation")
    async def agent_evaluation(
        id: int = Query(...),
        goal: str = Query(...),
        mode: str | None = Query(None),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        await selected_services.media.require_owned(id, int(user["id"]))
        return _ok(await selected_services.evaluation(id, _goal(goal, "分析目标"), _mode(mode)))

    @app.get("/analysis/agent-trace")
    async def agent_trace(
        id: int = Query(...),
        goal: str = Query(...),
        mode: str | None = Query(None),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        await selected_services.media.require_owned(id, int(user["id"]))
        return _ok(selected_services.trace_snapshot(id, _goal(goal, "分析目标"), _mode(mode)))

    # ------------------------------------------------------ X2 historical replay
    @app.get("/analysis/executions/{execution_id}/replay")
    async def historical_replay(
        execution_id: str,
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        view = await replay_access_call("read", execution_id, user)
        return _ok(view.payload)

    @app.get("/analysis/executions/{execution_id}")
    async def historical_execution_metadata(
        execution_id: str,
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        metadata = await replay_access_call("metadata", execution_id, user)
        return _ok(metadata)

    @app.post("/admin/executions/{execution_id}/replay")
    async def initiate_historical_replay(
        execution_id: str,
        _operator: dict[str, Any] = Depends(require_operator),
    ) -> JSONResponse:
        view = await replay_access_call("initiate", execution_id, _operator)
        return _ok(view.payload, "历史回放完成")

    # ------------------------------------------------------- transcription API
    @app.post("/analysis/transcribe")
    async def transcribe(
        id: int = Query(...),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        await selected_services.start_transcription(id, int(user["id"]))
        return _ok(None, "文字提取任务已受理", 202)

    @app.get("/analysis/transcription-status")
    async def transcription_status(
        id: int = Query(...),
        user: dict[str, Any] = Depends(require_user),
    ) -> JSONResponse:
        return _ok(await selected_services.transcription_status(id, int(user["id"])))

    @app.get("/analysis/transcription-events")
    async def transcription_events(
        id: int = Query(...),
        user: dict[str, Any] = Depends(require_user),
    ) -> StreamingResponse:
        await selected_services.media.require_owned(id, int(user["id"]))
        key = TaskKey(id, "__transcription__", AnalysisMode.GENERAL)
        return _sse_response(selected_services, key)

    @app.get("/analysis/download")
    async def download(
        id: int = Query(...),
        user: dict[str, Any] = Depends(require_user),
    ) -> FileResponse:
        path, filename = await selected_services.download_path(id, int(user["id"]))
        # The R1 local adapter returns the owned local source as a bounded
        # binary download.  Audio extraction remains an infrastructure slice.
        return FileResponse(path, media_type="audio/mpeg", filename=filename)

    return app


def _ok(data: Any = None, message: str = "", status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": 0, "message": message, "data": _jsonable(data)},
    )


def _error(
    message: str,
    status_code: int,
    code: int | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": status_code if code is None else code, "message": message, "data": None},
        headers=headers,
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable(model_dump(mode="json", by_alias=True))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)


def _goal(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise R1ServiceError(f"{field}不能为空且不能超过 500 字", status_code=400)
    return value.strip()


def _failed_task_payload(view: Any, *, include_detail: bool = False) -> dict[str, Any]:
    """Project only bounded operator-safe metadata from a persisted failure."""

    record = view.record
    lifecycle = view.lifecycle
    mode = str(getattr(record, "mode", "")).strip().upper()
    if mode not in {"GENERAL", "LEARNING", "REVIEW", "CREATION"}:
        mode = "UNKNOWN"
    error_type = str(getattr(record, "error_type", "WorkerFailure"))
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,127}", error_type):
        error_type = "WorkerFailure"
    payload: dict[str, Any] = {
        "failedTaskId": int(record.task_id),
        "mediaId": int(record.media_id),
        "action": str(record.action)[:32],
        "mode": mode,
        "attemptCount": int(record.attempt_count),
        "failureStatus": str(record.status)[:32],
        "errorType": error_type,
        "errorMessage": "原始错误详情未通过管理接口公开",
        "createdAt": _admin_timestamp(record.created_at),
        "updatedAt": _admin_timestamp(record.updated_at),
        "replayStatus": str(view.replay_status),
        "replayAttemptCount": int(record.replay_attempt_count),
        "taskLifecycleState": (
            None if lifecycle is None or lifecycle.state is None else lifecycle.state.value
        ),
        "taskLifecycleStage": (
            None if lifecycle is None or lifecycle.stage is None else lifecycle.stage.value
        ),
    }
    if include_detail:
        payload["goal"] = str(record.user_goal)[:500]
        payload["replayEligible"] = view.replay_eligible
        payload["ineligibleReason"] = view.ineligible_reason
        payload["replayAttempts"] = [
            {
                "attemptId": item.attempt_id,
                "attemptNumber": int(item.attempt_number),
                "status": str(item.status),
                "errorType": item.error_type,
                "createdAt": _admin_timestamp(item.created_at),
                "updatedAt": _admin_timestamp(item.updated_at),
            }
            for item in view.replay_attempts[:20]
        ]
    return payload


def _admin_timestamp(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _mode(value: str | None) -> AnalysisMode:
    try:
        return AnalysisMode.from_request(value)
    except ValueError as exc:
        raise R1ServiceError(str(exc), status_code=400) from exc


def _submission(disposition: DispatchDisposition) -> JSONResponse:
    if disposition is DispatchDisposition.ACCEPTED:
        return _ok(None, "任务已受理", 202)
    if disposition is DispatchDisposition.DUPLICATE:
        raise R1ServiceError("相同视频和分析目标正在处理中", status_code=409)
    if disposition is DispatchDisposition.RATE_LIMITED:
        # Compatibility for legacy/custom submitters; API admission owns 429s.
        raise R1ServiceError("系统繁忙，请稍后再试", status_code=429)
    raise R1ServiceError("任务提交失败", status_code=500)


def _validate_rating(feedback: AgentFeedback) -> None:
    if feedback.rating not in (None, -1, 1):
        raise R1ServiceError("rating 只能是 -1 或 1", status_code=400)


def _upload_status_payload(status: Any) -> UploadStatusData:
    return UploadStatusData(
        uploadId=status.session.upload_id,
        filename=status.session.filename,
        totalChunks=status.session.total_chunks,
        uploadedChunks=status.uploaded_chunks,
        completedMediaId=status.completed_media_id,
    )


def _sse_response(services: Any, key: TaskKey) -> StreamingResponse:
    async def stream():
        async for event in services.subscribe(key):
            if event is None:
                yield ": keep-alive\n\n"
                continue
            payload = json.dumps(
                _jsonable(event.event),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield f"event: task-status\ndata: {payload}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _read_upload(upload: UploadFile) -> bytes:
    max_bytes = 2 * 1024 * 1024 * 1024
    result = bytearray()
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        result.extend(chunk)
        if len(result) > max_bytes:
            raise R1ServiceError("上传文件过大", status_code=413)
    return bytes(result)


async def _read_path(path: Path) -> bytes:
    if not path.is_file():
        raise FileNotFoundError(path)
    return await asyncio.to_thread(path.read_bytes)


__all__ = ["ApiSettings", "create_app"]
