"""Bounded, read-only video tools for the X1 V1 tool allowlist.

The handlers in this module consume only the trusted ``VideoContext`` carried
by ``ToolExecutionContext``.  They never resolve a media ID, consult a
checkpoint, or accept model-supplied identity.  Retrieval is delegated to the
existing ``LongVideoContextService`` so this module remains a projection and
dispatch boundary rather than a second retrieval engine.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import Iterable
from typing import Any, Callable

from dovideo.domain import VideoContext, VideoEvidenceHit, VideoSegment

from .errors import DeadlineExceededError
from .adaptive_retrieval import baseline_retrieval
from .long_context import LongVideoContextService
from .ports.tools import ToolExecutionContext, ToolExecutorPort
from .tool_contracts import (
    GetContextWindowArguments,
    GetSegmentArguments,
    MAX_CONTEXT_WINDOW_MS as CONTRACT_MAX_CONTEXT_WINDOW_MS,
    MAX_QUERY_LENGTH,
    MAX_TOOL_RESULT_PAYLOAD_BYTES,
    SearchEvidenceArguments,
    ToolCall,
    ToolName,
    ToolResult,
    ToolResultStatus,
)


# The X1-A request bounds remain authoritative.  These smaller projection
# bounds make the payload ceiling a construction invariant rather than a
# last-minute JSON-size accident.
MAX_SEARCH_EVIDENCE_HITS = 8
MAX_TOOL_SEGMENTS = 8
MAX_TOOL_TEXT_CHARS = 512
MAX_TOOL_SOURCE_CHARS = 32
MAX_TOOL_OCR_ITEMS = 4
MAX_TOOL_OCR_TEXT_CHARS = 128
MAX_TOOL_RESULT_BYTES = MAX_TOOL_RESULT_PAYLOAD_BYTES


def _bounded_text(value: str, limit: int) -> tuple[str, bool]:
    if not isinstance(value, str):
        raise TypeError("video tool text must be a string")
    return value[:limit], len(value) > limit


def _bounded_texts(values: Iterable[str], limit: int, item_limit: int) -> tuple[tuple[str, ...], bool]:
    normalized = tuple(values)
    truncated = len(normalized) > item_limit
    projected: list[str] = []
    for value in normalized[:item_limit]:
        bounded, item_truncated = _bounded_text(value, limit)
        projected.append(bounded)
        truncated = truncated or item_truncated
    return tuple(projected), truncated


def _project_segment(segment: VideoSegment) -> tuple[dict[str, Any], bool]:
    transcript, transcript_truncated = _bounded_text(
        segment.transcript,
        MAX_TOOL_TEXT_CHARS,
    )
    ocr_texts, ocr_truncated = _bounded_texts(
        segment.ocr_texts,
        MAX_TOOL_OCR_TEXT_CHARS,
        MAX_TOOL_OCR_ITEMS,
    )
    projection: dict[str, Any] = {
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "transcript": transcript,
            "ocr_texts": list(ocr_texts),
        }
    _add_segment_provenance(projection, segment)
    return (
        projection,
        transcript_truncated or ocr_truncated,
    )


def _project_hit(hit: VideoEvidenceHit) -> tuple[dict[str, Any], bool]:
    source, source_truncated = _bounded_text(hit.source, MAX_TOOL_SOURCE_CHARS)
    snippet, snippet_truncated = _bounded_text(
        hit.snippet,
        MAX_TOOL_TEXT_CHARS,
    )
    transcript, transcript_truncated = _bounded_text(
        hit.transcript,
        MAX_TOOL_TEXT_CHARS,
    )
    ocr_texts, ocr_truncated = _bounded_texts(
        hit.ocr_texts,
        MAX_TOOL_OCR_TEXT_CHARS,
        MAX_TOOL_OCR_ITEMS,
    )
    projection: dict[str, Any] = {
            "start_ms": hit.start_ms,
            "end_ms": hit.end_ms,
            "source": source,
            "snippet": snippet,
            "transcript": transcript,
            "ocr_texts": list(ocr_texts),
        }
    if hit.source_revision:
        projection["source_revision"] = hit.source_revision
    if hit.chunk_id:
        projection["chunk_id"] = hit.chunk_id
    if hit.segment_id:
        projection["segment_id"] = hit.segment_id
    if hit.source_item_ids:
        projection["source_item_ids"] = list(hit.source_item_ids)
    return (
        projection,
        source_truncated
        or snippet_truncated
        or transcript_truncated
        or ocr_truncated,
    )


def _add_segment_provenance(
    projection: dict[str, Any],
    segment: VideoSegment,
) -> None:
    """Add only bounded identity metadata to new X1 result projections."""

    if segment.source_revision:
        projection["source_revision"] = segment.source_revision
    if segment.segment_id:
        projection["segment_id"] = segment.segment_id
    if segment.source_item_ids:
        projection["source_item_ids"] = list(segment.source_item_ids)


def _result(
    call: ToolCall,
    payload: dict[str, Any] | list[Any] | None,
    *,
    truncated: bool = False,
) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name.value,
        status=(
            ToolResultStatus.TRUNCATED
            if truncated
            else ToolResultStatus.SUCCESS
        ),
        payload=payload,
        truncated=truncated,
    )


def _failed(call: ToolCall) -> ToolResult:
    """Return a deliberately empty safe failure envelope."""

    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name.value,
        status=ToolResultStatus.FAILED,
    )


def _check_deadline(remaining_deadline: float | None) -> None:
    if remaining_deadline is None:
        return
    if isinstance(remaining_deadline, bool):
        raise ValueError("remaining_deadline must be a finite positive number")
    try:
        value = float(remaining_deadline)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("remaining_deadline must be a finite positive number") from error
    if not math.isfinite(value) or value <= 0:
        raise DeadlineExceededError("video tool execution deadline exhausted")


class VideoSegmentResolver:
    """Resolve all authoritative segments containing one timestamp."""

    @staticmethod
    def resolve(
        context: VideoContext,
        timestamp_ms: int,
    ) -> tuple[VideoSegment, ...]:
        if not isinstance(context, VideoContext):
            raise TypeError("context must be a VideoContext")
        if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
            raise TypeError("timestamp_ms must be an integer")

        matches = [
            (index, segment)
            for index, segment in enumerate(context.segments)
            if segment.start_ms <= timestamp_ms < segment.end_ms
        ]
        matches.sort(key=lambda item: (item[1].start_ms, item[1].end_ms, item[0]))
        return tuple(segment for _index, segment in matches)


class VideoContextWindowResolver:
    """Resolve a deterministic, half-open interval over current segments."""

    @staticmethod
    def bounds(
        context: VideoContext,
        timestamp_ms: int,
        before_ms: int,
        after_ms: int,
        *,
        media_duration_ms: int | None = None,
    ) -> tuple[int, int]:
        if not isinstance(context, VideoContext):
            raise TypeError("context must be a VideoContext")
        values = (timestamp_ms, before_ms, after_ms)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise TypeError("context window values must be integers")
        if timestamp_ms < 0:
            raise ValueError("context window timestamp cannot be negative")
        if before_ms < 0 or after_ms < 0:
            raise ValueError("context window durations cannot be negative")
        if before_ms + after_ms > CONTRACT_MAX_CONTEXT_WINDOW_MS:
            raise ValueError("context window exceeds the 60 second bound")

        duration = media_duration_ms
        if duration is None:
            duration = max((segment.end_ms for segment in context.segments), default=0)
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            raise ValueError("media duration must be a non-negative integer")
        requested_start = max(0, timestamp_ms - before_ms)
        requested_end = min(duration, timestamp_ms + after_ms)
        return requested_start, requested_end

    @classmethod
    def resolve(
        cls,
        context: VideoContext,
        timestamp_ms: int,
        before_ms: int,
        after_ms: int,
        *,
        media_duration_ms: int | None = None,
    ) -> tuple[VideoSegment, ...]:
        requested_start, requested_end = cls.bounds(
            context,
            timestamp_ms,
            before_ms,
            after_ms,
            media_duration_ms=media_duration_ms,
        )
        if requested_end <= requested_start:
            return ()
        matches = [
            (index, segment)
            for index, segment in enumerate(context.segments)
            if segment.start_ms < requested_end
            and segment.end_ms > requested_start
        ]
        matches.sort(key=lambda item: (item[1].start_ms, item[1].end_ms, item[0]))
        return tuple(segment for _index, segment in matches)


class VideoReadOnlyToolExecutor(ToolExecutorPort):
    """Static dispatcher for the three X1 V1 read-only video tools."""

    def __init__(
        self,
        search_service: LongVideoContextService | Any | None = None,
        *,
        long_context: LongVideoContextService | Any | None = None,
        long_context_service: LongVideoContextService | Any | None = None,
        search_evidence_service: LongVideoContextService | Any | None = None,
        segment_resolver: VideoSegmentResolver | None = None,
        window_resolver: VideoContextWindowResolver | None = None,
        max_result_bytes: int = MAX_TOOL_RESULT_BYTES,
    ) -> None:
        if isinstance(max_result_bytes, bool) or not isinstance(max_result_bytes, int):
            raise ValueError("max_result_bytes must be an integer")
        if not 1 <= max_result_bytes <= MAX_TOOL_RESULT_BYTES:
            raise ValueError("max_result_bytes is outside the X1 result bound")
        self._search_service = (
            search_service
            if search_service is not None
            else long_context_service
            if long_context_service is not None
            else long_context
            if long_context is not None
            else search_evidence_service
        )
        self._segment_resolver = segment_resolver or VideoSegmentResolver()
        self._window_resolver = window_resolver or VideoContextWindowResolver()
        self._max_result_bytes = max_result_bytes

    async def execute(
        self,
        tool_call: ToolCall,
        trusted_context: ToolExecutionContext,
        remaining_deadline: float | None = None,
    ) -> ToolResult:
        _check_deadline(remaining_deadline)
        self._validate_execution_context(trusted_context)
        if not isinstance(tool_call, ToolCall):
            raise TypeError("tool_call must be a validated ToolCall")

        handlers: dict[ToolName, Callable[..., Any]] = {
            ToolName.SEARCH_EVIDENCE: self._search_evidence,
            ToolName.GET_SEGMENT: self._get_segment,
            ToolName.GET_CONTEXT_WINDOW: self._get_context_window,
        }
        handler = handlers.get(tool_call.tool_name)
        if handler is None:
            raise ValueError("unsupported video tool")
        return self._bound_result(await handler(tool_call, trusted_context))

    def _bound_result(self, result: ToolResult) -> ToolResult:
        """Apply the configured production ceiling without exposing raw data."""

        if result.status in {ToolResultStatus.DENIED, ToolResultStatus.FAILED}:
            return result
        encoded = json.dumps(
            result.payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) <= self._max_result_bytes:
            return result
        return ToolResult(
            call_id=result.call_id,
            tool_name=result.tool_name,
            status=ToolResultStatus.TRUNCATED,
            payload=None,
            truncated=True,
        )

    async def _search_evidence(
        self,
        tool_call: ToolCall,
        trusted_context: ToolExecutionContext,
    ) -> ToolResult:
        arguments = tool_call.validated_arguments
        if not isinstance(arguments, SearchEvidenceArguments):
            raise TypeError("search_evidence arguments are invalid")
        if self._search_service is None:
            raise RuntimeError("search_evidence service is not configured")
        if arguments.limit < 1 or arguments.limit > MAX_SEARCH_EVIDENCE_HITS:
            raise ValueError("search_evidence limit is outside the V1 bound")

        query_context = trusted_context.video_context.model_copy(
            update={"user_goal": arguments.query}
        )
        try:
            search_method = self._search_service.search_evidence
            parameters = inspect.signature(search_method).parameters
            if "limit" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ):
                value = search_method(
                    trusted_context.media_id,
                    query_context,
                    limit=arguments.limit,
                )
            else:
                # Older injected fakes may expose the pre-X1-C two-argument
                # seam; projection still applies the validated limit below.
                value = search_method(
                    trusted_context.media_id,
                    query_context,
                )
            if inspect.isawaitable(value):
                with baseline_retrieval():
                    value = await value
        except asyncio.CancelledError:
            raise
        except DeadlineExceededError:
            raise
        except Exception:
            # Retrieval adapters own their detailed fallback behavior.  An
            # unexpected backend failure is safe data at this boundary, with
            # no traceback/provider/database text exposed to the model.
            return _failed(tool_call)

        hits = tuple(value)
        if not all(isinstance(hit, VideoEvidenceHit) for hit in hits):
            raise TypeError("search_evidence returned an invalid hit")
        selected = hits[: min(arguments.limit, MAX_SEARCH_EVIDENCE_HITS)]
        truncated = len(hits) > len(selected)
        projected: list[dict[str, Any]] = []
        for hit in selected:
            projection, item_truncated = _project_hit(hit)
            projected.append(projection)
            truncated = truncated or item_truncated
        payload = {
            "found": bool(projected),
            "query": arguments.query[:MAX_QUERY_LENGTH],
            "limit": arguments.limit,
            "count": len(projected),
            "hits": projected,
        }
        return _result(tool_call, payload, truncated=truncated)

    async def _get_segment(
        self,
        tool_call: ToolCall,
        trusted_context: ToolExecutionContext,
    ) -> ToolResult:
        arguments = tool_call.validated_arguments
        if not isinstance(arguments, GetSegmentArguments):
            raise TypeError("get_segment arguments are invalid")
        if arguments.timestamp_ms < 0:
            raise ValueError("get_segment timestamp cannot be negative")
        matches = self._segment_resolver.resolve(
            trusted_context.video_context,
            arguments.timestamp_ms,
        )
        selected = matches[:MAX_TOOL_SEGMENTS]
        truncated = len(matches) > len(selected)
        projected: list[dict[str, Any]] = []
        for segment in selected:
            projection, item_truncated = _project_segment(segment)
            projected.append(projection)
            truncated = truncated or item_truncated
        payload = {
            "found": bool(projected),
            "timestamp_ms": arguments.timestamp_ms,
            "segment": projected[0] if len(projected) == 1 else None,
            "segments": projected,
        }
        return _result(tool_call, payload, truncated=truncated)

    async def _get_context_window(
        self,
        tool_call: ToolCall,
        trusted_context: ToolExecutionContext,
    ) -> ToolResult:
        arguments = tool_call.validated_arguments
        if not isinstance(arguments, GetContextWindowArguments):
            raise TypeError("get_context_window arguments are invalid")
        if arguments.timestamp_ms < 0:
            raise ValueError("get_context_window timestamp cannot be negative")
        requested_start, requested_end = self._window_resolver.bounds(
            trusted_context.video_context,
            arguments.timestamp_ms,
            arguments.before_ms,
            arguments.after_ms,
            media_duration_ms=trusted_context.media_duration_ms,
        )
        matches = self._window_resolver.resolve(
            trusted_context.video_context,
            arguments.timestamp_ms,
            arguments.before_ms,
            arguments.after_ms,
            media_duration_ms=trusted_context.media_duration_ms,
        )
        selected = matches[:MAX_TOOL_SEGMENTS]
        truncated = len(matches) > len(selected)
        projected: list[dict[str, Any]] = []
        for segment in selected:
            projection, item_truncated = _project_segment(segment)
            projected.append(projection)
            truncated = truncated or item_truncated
        payload = {
            "found": bool(projected),
            "timestamp_ms": arguments.timestamp_ms,
            "requested_start_ms": requested_start,
            "requested_end_ms": requested_end,
            "count": len(projected),
            "segments": projected,
        }
        return _result(tool_call, payload, truncated=truncated)

    @staticmethod
    def _validate_execution_context(
        trusted_context: ToolExecutionContext,
    ) -> None:
        if not isinstance(trusted_context, ToolExecutionContext):
            raise TypeError("trusted_context must be a ToolExecutionContext")
        if trusted_context.task_key.media_id != trusted_context.media_id:
            raise ValueError("trusted tool context has mismatched media identity")
        if trusted_context.task_key.goal != trusted_context.video_context.user_goal:
            raise ValueError("trusted tool context has mismatched task goal")
        if trusted_context.media_duration_ms is not None:
            maximum_end = max(
                (segment.end_ms for segment in trusted_context.video_context.segments),
                default=0,
            )
            if trusted_context.media_duration_ms < maximum_end:
                raise ValueError("trusted tool context has invalid media duration")


# Descriptive aliases keep the concrete capability discoverable without
# creating another dispatcher implementation.
ReadOnlyVideoToolExecutor = VideoReadOnlyToolExecutor
SegmentResolver = VideoSegmentResolver
ContextWindowResolver = VideoContextWindowResolver


__all__ = [
    "CONTRACT_MAX_CONTEXT_WINDOW_MS",
    "ContextWindowResolver",
    "MAX_SEARCH_EVIDENCE_HITS",
    "MAX_TOOL_OCR_ITEMS",
    "MAX_TOOL_OCR_TEXT_CHARS",
    "MAX_TOOL_RESULT_BYTES",
    "MAX_TOOL_SEGMENTS",
    "MAX_TOOL_SOURCE_CHARS",
    "MAX_TOOL_TEXT_CHARS",
    "ReadOnlyVideoToolExecutor",
    "SegmentResolver",
    "VideoContextWindowResolver",
    "VideoReadOnlyToolExecutor",
    "VideoSegmentResolver",
]
