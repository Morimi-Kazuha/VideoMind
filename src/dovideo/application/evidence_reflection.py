"""Bounded advisory grading; no answer, provenance, tools or retrieval authority."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from .errors import BudgetExceededError, DeadlineExceededError
from .execution_budget import AgentExecutionBudget
from .follow_up_budget import optional_model_headroom


class ReflectionMode(str, Enum):
    OFF = "OFF"
    SHADOW = "SHADOW"


class AssessmentDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Text = Annotated[StrictStr, Field(min_length=1, max_length=240)]
Index = Annotated[StrictInt, Field(ge=0, le=7)]
References = Annotated[tuple[Index, ...], Field(max_length=8)]
GapCode = Literal["MISSING_COMPARISON_SIDE", "MISSING_CAUSAL_EVIDENCE",
    "MISSING_TEMPORAL_CONTEXT", "PARTIAL_FACT_COVERAGE", "TOPIC_RELATED_BUT_NOT_ANSWERABLE",
    "INSUFFICIENT_SOURCE_TEXT", "UNCERTAIN_SEMANTIC_SUPPORT"]


class Requirement(AssessmentDTO):
    # Exact question span constrains gaps to the actual question, including an
    # application-resolved M1 question. It cannot introduce new video entities.
    requirement_key: Annotated[StrictStr, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
    question_span: Text
    candidate_references: References = ()


class MissingRequirement(Requirement):
    reason_code: GapCode


class AssessmentSuggestion(AssessmentDTO):
    status: Literal["SUFFICIENT", "INSUFFICIENT", "UNKNOWN"]
    reason_code: Literal["SEMANTIC_COVERAGE", "EVIDENCE_GAPS", "UNCERTAIN_SEMANTIC_SUPPORT"]
    covered_requirements: Annotated[tuple[Requirement, ...], Field(max_length=6)] = ()
    missing_requirements: Annotated[tuple[MissingRequirement, ...], Field(max_length=6)] = ()
    candidate_references: References = ()

    @model_validator(mode="after")
    def consistent(self):
        requirements = self.covered_requirements + self.missing_requirements
        if len(requirements) > 6 or len({r.requirement_key for r in requirements}) != len(requirements):
            raise ValueError("duplicate or excessive requirements")
        expected = {"SUFFICIENT": "SEMANTIC_COVERAGE", "INSUFFICIENT": "EVIDENCE_GAPS",
                    "UNKNOWN": "UNCERTAIN_SEMANTIC_SUPPORT"}[self.status]
        if self.reason_code != expected:
            raise ValueError("inconsistent assessment reason")
        if self.status == "SUFFICIENT" and (not self.covered_requirements or self.missing_requirements or not self.candidate_references):
            raise ValueError("sufficient requires coverage and references")
        if self.status == "INSUFFICIENT" and not self.missing_requirements:
            raise ValueError("insufficient requires specific gaps")
        for refs in (self.candidate_references, *(r.candidate_references for r in requirements)):
            if len(set(refs)) != len(refs):
                raise ValueError("duplicate candidate references")
        if any(not r.candidate_references for r in self.covered_requirements):
            raise ValueError("covered requirement needs evidence")
        if set(self.candidate_references) != {i for r in requirements for i in r.candidate_references}:
            raise ValueError("reference union mismatch")
        return self


@dataclass(frozen=True)
class EvidenceAssessment:
    # scope is exclusively application-owned, not a provider output field.
    suggestion: AssessmentSuggestion | None
    evidence_scope: Literal["BOUNDED_ANSWER_INPUT"] = "BOUNDED_ANSWER_INPUT"
    scope_reason: str = "MODEL_ADVISORY"

    @property
    def status(self):
        return self.suggestion.status if self.suggestion else "UNKNOWN"


def validate_assessment(raw, question, candidates):
    if isinstance(raw, AssessmentSuggestion):
        raw = raw.model_dump(warnings=False)
    result = AssessmentSuggestion.model_validate(raw)
    for req in result.covered_requirements + result.missing_requirements:
        if not req.question_span.strip() or req.question_span not in question:
            raise ValueError("requirement not anchored in question")
    if any(i >= len(candidates) for i in result.candidate_references):
        raise ValueError("nonexistent candidate")
    return result


class ReflectionModelPort(Protocol):
    def messages(self, question: str, candidates: tuple[dict, ...], *, mixed: bool): ...
    async def assess(self, question: str, candidates: tuple[dict, ...], *, mixed: bool) -> AssessmentSuggestion: ...


@dataclass(frozen=True)
class ReflectionObservation:
    assessment: EvidenceAssessment
    outcome: str
    latency_ms: float


_capture = ContextVar("evidence_reflection_capture", default=None)
_logger = logging.getLogger("dovideo.evidence_reflection")


@contextmanager
def capture_evidence_reflection():
    records = []
    token = _capture.set(records.append)
    try:
        yield records
    finally:
        _capture.reset(token)


class EvidenceReflection:
    def __init__(self, model: ReflectionModelPort | None, mode: ReflectionMode = ReflectionMode.OFF):
        if not isinstance(mode, ReflectionMode):
            raise ValueError("invalid reflection mode")
        self.model, self.mode = model, mode

    async def observe(self, question, candidates, *, mixed=False, messages=None, answer_reserve=0, time_reserve=35, return_assessment=False):
        if self.mode == ReflectionMode.OFF:
            return EvidenceAssessment(None, scope_reason="REFLECTION_OFF") if return_assessment else None
        started = time.perf_counter()
        assessment = EvidenceAssessment(None)
        outcome = "UNAVAILABLE"
        try:
            AgentExecutionBudget.check("Evidence reflection")
            if not candidates:
                assessment = EvidenceAssessment(None, scope_reason="NO_SUPPORT_IN_RETRIEVED_SCOPE")
                outcome = "NO_CANDIDATES"
            elif self.model is None:
                outcome = "MODEL_UNAVAILABLE"
            else:
                remaining = AgentExecutionBudget.remaining_seconds()
                if remaining is not None and remaining < time_reserve + 5:
                    outcome = "SKIPPED_TIME"
                elif messages is None or not optional_model_headroom(messages, "EVIDENCE_REFLECTION", 1024, answer_reserve):
                    outcome = "SKIPPED_BUDGET"
                else:
                    # Local timeout does not install a new global deadline frame.
                    async with asyncio.timeout(5):
                        raw = await self.model.assess(question, candidates, mixed=mixed)
                    assessment = EvidenceAssessment(validate_assessment(raw, question, candidates))
                    outcome = "ASSESSED"
        except (BudgetExceededError, DeadlineExceededError):
            raise
        except TimeoutError:
            AgentExecutionBudget.check("Evidence reflection timeout")
            outcome = "TIMEOUT"
        except (ValueError, TypeError):
            outcome = "INVALID_RESPONSE"
        except Exception:
            outcome = "PROVIDER_FAILURE"
        finally:
            observation = ReflectionObservation(assessment, outcome, (time.perf_counter()-started)*1000)
            try:
                if _capture.get() is not None:
                    _capture.get()(observation)
                _logger.info("reflection outcome=%s status=%s scope=%s latency_ms=%.3f",
                    outcome, assessment.status, assessment.evidence_scope, observation.latency_ms)
            except Exception:
                pass
        # Original observer return remains None. R4 consumes this same checked
        # result, never a second grading call or a global observation cache.
        return assessment if return_assessment else None
