"""Read-only grounded follow-up use case over durable video evidence."""

from __future__ import annotations

import logging
import asyncio
import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from dovideo.domain import (
    AgentState,
    AnalysisEvidence,
    AnalysisMode,
    GroundedFollowUpAnswer,
    ModeProfile,
    VideoChunk,
    VideoContext,
    VideoEvidenceHit,
    TemporalObservation,
)

from .evidence import EvidenceVerificationService, normalize_evidence_text, _source_item_text
from .evidence_reflection import EvidenceReflection, ReflectionMode
from .evidence_projection import source_payload, bounded_source_hit, normalize_quote_text, MAX_FOLLOW_UP_SOURCE_CHARS, MAX_FOLLOW_UP_OCR_EXCERPTS
from .temporal_location import first_location_literals, first_literal_observations, literal_location_hit
from .mode_profiles import mode_profile_for
from .ports.checkpoint import ContextCheckpointPort
from .value_objects import TaskKey
from .execution_budget import AgentExecutionBudget
from .errors import BudgetExceededError, DeadlineExceededError
from .follow_up_budget import FollowUpBudgetSettings, FollowUpBudgetExceededError, follow_up_budget_scope
from .follow_up_outcome import video_outcome, direct_outcome, DirectFollowUpAnswer, KnowledgeBlock, MixedFollowUpAnswer, mixed_outcome, render_outcome
from .retrieval_need import (
    NeedInput, RetrievalNeed, RetrievalNeedMode, RetrievalNeedPolicy, RetrievalNeedSettings, observe_shadow,
)
from .retrieval_repair import FeedbackRetrievalMode, repair_evidence, validate_feedback_configuration
from .retrieval_budget import retrieval_budget_scope
from .conversation_memory import (
    ConversationMemoryService, ConversationIdentity, ConversationState,
    MemoryConflict, QueryRewrite, canonical_uuid, source_revision, needs_rewrite, question_digest,
)


MAX_FOLLOW_UP_CANDIDATES = 8
MAX_FOLLOW_UP_PRIOR_ITEMS = 4
MAX_FOLLOW_UP_PRIOR_SECTIONS = 2
_LOGGER = logging.getLogger("dovideo.follow_up")
_literal_location_capture = ContextVar("follow_up_literal_location_capture", default=None)


@contextmanager
def capture_literal_location():
    records = []
    token = _literal_location_capture.set(records)
    try:
        yield records
    finally:
        _literal_location_capture.reset(token)
_ALLOWED_METRICS = {
    "context_segments",
    "chunk_count",
    "retrieval_candidates",
    "asr_candidates",
    "ocr_candidates",
    "latency_ms",
    "candidate_source_refs",
    "verified_source_refs",
    "memory_turns",
    "summary_turns",
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
        observations: Sequence[TemporalObservation] = (),
        conversation_context: Mapping[str, Any] | None = None,
        fact_only: bool = False,
    ) -> GroundedFollowUpAnswer:
        ...


class DirectFollowUpModelPort(Protocol):
    async def answer(self, question: str) -> DirectFollowUpAnswer:
        ...


class MixedKnowledgeModelPort(Protocol):
    async def analyze(self, question: str, facts: tuple[str, ...]) -> tuple[KnowledgeBlock, ...]:
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
        max_wall_seconds: float = 60.0,
        memory: ConversationMemoryService | None = None,
        access_check: Any | None = None,
        retrieval_need_settings: RetrievalNeedSettings | None = None,
        retrieval_need_policy: RetrievalNeedPolicy | None = None,
        budget_settings: FollowUpBudgetSettings | None = None,
        direct_model: DirectFollowUpModelPort | None = None,
        mixed_knowledge_model: MixedKnowledgeModelPort | None = None,
        mixed_enabled: bool = False,
        reflection_model=None,
        reflection_mode: ReflectionMode = ReflectionMode.OFF,
        feedback_mode: FeedbackRetrievalMode = FeedbackRetrievalMode.OFF,
    ) -> None:
        self._reflection = EvidenceReflection(reflection_model, reflection_mode)
        self._checkpoint = checkpoint
        self._retrieval = retrieval
        self._model = model
        self._direct_model = direct_model
        if type(mixed_enabled) is not bool:
            raise ValueError("mixed flag must be boolean")
        self._mixed_enabled = mixed_enabled
        self._mixed_knowledge_model = mixed_knowledge_model
        self._verifier = verifier or EvidenceVerificationService()
        self._observer = observer
        self._memory = memory
        self._access_check = access_check
        self._retrieval_need_settings = retrieval_need_settings or RetrievalNeedSettings()
        self._retrieval_need_policy = retrieval_need_policy or RetrievalNeedPolicy()
        self._budget_settings = budget_settings or FollowUpBudgetSettings()
        validate_feedback_configuration(feedback_mode, reflection_mode, self._budget_settings,
            model_available=reflection_model is not None,
            metered_retrieval=getattr(retrieval, "supports_feedback_retrieval_budget", False))
        self._feedback_mode = feedback_mode
        if not math.isfinite(max_wall_seconds) or max_wall_seconds <= 0:
            raise ValueError("follow-up deadline must be finite and positive")
        self._max_wall_seconds = max_wall_seconds

    async def answer(
        self, media_id: int, question: str, original_goal: str | None, mode: AnalysisMode,
        *, user_id: int | None = None, conversation_id: str | None = None,
        request_id: str | None = None,
    ) -> str:
        self._validate_request(media_id, question, original_goal, mode)
        with AgentExecutionBudget.open(self._max_wall_seconds * 1000), follow_up_budget_scope(self._budget_settings), retrieval_budget_scope(
            self._feedback_mode != FeedbackRetrievalMode.OFF,
            enforced=self._feedback_mode == FeedbackRetrievalMode.ENABLED,
        ):
            try:
                async with asyncio.timeout(AgentExecutionBudget.remaining_seconds()):
                    if (self._retrieval_need_settings.mode == RetrievalNeedMode.ENABLED
                            and self._access_check is not None and user_id is not None):
                        await self._access_check(media_id, user_id)
                    if conversation_id is None:
                        result = await self._answer(media_id, question, original_goal, mode)
                        if (self._retrieval_need_settings.mode == RetrievalNeedMode.ENABLED
                                and self._access_check is not None and user_id is not None):
                            await self._access_check(media_id, user_id)
                        return result
                    return await self._with_memory(media_id, question, original_goal, mode,
                                                   user_id, conversation_id, request_id)
            except FollowUpBudgetExceededError:
                raise FollowUpFailure("budget_exceeded", "追问超过模型用量预算，请缩小问题后重试") from None
            except TimeoutError:
                raise FollowUpFailure("timeout", "追问超过执行时限，请缩小问题后重试") from None

    async def _with_memory(
        self, media_id: int, question: str, goal: str | None, mode: AnalysisMode,
        user_id: int | None, conversation_id: str, request_id: str | None,
    ) -> str:
        try:
            conversation_id = canonical_uuid(conversation_id)
            request_id = canonical_uuid(request_id) if request_id else str(uuid4())
            if user_id is None:
                raise ValueError("authenticated user required")
            context = await self._checkpoint.load_context(media_id)
            if not isinstance(context, VideoContext):
                raise FollowUpFailure("context_not_ready", "视频分析证据尚未准备完成")
            identity = ConversationIdentity.create(user_id, media_id, goal or context.user_goal,
                                                    mode, conversation_id, context)
        except (ValueError, TypeError):
            raise FollowUpFailure("invalid_request", "会话标识无效") from None
        except FollowUpFailure:
            raise
        except (BudgetExceededError, DeadlineExceededError):
            raise
        except Exception:
            raise FollowUpFailure("checkpoint_failure", "视频分析上下文暂不可用，请稍后重试") from None
        memory = self._memory
        if memory is None:
            raise FollowUpFailure("memory_unavailable", "当前运行模式尚未启用会话记忆")
        token = uuid4().hex
        acquired = False
        try:
            try:
                async with asyncio.timeout(2):
                    acquired = await memory.store.acquire(identity, token)
                    if not acquired:
                        raise MemoryConflict("conversation busy")
                    state = await memory.store.load(identity)
            except MemoryConflict:
                raise FollowUpFailure("conversation_conflict", "会话正在处理其他请求或已失效，请稍后重试") from None
            except (BudgetExceededError, DeadlineExceededError):
                raise
            except Exception:
                self._record("memory_read_failed", media_id=media_id, mode=mode, category="store_unavailable")
                # No history guess on an unavailable store. The stateless
                # path remains usable for self-contained questions.
                if needs_rewrite(question):
                    return "请明确你指的是哪个对象或方案；当前会话上下文暂不可用。"
                return await self._answer(media_id, question, goal, mode, allow_direct=False)
            for receipt in state.receipts:
                if receipt.request_id == request_id:
                    if receipt.question_digest != question_digest(question):
                        raise FollowUpFailure("conversation_conflict", "同一请求标识不能用于不同问题")
                    for turn in state.turns:
                        if turn.turn_id == request_id:
                            if self._access_check is not None:
                                await self._access_check(media_id, identity.user_id)
                            return turn.answer
                    raise FollowUpFailure("conversation_conflict", "该请求已经完成并压缩，请使用新的请求标识")
            self._record("memory_loaded", media_id=media_id, mode=mode,
                         memory_turns=min(6, len(state.turns)))
            return await self._answer(media_id, question, goal, mode,
                                      memory_session=(identity, state, token, request_id))
        finally:
            if acquired:
                try:
                    async with asyncio.timeout(2):
                        await memory.store.release(identity, token)
                except Exception:
                    self._record("memory_release_failed", media_id=media_id, mode=mode, category="store_unavailable")

    async def _answer(
        self,
        media_id: int,
        question: str,
        original_goal: str | None,
        mode: AnalysisMode,
        *, memory_session: tuple[ConversationIdentity, ConversationState, str, str] | None = None,
        allow_direct: bool = True,
    ) -> str:
        """Return a formatted answer only after source-level verification."""

        self._validate_request(media_id, question, original_goal, mode)
        started = time.perf_counter()
        self._record("follow_up_requested", media_id=media_id, mode=mode)

        context, chunks, selected_goal = await self._load_answer_context(
            media_id, mode, original_goal, memory_session, started)

        rewrite_required = needs_rewrite(question)
        prior_analysis = await self._load_bounded_prior_analysis(
            media_id,
            selected_goal,
            mode,
            rewrite_context=context if rewrite_required else None,
        )
        standalone_query = question
        conversation_context = None
        if memory_session:
            identity, state, token, request_id = memory_session
            conversation_context = state.prompt_context()
        if rewrite_required:
            rewrite_context = dict(conversation_context or {"rollingSummary": None, "recentTurns": []})
            if prior_analysis is not None:
                rewrite_context["priorAnalysisContext"] = prior_analysis
            if not rewrite_context["recentTurns"] and not rewrite_context["rollingSummary"] and prior_analysis is None:
                return "请明确你指的是哪个对象或方案，当前会话还没有足够的上下文。"
            try:
                if self._memory is None or self._memory.model is None:
                    raise ValueError("rewrite unavailable")
                self._record("query_rewrite_attempted", media_id=media_id, mode=mode)
                async with asyncio.timeout(8):
                    rewritten = await self._memory.model.rewrite(question, rewrite_context)
                rewritten = QueryRewrite.model_validate(rewritten.model_dump())
                if rewritten.needs_clarification:
                    return rewritten.clarification_question
                standalone_query = rewritten.standalone_query
            except (BudgetExceededError, DeadlineExceededError):
                raise
            except Exception:
                self._record("query_rewrite_failed", media_id=media_id, mode=mode, category="invalid_or_unavailable")
                return "请明确你指的是哪个对象或方案；上下文改写暂不可用。"
        mixed = False
        if self._retrieval_need_settings.mode != RetrievalNeedMode.OFF:
            need_input = NeedInput(
                original_question=question,
                standalone_query=standalone_query,
                was_rewritten=rewrite_required,
                has_discussion_context=bool(prior_analysis or (conversation_context and (
                    conversation_context["recentTurns"] or conversation_context["rollingSummary"]))),
            )
            decision = observe_shadow(need_input, self._retrieval_need_policy).decision
            if self._retrieval_need_settings.mode == RetrievalNeedMode.ENABLED:
                # Independently retain the R1 rule gate even for an injected policy.
                approved = RetrievalNeedPolicy().decide(need_input).need == RetrievalNeed.DIRECT_LLM
                if decision.need == RetrievalNeed.DIRECT_LLM and approved and allow_direct:
                    writer_ready = not memory_session or getattr(self._memory.store, "write_sources", False)
                    if writer_ready and self._direct_model is not None:
                        return await self._answer_direct(media_id, question, mode, context, memory_session)
                    self._record("direct_deferred", media_id=media_id, mode=mode,
                                 category="source_writer_or_adapter_unavailable")
                elif decision.need == RetrievalNeed.VIDEO_PLUS_KNOWLEDGE:
                    mixed_approved = RetrievalNeedPolicy().decide(need_input).need == RetrievalNeed.VIDEO_PLUS_KNOWLEDGE
                    writer_ready = not memory_session or getattr(self._memory.store, "write_sources", False)
                    mixed = (self._mixed_enabled and mixed_approved and allow_direct
                             and writer_ready and self._mixed_knowledge_model is not None)
                    if not mixed:
                        self._record("mixed_deferred", media_id=media_id, mode=mode,
                                     category="mixed_disabled_or_unavailable")
        literal_locations = first_location_literals(standalone_query)
        if literal_locations is not None and not mixed:
            return await self._answer_literal_locations(media_id, question, mode, context,
                memory_session, literal_locations)
        candidates, asr_count, ocr_count = await self._prepare_answer_evidence(
            media_id, mode, context, chunks, standalone_query, mixed, memory_session, started)
        return await self._generate_verified_answer(
            media_id, question, mode, context, candidates, selected_goal, prior_analysis,
            mixed, conversation_context, memory_session, started, asr_count, ocr_count)

    async def _load_answer_context(self, media_id, mode, original_goal, memory_session, started):
        """Recover authoritative context before any rewrite or retrieval."""
        try:
            context = await self._checkpoint.load_context(media_id)
            chunks = (await self._checkpoint.load_chunks(media_id)
                      if self._retrieval_need_settings.mode != RetrievalNeedMode.ENABLED else None)
        except (BudgetExceededError, TimeoutError):
            raise
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
        if self._retrieval_need_settings.mode != RetrievalNeedMode.ENABLED:
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
        if memory_session and source_revision(context) != memory_session[0].source_revision:
            raise FollowUpFailure("conversation_conflict", "视频来源版本已变化，请重新提问")
        selected_goal = (original_goal or context.user_goal or "")[:500]
        self._record(
            "context_recovery_succeeded",
            media_id=media_id,
            mode=mode,
            context_segments=len(context.segments),
            chunk_count=len(chunks or ()),
        )

        return context, chunks, selected_goal

    async def _prepare_answer_evidence(self, media_id, mode, context, chunks,
                                       standalone_query, mixed, memory_session, started):
        """Keep baseline, reflection and bounded repair in their original order."""
        if self._retrieval_need_settings.mode == RetrievalNeedMode.ENABLED:
            try:
                chunks = await self._checkpoint.load_chunks(media_id)
            except (BudgetExceededError, TimeoutError):
                raise
            except Exception:
                raise FollowUpFailure("checkpoint_failure", "视频分析上下文暂不可用，请稍后重试") from None
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
        retrieval_context = context.model_copy(update={"user_goal": standalone_query})
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
        except (BudgetExceededError, TimeoutError):
            raise
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

        candidates = self._prepare_candidates(context, hits, question=standalone_query)
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
        assessment = None
        downstream_attempts = getattr(getattr(getattr(self._reflection.model, "chat", None), "config", None), "max_attempts", 3)
        downstream_reserve = downstream_attempts * (30000 + (8000 if mixed else 0) + (12000 if memory_session else 0))
        if self._reflection.mode == ReflectionMode.SHADOW:
            projection = tuple(source_payload(i, item.prompt_hit, context.observations)
                               for i, item in enumerate(candidates))
            # Resolved M1 question describes the actual fact requirements; never
            # send history/prior analysis as evidence. No query mutation follows.
            reflection_question = standalone_query
            messages = None
            try:
                if self._reflection.model is not None:
                    messages = self._reflection.model.messages(reflection_question, projection, mixed=mixed)
            except (ValueError, TypeError):
                pass
            assessment = await self._reflection.observe(reflection_question, projection, mixed=mixed,
                messages=messages, answer_reserve=downstream_reserve,
                time_reserve=35 + (15 if mixed else 0), return_assessment=self._feedback_mode != FeedbackRetrievalMode.OFF)
        if self._feedback_mode != FeedbackRetrievalMode.OFF:
            merged_hits = await repair_evidence(self._feedback_mode, assessment, question=standalone_query,
                media_id=media_id, context=context, chunks=durable_chunks,
                initial=tuple(c.source_hit for c in candidates), retrieval=self._retrieval,
                mixed=mixed, downstream_reserve=downstream_reserve, planner_attempts=downstream_attempts)
            if self._feedback_mode == FeedbackRetrievalMode.ENABLED:
                candidates = self._prepare_candidates(context, merged_hits, question=standalone_query)
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

        return candidates, asr_count, ocr_count

    async def _generate_verified_answer(self, media_id, question, mode, context, candidates,
                                        selected_goal, prior_analysis, mixed, conversation_context,
                                        memory_session, started, asr_count, ocr_count):
        """Only verified outcomes reach rendering and the existing memory commit."""
        profile = mode_profile_for(mode)
        provider_started = time.perf_counter()
        self._record("provider_call_attempted", media_id=media_id, mode=mode)
        try:
            response = await self._model.answer(
                question,
                original_goal="" if mixed else selected_goal,
                profile=profile,
                prior_analysis=None if mixed else prior_analysis,
                sources=tuple(item.prompt_hit for item in candidates),
                observations=context.observations,
                **({"fact_only": True} if mixed else
                   ({"conversation_context": conversation_context} if memory_session else {})),
            )
        except (BudgetExceededError, DeadlineExceededError):
            raise
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
        except TimeoutError:
            raise
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

        try:
            response = GroundedFollowUpAnswer.model_validate(response.model_dump())
        except (ValueError, TypeError):
            raise self._failure(media_id, mode, "invalid_response", "追问响应暂不可用，请稍后重试", started) from None
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

        for item, citation in zip(response.evidence, verified, strict=True):
            self._record(
                "citation_verified",
                media_id=media_id,
                mode=mode,
                candidate_source_refs=len(candidates[item.candidate_index].source_hit.source_item_ids),
                verified_source_refs=len(citation.evidence.source_item_ids),
            )
        self._record(
            "evidence_verification_succeeded",
            media_id=media_id,
            mode=mode,
            retrieval_candidates=len(candidates),
            asr_candidates=asr_count,
            ocr_candidates=ocr_count,
            candidate_source_refs=sum(
                len(candidates[item.candidate_index].source_hit.source_item_ids)
                for item in response.evidence
            ),
            verified_source_refs=sum(len(item.evidence.source_item_ids) for item in verified),
        )
        rendered = _render_answer(response.answer, verified)
        outcome = video_outcome(response, rendered, tuple(item.evidence for item in verified), context, self._verifier)
        if mixed:
            # Stronger MIXED extraction boundary: all prose must be verified quotes.
            # This does not claim semantic sufficiency or whole-video coverage.
            facts = tuple(dict.fromkeys(item.claim for item in response.evidence))
            if (any(item.claim != item.content for item in response.evidence)
                    or response.answer != "\n".join(facts)):
                raise FollowUpFailure("evidence_rejected", "混合问答需要完整可核验的视频引文，请调整问题后重试")
            # video_outcome above has already validated source provenance.
            try:
                blocks = await self._mixed_knowledge_model.analyze(question, facts)
                proposal = MixedFollowUpAnswer(video=response, knowledge_blocks=blocks)
                outcome = mixed_outcome(proposal, rendered, tuple(item.evidence for item in verified), context, self._verifier)
            except (BudgetExceededError, DeadlineExceededError, TimeoutError):
                raise
            except FollowUpModelFailure as error:
                raise FollowUpFailure(error.category, "混合问答的知识分析暂不可用，请稍后重试") from None
            except (ValueError, TypeError):
                raise FollowUpFailure("invalid_response", "混合问答的知识分析格式无效，请稍后重试") from None
            except Exception:
                raise FollowUpFailure("unexpected", "混合问答暂不可用，请稍后重试") from None
            if not memory_session:
                latest = await self._checkpoint.load_context(media_id)
                if not isinstance(latest, VideoContext) or source_revision(latest) != source_revision(context):
                    raise FollowUpFailure("conversation_conflict", "视频来源版本已变化，请重新提问")
        rendered = render_outcome(outcome)
        rendered = await self._save_outcome(media_id, question, mode, outcome, memory_session)
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

    async def _answer_literal_locations(self, media_id, question, mode, context, memory_session, literals):
        matches = first_literal_observations(context, literals)
        if not matches:
            return "当前缓存没有可唯一核验的精确字面定位，无法确认全视频首次出现；请提供明确的字面目标或缩小问题范围。"
        candidates, evidence, claims = [], [], []
        for literal, observation in matches:
            item = observation.source_item
            source_hit, prompt_hit = literal_location_hit(context, literal, observation)
            quote = prompt_hit.transcript if item.source_type == "ASR" else prompt_hit.ocr_texts[0]
            # A spaced target may match the full text but fail bounded selection.
            if literal not in "".join(quote.split()):
                return "当前有界摘录不足以核验目标，无法确认全视频首次出现。"
            claim = f"字面目标 {literal} 在当前缓存可核验观察中的最早匹配为 {item.timestamp_ms}ms（{item.source_type}观察起点）。"
            evidence.append(dict(candidateIndex=len(candidates), timestampMs=item.timestamp_ms,
                source=item.source_type, content=quote, claim=claim))
            candidates.append(_FollowUpCandidate(source_hit, prompt_hit))
            claims.append(claim)
        response = GroundedFollowUpAnswer.model_validate(dict(
            answer="仅按当前视频缓存中忽略空白的精确字面匹配定位，不能确认全视频首次出现、语义等价形式或额外事件条件。\n" + "\n".join(claims),
            evidence=evidence))
        verified = self._verify_response(context, candidates, response)
        if verified is None:
            raise FollowUpFailure("evidence_rejected", "缓存定位的来源核验失败，请重新提问")
        latest = await self._checkpoint.load_context(media_id)
        if (not isinstance(latest, VideoContext) or latest.source != context.source
                or source_revision(latest) != source_revision(context)):
            raise FollowUpFailure("conversation_conflict", "视频来源版本已变化，请重新提问")
        rendered = _render_answer(response.answer, verified)
        outcome = video_outcome(response, rendered, tuple(v.evidence for v in verified), context, self._verifier)
        captured = _literal_location_capture.get()
        if captured is not None:
            captured.append(dict(scope="VALID_CACHED_LITERAL_OBSERVATIONS_ONLY", media_id=media_id,
                source_revision=context.source_revision,
                projection=[source_payload(i,c.prompt_hit,context.observations) for i,c in enumerate(candidates)],
                response=response.model_dump(mode="json"),
                verified_source_item_ids=[v.evidence.source_item_ids for v in verified]))
        result = await self._save_outcome(media_id, question, mode, outcome, memory_session)
        self._record("literal_location_succeeded", media_id=media_id, mode=mode,
            category="CACHED_EXACT_MATCH_ONLY", verified_source_refs=len(verified))
        return result

    async def _answer_direct(self, media_id, question, mode, context, memory_session):
        self._record("direct_provider_call_attempted", media_id=media_id, mode=mode)
        try:
            response = await self._direct_model.answer(question)
            if not isinstance(response, DirectFollowUpAnswer):
                raise FollowUpModelFailure("invalid_response")
            outcome = direct_outcome(response)
        except (BudgetExceededError, DeadlineExceededError, TimeoutError):
            raise
        except FollowUpModelFailure as error:
            raise FollowUpFailure(error.category, "通用知识回答暂不可用，请稍后重试") from None
        except (ValueError, TypeError):
            raise FollowUpFailure("invalid_response", "通用知识回答格式无效，请稍后重试") from None
        except Exception:
            raise FollowUpFailure("unexpected", "通用知识回答暂不可用，请稍后重试") from None
        # Stateless requests also reject extraction changes while generating.
        if not memory_session:
            latest = await self._checkpoint.load_context(media_id)
            if not isinstance(latest, VideoContext) or source_revision(latest) != source_revision(context):
                raise FollowUpFailure("conversation_conflict", "视频来源版本已变化，请重新提问")
        return await self._save_outcome(media_id, question, mode, outcome, memory_session)

    async def _save_outcome(self, media_id, question, mode, outcome, memory_session):
        rendered = render_outcome(outcome)
        if memory_session:
            identity, state, token, request_id = memory_session
            # Recheck durable ownership and version after the provider call;
            # deleted/re-extracted videos must never acquire successful turns.
            if self._access_check is not None:
                await self._access_check(media_id, identity.user_id)
            latest = await self._checkpoint.load_context(media_id)
            if not isinstance(latest, VideoContext) or source_revision(latest) != identity.source_revision:
                raise FollowUpFailure("conversation_conflict", "视频来源版本已变化，请重新提问")
            if outcome.answer_kind.value in {"MODEL_KNOWLEDGE", "MIXED"} and not getattr(self._memory.store, "write_sources", False):
                raise FollowUpFailure("memory_unavailable", "通用知识回答的来源保存暂不可用，请稍后重试")
            try:
                async def before_commit():
                    if self._access_check is not None:
                        await self._access_check(media_id, identity.user_id)
                    current = await self._checkpoint.load_context(media_id)
                    if not isinstance(current, VideoContext) or source_revision(current) != identity.source_revision:
                        raise FollowUpFailure("conversation_conflict", "视频来源版本已变化，请重新提问")

                remaining = AgentExecutionBudget.remaining_seconds()
                summarize = remaining is None or remaining > 12
                if len(state.turns) + 1 >= 10 and summarize:
                    self._record("summary_attempted", media_id=media_id, mode=mode, summary_turns=4)
                async with asyncio.timeout(10 if summarize else 2):
                    saved = await self._memory.save_verified(identity, state, token, request_id, question,
                        rendered, summarize=summarize, before_commit=before_commit,
                        answer_kind=(outcome.answer_kind.value if getattr(self._memory.store, "write_sources", False)
                                     else "LEGACY_UNKNOWN"))
                self._record("memory_saved", media_id=media_id, mode=mode, memory_turns=len(saved.turns))
                if len(state.turns) + 1 >= 10 and summarize:
                    self._record("summary_" + saved.summary_status, media_id=media_id, mode=mode)
            except FollowUpFailure:
                raise
            except (BudgetExceededError, DeadlineExceededError):
                raise
            except Exception:
                # Verified response remains useful on optional memory failure.
                self._record("memory_update_failed", media_id=media_id, mode=mode, category="store_or_summary_failure")
                rendered += "\n\n> 本轮对话暂未保存；下次提问请明确对象。"
        return rendered

    async def _load_bounded_prior_analysis(
        self,
        media_id: int,
        goal: str,
        mode: AnalysisMode,
        *, rewrite_context: VideoContext | None = None,
    ) -> Mapping[str, Any] | None:
        loader = getattr(self._checkpoint, "load_result", None)
        if not goal or not callable(loader):
            return None
        try:
            state = await loader(TaskKey(media_id, goal, mode))
        except (BudgetExceededError, DeadlineExceededError):
            raise
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
        if rewrite_context is not None:
            # AgentState has no result-level revision. Use its evidence only
            # to reject stale/unknown continuity, never as current candidates.
            # A legacy result cannot prove which extraction it belongs to;
            # recent turns can still resolve references in that case.
            if not rewrite_context.source_revision or not result.evidence:
                return None
            if any(item.source_revision != rewrite_context.source_revision for item in result.evidence):
                return None
            if any(not self._verifier.supported(rewrite_context, item) for item in result.evidence):
                return None
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
            # Keep displayed list order and disclose clipping. A bounded
            # prefix must not silently turn its fourth item into "the last".
            "listBounds": {
                "conclusions": _prior_list_bounds(result.conclusions, MAX_FOLLOW_UP_PRIOR_ITEMS, 220),
                "suggestions": _prior_list_bounds(result.suggestions, 2, 160),
                "sectionsTotal": len(result.sections),
            },
            "sections": tuple({
                "key": _bounded_text(section.key, 80),
                "title": _bounded_text(section.title, 80),
                "items": tuple(_bounded_text(value, 160) for value in section.items[:MAX_FOLLOW_UP_PRIOR_ITEMS]),
                "listBounds": _prior_list_bounds(section.items, MAX_FOLLOW_UP_PRIOR_ITEMS, 160),
            } for section in result.sections[:MAX_FOLLOW_UP_PRIOR_SECTIONS]),
        }

    @staticmethod
    def _prepare_candidates(
        context: VideoContext,
        hits: Sequence[VideoEvidenceHit],
        *, question: str | None = None,
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
            prompt_hit = bounded_source_hit(hit.model_copy(update={"transcript": transcript, "ocr_texts": ocr_texts}),
                context.user_goal if question is None else question)
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
            quote = normalize_quote_text(item.content)
            if not quote or quote not in normalize_quote_text(source_text):
                return None

            evidence = AnalysisEvidence(
                timestamp_ms=item.timestamp_ms,
                source=item.source,
                content=item.content,
                claim=item.claim,
                source_revision=hit.source_revision,
                segment_id=hit.segment_id,
            )
            if (
                hit.source_revision or hit.segment_id or hit.source_item_ids
                or context.source_revision
            ):
                source_item_ids = self._verifier.supporting_source_item_ids(
                    context, evidence, candidate.source_hit.source_item_ids,
                )
                if source_item_ids is None:
                    return None
                segment = next(s for s in context.segments if s.segment_id == hit.segment_id)
                original_texts = [_source_item_text(segment, source_item)
                    for source_item in segment.source_items if source_item.source_item_id in source_item_ids]
                if (not original_texts or any(text is None for text in original_texts)
                        or quote not in normalize_quote_text(" ".join(original_texts))):
                    return None
                evidence = AnalysisEvidence(
                    timestamp_ms=item.timestamp_ms,
                    source=item.source,
                    content=item.content,
                    claim=item.claim,
                    source_revision=hit.source_revision,
                    segment_id=hit.segment_id,
                    source_item_ids=source_item_ids,
                    source_provenance_version=context.provenance_version,
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
                    evidence=evidence,
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
    evidence: AnalysisEvidence


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


def _prior_list_bounds(items: Sequence[str], count: int, chars: int) -> dict[str, Any]:
    return {
        "totalItems": len(items),
        "clippedPositions": tuple(index + 1 for index, value in enumerate(items[:count]) if len(value) > chars),
    }


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
