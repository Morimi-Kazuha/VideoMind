"""Bounded conversation context; never a video evidence source."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from dovideo.domain import AnalysisMode, VideoContext
from .analysis_task_keys import goal_digest

RECENT_TURNS = 6
SUMMARY_THRESHOLD = 10
MAX_PENDING_TURNS = 20
MEMORY_TTL_SECONDS = 7 * 24 * 60 * 60
# Six complete bounded turns (500 + 12,000 each), summary and JSON overhead.
CONTEXT_CHARS = 80_000
SUMMARY_CHARS = 1600
TextItem = Annotated[str, Field(min_length=1, max_length=160)]


class MemoryDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RollingSummary(MemoryDTO):
    topics: Annotated[list[TextItem], Field(max_length=6)]
    entities: Annotated[list[TextItem], Field(max_length=8)]
    key_points: Annotated[list[TextItem], Field(max_length=8)]
    unresolved_questions: Annotated[list[TextItem], Field(max_length=4)]
    summary_text: Annotated[str, Field(max_length=800)]

    @model_validator(mode="after")
    def bounded(self):
        size = sum(len(x) for field in (self.topics, self.entities, self.key_points,
                                      self.unresolved_questions) for x in field)
        if size + len(self.summary_text) > SUMMARY_CHARS:
            raise ValueError("summary too large")
        return self


class QueryRewrite(MemoryDTO):
    standalone_query: Annotated[str, Field(max_length=500)]
    needs_clarification: StrictBool
    clarification_question: Annotated[str, Field(max_length=200)]

    @model_validator(mode="after")
    def coherent(self):
        if self.needs_clarification:
            if not self.clarification_question.strip() or self.standalone_query:
                raise ValueError("invalid clarification")
        elif not self.standalone_query.strip() or self.clarification_question:
            raise ValueError("invalid standalone query")
        return self


class ConversationTurn(MemoryDTO):
    turn_id: Annotated[str, Field(min_length=1, max_length=36)]
    question: Annotated[str, Field(min_length=1, max_length=500)]
    answer: Annotated[str, Field(min_length=1, max_length=12_000)]
    created_at: Annotated[str, Field(max_length=40)]
    source_revision: Annotated[str, Field(min_length=1, max_length=128)]


class RequestReceipt(MemoryDTO):
    request_id: Annotated[str, Field(max_length=36)]
    question_digest: Annotated[str, Field(min_length=64, max_length=64)]


class ConversationState(MemoryDTO):
    version: Annotated[StrictInt, Field(ge=0)] = 0
    turns: Annotated[list[ConversationTurn], Field(max_length=MAX_PENDING_TURNS)] = Field(default_factory=list)
    summary: RollingSummary | None = None
    receipts: Annotated[list[RequestReceipt], Field(max_length=64)] = Field(default_factory=list)
    summary_status: Literal["idle", "completed", "failed", "unavailable", "capacity", "deferred"] = "idle"

    def prompt_context(self) -> dict:
        # Recent six are complete Q/A, including verified Markdown citations.
        # The bound follows from per-turn limits; do not silently clip history.
        result = {
            "rollingSummary": self.summary.model_dump() if self.summary else None,
            "recentTurns": [{"question": t.question, "answer": t.answer}
                            for t in self.turns[-RECENT_TURNS:]],
        }
        if len(json.dumps(result, ensure_ascii=False)) > CONTEXT_CHARS:
            raise ValueError("conversation context too large")
        return result


def source_revision(context: VideoContext) -> str:
    if context.source_revision:
        return context.source_revision
    # Legacy checkpoints have no revision; derive a content-bound version,
    # excluding user_goal so changing a retrieval query cannot change it.
    payload = {"source": context.source, "segments": [s.model_dump(mode="json") for s in context.segments]}
    return "legacy-" + hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def canonical_uuid(value: str) -> str:
    return str(UUID(value))


@dataclass(frozen=True, slots=True)
class ConversationIdentity:
    user_id: int
    media_id: int
    goal_digest: str
    analysis_mode: str
    conversation_id: str
    source_revision: str

    @classmethod
    def create(cls, user_id: int, media_id: int, goal: str, mode: AnalysisMode,
               conversation_id: str, context: VideoContext):
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("authenticated user required")
        return cls(user_id, media_id, goal_digest(goal or "当前视频", mode), mode.value,
                   canonical_uuid(conversation_id), source_revision(context))


class MemoryConflict(RuntimeError):
    """An active request, stale version, deleted media or changed request."""


class ConversationMemoryPort(Protocol):
    async def load(self, identity: ConversationIdentity) -> ConversationState: ...
    async def acquire(self, identity: ConversationIdentity, token: str) -> bool: ...
    async def release(self, identity: ConversationIdentity, token: str) -> None: ...
    async def commit(self, identity: ConversationIdentity, expected_version: int,
                     state: ConversationState, token: str) -> bool: ...
    async def delete_media(self, media_id: int) -> None: ...


class ConversationModelPort(Protocol):
    async def rewrite(self, question: str, context: dict) -> QueryRewrite: ...
    async def summarize(self, previous: RollingSummary | None,
                        turns: list[ConversationTurn]) -> RollingSummary: ...


def needs_rewrite(question: str) -> bool:
    return bool(re.search(r"它|他们|它们|这(?:个|种|些|样)|那(?:个|种|些|样)|刚才|之前|上述|前者|后者|第[一二三四五六七八九十\d]+(?:个|种)|^那|\b(it|they|that|those|second|former|latter)\b", question, re.I))


def question_digest(question: str) -> str:
    return hashlib.sha256(question.encode()).hexdigest()


def successful_turn(request_id: str, question: str, answer: str, revision: str) -> ConversationTurn:
    return ConversationTurn(turn_id=request_id, question=question, answer=answer,
                            created_at=datetime.now(timezone.utc).isoformat(), source_revision=revision)


class ConversationMemoryService:
    """Prepare bounded context, then atomically append/compact with one CAS."""

    def __init__(self, store: ConversationMemoryPort, model: ConversationModelPort | None = None):
        self.store = store
        self.model = model

    async def save_verified(self, identity: ConversationIdentity, state: ConversationState,
                            token: str, request_id: str, question: str, answer: str,
                            *, summarize: bool = False, before_commit=None) -> ConversationState:
        expected_version = state.version
        summary_prepared = False
        if len(state.turns) >= MAX_PENDING_TURNS:
            if summarize:
                state = await self._prepare_summary(state)
                summary_prepared = True
        if len(state.turns) >= MAX_PENDING_TURNS:
            # Backpressure preserves unsummarized turns rather than silently
            # dropping them after repeated summary failures.
            full = state.model_copy(update={"version": expected_version + 1, "summary_status": "capacity"})
            if before_commit is not None:
                await before_commit()
            await self.store.commit(identity, expected_version, full, token)
            raise MemoryConflict("memory capacity reached")
        updated = state.model_copy(update={
            "version": state.version + 1,
            "turns": [*state.turns, successful_turn(request_id, question, answer, identity.source_revision)],
            "receipts": [*state.receipts, RequestReceipt(request_id=request_id,
                           question_digest=question_digest(question))][-64:],
        })
        if summarize and not summary_prepared:
            updated = await self._prepare_summary(updated)
        elif not summarize and len(updated.turns) >= SUMMARY_THRESHOLD:
            updated = updated.model_copy(update={"summary_status": "deferred"})
        if before_commit is not None:
            await before_commit()
        if not await self.store.commit(identity, expected_version, updated, token):
            raise MemoryConflict("memory append conflict")
        return updated

    async def compress(self, identity: ConversationIdentity, state: ConversationState,
                       token: str) -> ConversationState:
        if len(state.turns) < SUMMARY_THRESHOLD:
            return state
        prepared = await self._prepare_summary(state)
        updated = prepared.model_copy(update={"version": state.version + 1})
        if not await self.store.commit(identity, state.version, updated, token):
            raise MemoryConflict("summary commit conflict")
        return updated

    async def _prepare_summary(self, state: ConversationState) -> ConversationState:
        if len(state.turns) < SUMMARY_THRESHOLD:
            return state
        status = "unavailable" if self.model is None else "failed"
        summary = state.summary
        turns = state.turns
        if self.model is not None:
            try:
                import asyncio
                # Optional work has its own shorter timeout; a valid answer
                # already exists even if compression fails or times out.
                async with asyncio.timeout(8):
                    candidate = await self.model.summarize(summary, turns[:4])
                summary = RollingSummary.model_validate(candidate.model_dump())
                turns = turns[4:]
                status = "completed"
            except Exception:
                pass  # only status is persisted; never log private content
        return state.model_copy(update={"summary": summary, "turns": turns, "summary_status": status})
