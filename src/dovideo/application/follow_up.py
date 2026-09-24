"""Read-only grounded follow-up use case over durable video evidence."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from dovideo.domain import (
    AgentState,
    AnalysisEvidence,
    AnalysisMode,
    GroundedFollowUpAnswer,
    ModeProfile,
    VideoChunk,
    VideoContext,
    VideoEvidenceHit,
)

from .evidence import EvidenceVerificationService, normalize_evidence_text
from .mode_profiles import mode_profile_for
from .ports.checkpoint import ContextCheckpointPort
from .value_objects import TaskKey


MAX_FOLLOW_UP_CANDIDATES = 8
MAX_FOLLOW_UP_SOURCE_CHARS = 400
MAX_FOLLOW_UP_OCR_EXCERPTS = 2
MAX_FOLLOW_UP_PRIOR_ITEMS = 4
_LOGGER = logging.getLogger("dovideo.follow_up")
_ALLOWED_METRICS = {
    "context_segments",
    "chunk_count",
    "retrieval_candidates",
    "asr_candidates",
    "ocr_candidates",
    "latency_ms",
}


class FollowUpFailure(RuntimeError):
    """A bounded, content-free application failure for follow-up requests."""

    def __init__(self, category: str, safe_message: str) -> None:
        self.category = category
        self.safe_message = safe_message
        super().__init__(category)


class FollowUpModelFailure(RuntimeError):
    """Safe model-boundary category; the provider error remains only as cause."""

    _CATEGORIES = frozenset(
        {"timeout", "provider_failure", "invalid_response", "unexpected"}
    )

    def __init__(self, category: str) -> None:
        if category not in self._CATEGORIES:
            category = "unexpected"
        self.category = category
        super().__init__(category)


class FollowUpEvidenceSearchPort(Protocol):
    """The existing query-aware long-context evidence search boundary."""

    async def search_evidence(
        self,
        media_id: int | None,
        context: VideoContext,
        *,
        chunks: Sequence[VideoChunk] | None = None,
    ) -> tuple[VideoEvidenceHit, ...]:
        ...


class FollowUpModelPort(Protocol):
    """Produce one typed answer from bounded retrieved source candidates."""

    async def answer(
        self,
        question: str,
        *,
        original_goal: str,
        profile: ModeProfile,
        prior_analysis: Mapping[str, Any] | None,
        sources: Sequence[VideoEvidenceHit],
    ) -> GroundedFollowUpAnswer:
        ...


class FollowUpObserver(Protocol):
    """Value-free, bounded event sink for follow-up interactions."""

    def record_follow_up_event(
        self,
        event: str,
        *,
        media_id: int,
        mode: str,
        category: str | None = None,
        **metrics: int | float,
    ) -> None:
        ...


class GroundedFollowUpService:
    """Answer one same-video question without creating or mutating a task."""

    def __init__(
        self,
        checkpoint: ContextCheckpointPort,
        retrieval: FollowUpEvidenceSearchPort,
        model: FollowUpModelPort,
        *,
        verifier: EvidenceVerificationService | None = None,
        observer: FollowUpObserver | Any | None = None,
    ) -> None:
        self._checkpoint = checkpoint
        self._retrieval = retrieval
        self._model = model
        self._verifier = verifier or EvidenceVerificationService()
        self._observer = observer

    async def answer(
        self,
        media_id: int,
        question: str,
        original_goal: str | None,
        mode: AnalysisMode,
    ) -> str:
        """Return a formatted answer only after source-level verification."""

        self._validate_request(media_id, question, original_goal, mode)
        started = time.perf_counter()
        self._record("follow_up_requested", media_id=media_id, mode=mode)

        try:
            context = await self._checkpoint.load_context(media_id)
            chunks = await self._checkpoint.load_chunks(media_id)
        except Exception:
            self._record(
                "context_recovery_failed",
                media_id=media_id,
                mode=mode,
                category="checkpoint_error",
            )
            raise self._failure(
                media_id,
                mode,
                "checkpoint_failure",
                "视频分析上下文暂不可用，请稍后重试",
                started,
            ) from None

        if not isinstance(context, VideoContext) or not context.segments:
            self._record(
                "context_recovery_failed",
                media_id=media_id,
                mode=mode,
                category="context_not_ready",
            )
            raise self._failure(
                media_id,
                mode,
                "context_not_ready",
                "视频分析证据尚未准备完成",
                started,
                context_segments=0,
            )
        if not chunks:
            self._record(
                "context_recovery_failed",
                media_id=media_id,
                mode=mode,
                category="chunks_not_ready",
            )
            raise self._failure(
                media_id,
                mode,
                "context_not_ready",
                "视频分析证据尚未准备完成",
                started,
                context_segments=len(context.segments),
                chunk_count=0,
            )
        durable_chunks = tuple(chunks)
        selected_goal = (original_goal or context.user_goal or "")[:500]
        self._record(
            "context_recovery_succeeded",
            media_id=media_id,
            mode=mode,
            context_segments=len(context.segments),
            chunk_count=len(durable_chunks),
        )

        prior_analysis = await self._load_bounded_prior_analysis(
            media_id,
            selected_goal,
            mode,
        )
        retrieval_context = context.model_copy(update={"user_goal": question})
        retrieval_started = time.perf_counter()
        self._record("retrieval_reached", media_id=media_id, mode=mode)
        try:
            hits = tuple(
                await self._retrieval.search_evidence(
                    media_id,
                    retrieval_context,
                    chunks=durable_chunks,
                )
            )
        except Exception:
            raise self._failure(
                media_id,
                mode,
                "retrieval_failure",
                "视频证据检索暂不可用，请稍后重试",
                started,
                context_segments=len(context.segments),
                chunk_count=len(durable_chunks),
                latency_ms=_elapsed_ms(retrieval_started),
            ) from None

        candidates = self._prepare_candidates(context, hits)
        asr_count = sum(bool(item.prompt_hit.transcript.strip()) for item in candidates)
        ocr_count = sum(bool(item.prompt_hit.ocr_texts) for item in candidates)
        self._record(
            "retrieval_completed",
            media_id=media_id,
            mode=mode,
            context_segments=len(context.segments),
            chunk_count=len(durable_chunks),
            retrieval_candidates=len(candidates),
            asr_candidates=asr_count,
            ocr_candidates=ocr_count,
            latency_ms=_elapsed_ms(retrieval_started),
        )
        if not candidates:
            raise self._failure(
                media_id,
                mode,
                "no_evidence",
                "没有检索到可核验的视频证据，请尝试更具体的问题",
                started,
                retrieval_candidates=0,
                asr_candidates=0,
                ocr_candidates=0,
            )

        profile = mode_profile_for(mode)
        provider_started = time.perf_counter()
        self._record("provider_call_attempted", media_id=media_id, mode=mode)
        try:
            response = await self._model.answer(
                question,
                original_goal=selected_goal,
                profile=profile,
                prior_analysis=prior_analysis,
                sources=tuple(item.prompt_hit for item in candidates),
            )
        except FollowUpModelFailure as error:
            self._record(
                "provider_call_failed",
                media_id=media_id,
                mode=mode,
                category=error.category,
                latency_ms=_elapsed_ms(provider_started),
            )
            safe_message = (
                "追问响应暂不可用，请稍后重试"
                if error.category == "invalid_response"
                else "追问服务暂不可用，请稍后重试"
            )
            raise self._failure(
                media_id,
                mode,
                error.category,
                safe_message,
                started,
            ) from None
        except Exception:
            self._record(
                "provider_call_failed",
                media_id=media_id,
                mode=mode,
                category="unexpected",
                latency_ms=_elapsed_ms(provider_started),
            )
            raise self._failure(
                media_id,
                mode,
                "unexpected",
                "追问服务暂不可用，请稍后重试",
                started,
            ) from None

        self._record(
            "provider_call_succeeded",
            media_id=media_id,
            mode=mode,
            latency_ms=_elapsed_ms(provider_started),
        )
        if not isinstance(response, GroundedFollowUpAnswer):
            self._record(
                "evidence_verification_failed",
                media_id=media_id,
                mode=mode,
                category="invalid_response",
            )
            raise self._failure(
                media_id,
                mode,
                "invalid_response",
                "追问响应暂不可用，请稍后重试",
                started,
            ) from None

        verified = self._verify_response(context, candidates, response)
        if verified is None:
            self._record(
                "evidence_verification_failed",
                media_id=media_id,
                mode=mode,
                category="source_mismatch",
            )
            raise self._failure(
                media_id,
                mode,
                "evidence_rejected",
                "无法从已检索的视频证据中核验本次回答，请调整问题后重试",
                started,
                retrieval_candidates=len(candidates),
                asr_candidates=asr_count,
                ocr_candidates=ocr_count,
            ) from None

        self._record(
            "evidence_verification_succeeded",
            media_id=media_id,
            mode=mode,
            retrieval_candidates=len(candidates),
            asr_candidates=asr_count,
            ocr_candidates=ocr_count,
        )
        rendered = _render_answer(response.answer, verified)
        self._record(
            "follow_up_succeeded",
            media_id=media_id,
            mode=mode,
            category="completed",
            retrieval_candidates=len(candidates),
            asr_candidates=asr_count,
            ocr_candidates=ocr_count,
            latency_ms=_elapsed_ms(started),
        )
        return rendered

    async def _load_bounded_prior_analysis(
        self,
        media_id: int,
        goal: str,
        mode: AnalysisMode,
    ) -> Mapping[str, Any] | None:
        loader = getattr(self._checkpoint, "load_result", None)
        if not goal or not callable(loader):
            return None
        try:
            state = await loader(TaskKey(media_id, goal, mode))
        except Exception:
            # Prior results improve continuity only; source context remains
            # authoritative and a missing optional result does not fail P3.
            self._record(
                "prior_result_unavailable",
                media_id=media_id,
                mode=mode,
                category="checkpoint_error",
            )
            return None
        if (
            not isinstance(state, AgentState)
            or state.goal != goal
            or state.result is None
        ):
            return None
        result = state.result
        return {
            "title": _bounded_text(result.title, 160),
            "conclusions": tuple(
                _bounded_text(value, 220)
                for value in result.conclusions[:MAX_FOLLOW_UP_PRIOR_ITEMS]
            ),
            "suggestions": tuple(
                _bounded_text(value, 160)
                for value in result.suggestions[:2]
            ),
        }

    @staticmethod
    def _prepare_candidates(
        context: VideoContext,
        hits: Sequence[VideoEvidenceHit],
    ) -> tuple[_FollowUpCandidate, ...]:
        segments = {
            (segment.start_ms, segment.end_ms): segment
            for segment in context.segments
        }
        prepared: list[_FollowUpCandidate] = []
        for hit in hits[:MAX_FOLLOW_UP_CANDIDATES]:
            if not isinstance(hit, VideoEvidenceHit):
                continue
            segment = segments.get((hit.start_ms, hit.end_ms))
            if segment is None or hit.end_ms <= hit.start_ms:
                continue
            transcript = hit.transcript.strip()
            ocr_texts = tuple(
                value.strip()
                for value in hit.ocr_texts[:MAX_FOLLOW_UP_OCR_EXCERPTS]
                if isinstance(value, str) and value.strip()
            )
            if not transcript and not ocr_texts:
                continue
            prompt_hit = hit.model_copy(
                update={
                    "snippet": _bounded_text(hit.snippet, MAX_FOLLOW_UP_SOURCE_CHARS),
                    "transcript": _bounded_text(
                        transcript,
                        MAX_FOLLOW_UP_SOURCE_CHARS,
                    ),
                    "ocr_texts": tuple(
                        _bounded_text(value, MAX_FOLLOW_UP_SOURCE_CHARS)
                        for value in ocr_texts
                    ),
                }
            )
            prepared.append(_FollowUpCandidate(hit, prompt_hit))
        return tuple(prepared)

    def _verify_response(
        self,
        context: VideoContext,
        candidates: Sequence[_FollowUpCandidate],
        response: GroundedFollowUpAnswer,
    ) -> tuple[_VerifiedCitation, ...] | None:
        verified: list[_VerifiedCitation] = []
        for item in response.evidence:
            if item.candidate_index >= len(candidates):
                return None
            candidate = candidates[item.candidate_index]
            hit = candidate.prompt_hit
            if not (hit.start_ms <= item.timestamp_ms < hit.end_ms):
                return None
            if item.claim not in response.answer:
                return None

            source_text = _source_text_for(item.source, hit)
            quote = normalize_evidence_text(item.content)
            if not quote or quote not in normalize_evidence_text(source_text):
                return None

            evidence = AnalysisEvidence(
                timestamp_ms=item.timestamp_ms,
                source=item.source,
                content=item.content,
                claim=item.claim,
                source_revision=hit.source_revision,
                segment_id=hit.segment_id,
                source_item_ids=hit.source_item_ids,
            )
            if (
                not self._verifier.timestamp_covered(context, evidence)
                or not self._verifier.supported(context, evidence)
                or not self._verifier.supports_claim(
                    context,
                    item.claim,
                    evidence,
                )
            ):
                return None
            verified.append(
                _VerifiedCitation(
                    start_ms=candidate.source_hit.start_ms,
                    end_ms=candidate.source_hit.end_ms,
                    source=item.source,
                    claim=item.claim,
                    content=item.content,
                )
            )
        return tuple(verified) if verified else None

    def _validate_request(
        self,
        media_id: int,
        question: str,
        original_goal: str | None,
        mode: AnalysisMode,
    ) -> None:
        if isinstance(media_id, bool) or not isinstance(media_id, int) or media_id <= 0:
            raise FollowUpFailure("invalid_request", "追问请求无效")
        if not isinstance(question, str) or not question.strip() or len(question) > 500:
            raise FollowUpFailure("invalid_request", "追问内容不能为空且不能超过 500 字")
        if original_goal is not None and (
            not isinstance(original_goal, str)
            or not original_goal.strip()
            or len(original_goal) > 500
        ):
            raise FollowUpFailure("invalid_request", "原始分析目标无效")
        if not isinstance(mode, AnalysisMode):
            raise FollowUpFailure("invalid_mode", "分析模式无效")

    def _failure(
        self,
        media_id: int,
        mode: AnalysisMode,
        category: str,
        safe_message: str,
        started: float,
        **metrics: int | float,
    ) -> FollowUpFailure:
        metrics.setdefault("latency_ms", _elapsed_ms(started))
        self._record(
            "follow_up_failed",
            media_id=media_id,
            mode=mode,
            category=category,
            **metrics,
        )
        return FollowUpFailure(category, safe_message)

    def _record(
        self,
        event: str,
        *,
        media_id: int,
        mode: AnalysisMode,
        category: str | None = None,
        **metrics: int | float,
    ) -> None:
        bounded: dict[str, int | float] = {}
        for key, value in metrics.items():
            if key not in _ALLOWED_METRICS:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if isinstance(value, float):
                if not math.isfinite(value):
                    continue
                bounded[key] = round(max(0.0, min(value, 86_400_000.0)), 2)
            else:
                bounded[key] = max(0, min(value, 1_000_000))
        observer = self._observer
        try:
            if observer is not None:
                observer.record_follow_up_event(
                    event,
                    media_id=media_id,
                    mode=mode.value,
                    category=category,
                    **bounded,
                )
                return
        except Exception:
            return
        level = (
            logging.WARNING
            if event.endswith("failed") or event == "evidence_verification_failed"
            else logging.INFO
        )
        metric_text = " ".join(f"{key}={value}" for key, value in sorted(bounded.items()))
        _LOGGER.log(
            level,
            "follow_up event=%s media_id=%d mode=%s category=%s %s",
            event,
            media_id,
            mode.value,
            category or "-",
            metric_text,
        )


@dataclass(frozen=True, slots=True)
class _FollowUpCandidate:
    source_hit: VideoEvidenceHit
    prompt_hit: VideoEvidenceHit


@dataclass(frozen=True, slots=True)
class _VerifiedCitation:
    start_ms: int
    end_ms: int
    source: str
    claim: str
    content: str


def _source_text_for(source: str, hit: VideoEvidenceHit) -> str:
    if source == "ASR":
        return hit.transcript
    if source == "OCR":
        return " ".join(hit.ocr_texts)
    if source == "ASR+OCR":
        if not hit.transcript.strip() or not any(value.strip() for value in hit.ocr_texts):
            return ""
        return " ".join((hit.transcript, *hit.ocr_texts))
    return ""


def _render_answer(answer: str, citations: Sequence[_VerifiedCitation]) -> str:
    lines = [answer.strip(), "", "视频证据"]
    for item in citations:
        time_range = f"{_format_time(item.start_ms)}–{_format_time(item.end_ms)}"
        claim = _single_line(item.claim)
        content = _single_line(item.content)
        lines.append(
            f'- [{time_range}] {item.source}（支持：{claim}）：“{content}”'
        )
    return "\n".join(lines)


def _format_time(value_ms: int) -> str:
    total_seconds = max(0, value_ms) // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _single_line(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def _bounded_text(value: str, limit: int) -> str:
    return "".join(
        character
        for character in value[:limit]
        if character.isprintable() or character in "\n\t"
    ).strip()


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000.0)


__all__ = [
    "FollowUpFailure",
    "FollowUpModelFailure",
    "FollowUpModelPort",
    "FollowUpObserver",
    "GroundedFollowUpService",
    "MAX_FOLLOW_UP_CANDIDATES",
    "MAX_FOLLOW_UP_OCR_EXCERPTS",
    "MAX_FOLLOW_UP_SOURCE_CHARS",
]
