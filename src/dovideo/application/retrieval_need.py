"""Conservative retrieval-need rules and content-free request-local shadow events.

This policy never executes a route. Identity, permissions and budgets remain
with the caller. Discussion context is represented only by trusted booleans;
neither history nor video text is needed for this first rule-only version.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError, field_validator, model_validator

from .conversation_memory import MemoryConflict, needs_rewrite
from .errors import BudgetExceededError, CheckpointPayloadCompatibilityError, DeadlineExceededError

POLICY_VERSION = "r1-rules-v1"


class RetrievalNeed(str, Enum):
    DIRECT_LLM = "DIRECT_LLM"
    VIDEO_RAG = "VIDEO_RAG"
    VIDEO_PLUS_KNOWLEDGE = "VIDEO_PLUS_KNOWLEDGE"


class RetrievalNeedMode(str, Enum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ENABLED = "ENABLED"


class NeedDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Question = Annotated[StrictStr, Field(min_length=1, max_length=500)]
ReasonCode = Literal[
    "EXPLICIT_GENERAL_KNOWLEDGE", "EXPLICIT_VIDEO_REFERENCE",
    "VIDEO_DEPENDENT_CONTEXT", "VIDEO_KNOWLEDGE_COMPARISON",
    "AMBIGUOUS_CONTEXT", "INVALID_INPUT", "SAFE_FALLBACK",
]


class NeedInput(NeedDTO):
    original_question: Question
    standalone_query: Question
    # Set by the application after successful M1 rewrite, never from request
    # parameters or rewrite-provider-supplied metadata. Old history has unknown
    # provenance; resolved references still veto DIRECT conservatively.
    was_rewritten: StrictBool = False
    has_discussion_context: StrictBool = False

    @field_validator("original_question", "standalone_query")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("blank question")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if not self.was_rewritten and self.original_question != self.standalone_query:
            raise ValueError("query changed without rewrite")
        return self


class NeedDecision(NeedDTO):
    need: RetrievalNeed
    reason_code: ReasonCode
    decision_source: Literal["RULE", "FALLBACK"]
    policy_version: Literal["r1-rules-v1"] = POLICY_VERSION

    @model_validator(mode="after")
    def coherent(self):
        expected = {
            "EXPLICIT_GENERAL_KNOWLEDGE": (RetrievalNeed.DIRECT_LLM, "RULE"),
            "EXPLICIT_VIDEO_REFERENCE": (RetrievalNeed.VIDEO_RAG, "RULE"),
            "VIDEO_DEPENDENT_CONTEXT": (RetrievalNeed.VIDEO_RAG, "RULE"),
            "VIDEO_KNOWLEDGE_COMPARISON": (RetrievalNeed.VIDEO_PLUS_KNOWLEDGE, "RULE"),
        }.get(self.reason_code, (RetrievalNeed.VIDEO_RAG, "FALLBACK"))
        if (self.need, self.decision_source) != expected:
            raise ValueError("inconsistent need decision")
        return self


@dataclass(frozen=True)
class RetrievalNeedSettings:
    mode: RetrievalNeedMode = RetrievalNeedMode.OFF

    def __post_init__(self):
        if not isinstance(self.mode, RetrievalNeedMode):
            raise ValueError("invalid retrieval need mode")


# Deliberately small gates. DIRECT requires a single self-contained explicit
# general-knowledge instruction, not just a topic like Redis or a comparison.
_GENERAL = re.compile(
    r"^(?:不考虑(?:当前)?视频|脱离(?:当前)?视频|从一般(?:计算机)?知识角度|仅从通用知识角度)"
    r"[，,：:\s]*(?:请)?(?:解释|介绍|说明)(?P<zh>[^。！？!?\n；;]+)[。？?]?$"
    r"|^(?:independent of (?:the |this |current )?video|from general (?:computer )?knowledge)"
    r"[, :]*(?:please )?(?:explain|describe) (?P<en>[^.!?\n;]+)[.?]?$", re.I,
)
_VIDEO = re.compile(
    r"视频|作者|讲者|讲师|课程|第\s*[几一二三四五六七八九十\d]+\s*(?:分钟|章|节)|\d{1,2}:\d{2}"
    r"|\b(?:video|author|speaker|lecturer|lecture)\b", re.I,
)
_EVALUATION = re.compile(
    r"正确吗|是否正确|是否合理|合理吗|准确吗|靠谱吗|技术问题|技术取舍|技术缺陷"
    r"|(?:标准(?:做法|知识)|通用知识|行业实践).*(?:相比|对照|比较|不足|一致)"
    r"|(?:相比|对照|比较).*(?:标准(?:做法|知识)|通用知识|行业实践)"
    r"|\b(?:is .+ correct|technically correct|technical (?:issues|tradeoffs)|standard practice)\b", re.I,
)


def _text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def safe_fallback(reason: ReasonCode = "SAFE_FALLBACK") -> NeedDecision:
    return NeedDecision(need=RetrievalNeed.VIDEO_RAG, reason_code=reason, decision_source="FALLBACK")


class RetrievalNeedPolicy:
    def decide(self, value: NeedInput) -> NeedDecision:
        try:
            # Typed copies/constructs can bypass validation; always revalidate.
            value = NeedInput.model_validate(value.model_dump(warnings=False) if isinstance(value, NeedInput) else value)
        except (ValidationError, ValueError, TypeError):
            return safe_fallback("INVALID_INPUT")
        original, standalone = _text(value.original_question), _text(value.standalone_query)
        general = _GENERAL.fullmatch(original)
        topic = (general.group("zh") or general.group("en")) if general else ""
        # The exclusion phrase itself contains 'video'. Only remove that exact
        # grammar when checking independent questions; all remaining video or
        # dependent references (including adversarial second instructions) veto.
        explicit_video = bool(_VIDEO.search(topic if general else original) or
                              (value.was_rewritten and _VIDEO.search(standalone)))
        referent_text = topic if general else original
        dependent = value.was_rewritten or needs_rewrite(referent_text) or bool(
            re.search(r"这里|这段|那段|前文|\b(?:here|above|previous)\b", referent_text, re.I))
        linked = explicit_video or dependent
        if linked and _EVALUATION.search(original):
            return NeedDecision(need=RetrievalNeed.VIDEO_PLUS_KNOWLEDGE,
                                reason_code="VIDEO_KNOWLEDGE_COMPARISON", decision_source="RULE")
        if explicit_video:
            return NeedDecision(need=RetrievalNeed.VIDEO_RAG,
                                reason_code="EXPLICIT_VIDEO_REFERENCE", decision_source="RULE")
        if dependent:
            return NeedDecision(need=RetrievalNeed.VIDEO_RAG,
                                reason_code="VIDEO_DEPENDENT_CONTEXT", decision_source="RULE")
        if general:
            return NeedDecision(need=RetrievalNeed.DIRECT_LLM,
                                reason_code="EXPLICIT_GENERAL_KNOWLEDGE", decision_source="RULE")
        return safe_fallback("AMBIGUOUS_CONTEXT")


@dataclass(frozen=True)
class NeedObservation:
    decision: NeedDecision
    decision_latency_ms: float
    routing_count: int = 1

    @property
    def safe_fallback_count(self) -> int:
        return int(self.decision.decision_source == "FALLBACK")


_observer: ContextVar[object | None] = ContextVar("retrieval_need_observer", default=None)
_logger = logging.getLogger("dovideo.retrieval_need")


@contextmanager
def capture_retrieval_need():
    """Explicit request-local capture; no global mutable last_decision."""
    observations = []
    token = _observer.set(observations.append)
    try:
        yield observations
    finally:
        _observer.reset(token)


def observe_shadow(value: NeedInput, policy: RetrievalNeedPolicy) -> NeedObservation:
    started = time.perf_counter()
    try:
        decision = policy.decide(value)
        decision = NeedDecision.model_validate(decision.model_dump())
    except (BudgetExceededError, DeadlineExceededError, CheckpointPayloadCompatibilityError,
            MemoryConflict, PermissionError):
        raise
    except (ValueError, TypeError, ArithmeticError, AttributeError, RuntimeError):
        # Only local computation/contract errors are optional. CancelledError
        # (BaseException), termination and source/access errors propagate.
        decision = safe_fallback()
    observation = NeedObservation(decision, max(0.0, (time.perf_counter() - started) * 1000))
    callback = _observer.get()
    try:
        if callback is not None:
            callback(observation)
        _logger.info(
            "retrieval_need=%s reason_code=%s decision_source=%s policy_version=%s "
            "decision_latency_ms=%.3f routing_count=1 safe_fallback_count=%d",
            decision.need.value, decision.reason_code, decision.decision_source,
            decision.policy_version, observation.decision_latency_ms, observation.safe_fallback_count,
        )
    except (BudgetExceededError, DeadlineExceededError, CheckpointPayloadCompatibilityError,
            MemoryConflict, PermissionError):
        raise
    except (ValueError, TypeError, ArithmeticError, AttributeError, RuntimeError):
        pass  # An optional event sink failure cannot replace a valid answer.
    return observation
