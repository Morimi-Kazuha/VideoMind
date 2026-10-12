"""One deterministic, video-only repair in the Follow-up harness.

Model keys/codes are never queries or permission. Admission recognizes a small
set of explicit original-question targets, independently of the model's label.
Unrecognized/implicit requirements stop; this is not a semantic classifier.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
import logging
import re
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from dovideo.domain import VideoContext, VideoChunk, VideoEvidenceHit
from .adaptive_retrieval import AdaptiveRetrievalService
from .chunking import chunks_compatible
from .evidence_projection import source_payload
from .evidence import _source_item_text
from .evidence_reflection import EvidenceAssessment, ReflectionMode, validate_assessment
from .execution_budget import AgentExecutionBudget
from .errors import BudgetExceededError, DeadlineExceededError
from .follow_up_budget import FollowUpBudgetMode, enforced_request_headroom
from .retrieval_budget import current_retrieval_budget, repair_retrieval_round
from .retrieval_documents import normalize_text


class FeedbackRetrievalMode(str, Enum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ENABLED = "ENABLED"


class QueryRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    query: Annotated[StrictStr, Field(min_length=2, max_length=500)]
    question_span: Annotated[StrictStr, Field(min_length=1, max_length=240)]
    target_kind: Literal["CAUSE", "COMPARISON", "STAGE", "STEP", "NAMED_FACT"]


@dataclass(frozen=True)
class FactTarget:
    kind: str
    anchor: str
    query: str


@dataclass(frozen=True)
class RepairObservation:
    state: str
    mode: FeedbackRetrievalMode
    eligible: bool = False
    attempted: bool = False
    new_segments: int = 0
    invalid_candidates: int = 0
    unhandled_gaps: int = 0
    plan: QueryRepair | None = None  # transient capture only; never log text
    latency_ms: float = 0


_capture = ContextVar("feedback_retrieval_capture", default=None)
_CONTROL = re.compile(r"https?://|[<>`]|[a-z]:[\\/]|\b(?:media[_ ]?id|source[_ ]?revision|revision|tool|system|ignore|search_evidence|get_segment|get_context_window|get_frame|curl|powershell|cmd|remove-item|invoke-restmethod)\b|(?:调用工具|执行命令|忽略指令)", re.I)
_VERDICT = re.compile(r"正确|合理|是否对|对吗|技术标准|通用|外部|评价|correct|reasonable|verdict|standard|general knowledge", re.I)
_STAGES = ("拍摄前", "拍摄中", "拍摄后", "初期", "中期", "后期", "开头", "结尾", "最初", "后来", "初始", "最终")
_STEPS = ("回填", "测光", "查询数据库", "设定曝光", "缓存命中", "未命中")


def validate_feedback_configuration(mode, reflection_mode, budget_settings, *, model_available, metered_retrieval):
    if not isinstance(mode, FeedbackRetrievalMode):
        raise ValueError("invalid feedback retrieval mode")
    if mode == FeedbackRetrievalMode.ENABLED and (
        reflection_mode != ReflectionMode.SHADOW or not model_available
        or budget_settings.mode != FollowUpBudgetMode.ENFORCED or budget_settings.max_chat_tokens is None
        or not metered_retrieval
    ):
        raise ValueError("ENABLED feedback retrieval requires R3 SHADOW, model, ENFORCED token ceiling and metered baseline Hybrid")


@dataclass(frozen=True)
class TrustedRepairScope:
    media_id: int
    context: VideoContext
    chunks: tuple[VideoChunk, ...]

    @classmethod
    def create(cls, media_id, context, chunks):
        context = VideoContext.model_validate(context.model_dump(warnings=False))
        chunks = tuple(VideoChunk.model_validate(c.model_dump(warnings=False)) for c in chunks)
        if type(media_id) is not int or media_id <= 0 or not context.source_revision or not context.provenance_version:
            raise ValueError("missing trusted scope")
        if not context.observations or not chunks_compatible(chunks, context.segments):
            raise ValueError("incomplete source/chunk verification")
        observations = {o.source_item.source_item_id: o for o in context.observations}
        for segment in context.segments:
            if not segment.segment_id or segment.source_revision != context.source_revision or not segment.source_items:
                raise ValueError("incomplete segment identity")
            if any(observations.get(item.source_item_id) is None or observations[item.source_item_id].source_item != item
                   for item in segment.source_items):
                raise ValueError("incomplete original observations")
            if any(_source_item_text(segment, item) is None for item in segment.source_items):
                raise ValueError("source text does not match authoritative digest")
            for item in segment.source_items:
                overlaps = (segment.start_ms <= item.timestamp_ms < segment.end_ms if item.source_type == "OCR"
                    else max(segment.start_ms, item.timestamp_ms) < min(segment.end_ms, item.end_ms or item.timestamp_ms))
                if item.segment_id != segment.segment_id or item.source_revision != context.source_revision or not overlaps:
                    raise ValueError("source item identity/window mismatch")
        return cls(media_id, context, chunks)

    def checked_hits(self, hits):
        hits = tuple(hits)
        if len(hits) > 8:
            raise ValueError("oversized retrieval results")
        valid = AdaptiveRetrievalService._trusted_hits(hits, self.chunks)
        if len(valid) != len(hits):
            raise ValueError("foreign or invalid retrieval identity/text")
        if any(not hit.source_item_ids or hit.source not in {"ASR", "OCR", "ASR+OCR"} for hit, _ in valid):
            raise ValueError("missing source provenance")
        if any(hit.source != ("ASR+OCR" if hit.transcript.strip() and hit.ocr_texts else "OCR" if hit.ocr_texts else "ASR") for hit, _ in valid):
            raise ValueError("source channel mismatch")
        return tuple(hit for hit, _ in valid)


def _bounded_text(candidates):
    return "\n".join(str(p.get("asrExcerpt", "")) + "\n" + "\n".join(p.get("ocrExcerpts", ())) for p in candidates)


def explicit_targets(question, text, *, mixed):
    """Closed recognizers, not a reason-code or requirement-key allowlist."""
    if not re.search(r"作者|讲者|讲师|老师|视频", question) or _CONTROL.search(question):
        return ()
    targets = []
    # Unique literal formulas identify a requested fact without importing model
    # entities. Repeated occurrence/position of the SAME formula is ambiguous.
    for atom in dict.fromkeys(re.findall(r"\d+n(?:\+\d+)?", question)):
        if atom not in text and question.count(atom) == 1:
            targets.append(FactTarget("NAMED_FACT", atom, "视频 " + atom))
    if not mixed and not _VERDICT.search(question):
        cause = re.search(r"(为什么|为何)([^？?。\n]{2,100})", question)
        if cause and not re.search(r"因为|由于|为了|原因是|because|in order to", text, re.I):
            query = question[:cause.start()] + cause[2].strip() + "的原因"
            targets.append(FactTarget("CAUSE", cause[1], query))
        side = r"([A-Za-z][A-Za-z0-9_-]{0,19}|[甲乙丙丁]|单晶|薄膜)"
        comparison = re.search(side + r"\s*(?:和|与)\s*" + side + r"\s*的(内存开销|转换率|时间复杂度|复杂度|耗时)", question)
        if comparison:
            a, b, topic = comparison.groups()
            present = [bool(re.search(re.escape(s) + r"[^。；\n]{0,20}" + re.escape(topic)
                        + r"[^。；\n]{0,8}(?:\d|[一二三四五六七八九十百]|O\()", text)) for s in (a, b)]
            if sum(present) == 1:
                absent = b if present[0] else a
                targets.append(FactTarget("COMPARISON", absent, absent + " " + topic))
    # Only explicitly named video stages/steps whose literal target is absent.
    # Generic 'is the cache plan reasonable?' supplies no safe step target.
    stages = [s for s in _STAGES if s in question]
    topics = [s for s in ("推荐", "方案", "颜色", "校准", "步骤", "处理", "实现", "复杂度") if s in question]
    if len(stages) >= 2 and topics:
        for atom in stages:
            if atom not in text:
                targets.append(FactTarget("STAGE", atom, "视频 " + atom + " " + " ".join(topics)))
    for atom in _STEPS:
        if atom in question and atom not in text:
            targets.append(FactTarget("STEP", atom, "视频 " + atom))
    return tuple(targets)


def admit_gap(assessment, question, candidates, *, mixed=False, executed_queries=()):
    if not isinstance(assessment, EvidenceAssessment) or assessment.suggestion is None:
        return "NOT_ELIGIBLE", None, 0
    if assessment.evidence_scope != "BOUNDED_ANSWER_INPUT":
        return "INVALID_GAP", None, 0
    try:
        proposal = validate_assessment(assessment.suggestion, question, candidates)
    except (ValueError, TypeError):
        return "INVALID_GAP", None, 0
    if proposal.status != "INSUFFICIENT":
        return "NOT_REQUIRED" if proposal.status == "SUFFICIENT" else "NOT_ELIGIBLE", None, 0
    if not candidates:
        return "NOT_ELIGIBLE", None, len(proposal.missing_requirements)
    targets = explicit_targets(question, _bounded_text(candidates), mixed=mixed)
    plans = []
    for target in targets:
        gaps = [g for g in proposal.missing_requirements if target.anchor in g.question_span
                and not _CONTROL.search(g.question_span) and not (mixed and _VERDICT.search(g.question_span))]
        if not gaps:
            continue
        # A separately covered span claiming this sole target is contradictory.
        if any(c.question_span.strip() == target.anchor for c in proposal.covered_requirements):
            continue
        span = min((g.question_span for g in gaps), key=lambda s: (len(s), question.index(s)))
        query = target.query.strip().rstrip("？?")
        if not query or len(query) > 500 or _CONTROL.search(query):
            continue
        if normalize_text(query) in {normalize_text(q) for q in (question, *executed_queries)}:
            continue
        plans.append(QueryRepair(query=query, question_span=span, target_kind=target.kind))
    if not plans:
        return "NO_VALID_REPAIR", None, len(proposal.missing_requirements)
    # Priority follows original question targets; duplicates map to one plan.
    return "ELIGIBLE", plans[0], max(0, len(proposal.missing_requirements) - 1)


def validate_repair(plan, expected, question, executed_queries=()):
    try:
        checked = QueryRepair.model_validate(plan.model_dump(warnings=False) if isinstance(plan, QueryRepair) else plan)
        return checked if (checked == expected and checked.question_span in question and not _CONTROL.search(checked.query)
            and normalize_text(checked.query) not in {normalize_text(q) for q in (question, *executed_queries)}) else None
    except (ValueError, TypeError):
        return None


def merge_evidence(initial, additional):
    original_list, originals_seen = [], set()
    for hit in initial:
        identity = (hit.source_revision, hit.segment_id)
        if identity not in originals_seen:
            originals_seen.add(identity)
            original_list.append(hit)
    original = tuple(original_list)
    seen = {(h.source_revision, h.segment_id) for h in original}
    new = []
    for hit in additional:
        identity = (hit.source_revision, hit.segment_id)
        if identity not in seen:
            seen.add(identity)
            new.append(hit)
    if not new:
        return tuple(initial), 0
    # Keep at least seven originals at capacity; reserve one incremental slot.
    kept = original[:7] if len(original) >= 8 else original
    added = new[:8 - len(kept)]
    return kept + tuple(added), len(added)


async def repair_evidence(mode, assessment, *, question, media_id, context, chunks, initial,
                          retrieval, mixed=False, downstream_reserve=0, planner_attempts=3):
    if mode == FeedbackRetrievalMode.OFF:
        return tuple(initial)
    start = time.perf_counter()
    state, plan, eligible, attempted, added, unhandled, rejected = "NOT_ELIGIBLE", None, False, False, 0, 0, 0
    try:
        try:
            scope = TrustedRepairScope.create(media_id, context, chunks)
            scope.checked_hits(initial)
        except (ValueError, TypeError, AttributeError):
            state = "INVALID_SCOPE"
            return tuple(initial)
        projection = tuple(source_payload(i, h, context.observations) for i, h in enumerate(initial))
        budget = current_retrieval_budget()
        executed = tuple(budget.queries) if budget is not None else (question,)
        state, plan, unhandled = admit_gap(assessment, question, projection, mixed=mixed, executed_queries=executed)
        if plan is None:
            return tuple(initial)
        eligible = True
        if validate_repair(plan, plan, question, executed) is None:
            state = "INVALID_REPAIR"
            return tuple(initial)
        if mode == FeedbackRetrievalMode.SHADOW:
            state = "SHADOW_PLANNED"
            return tuple(initial)
        if budget is None or not budget.initial_calls or not budget.enforced:
            state = "UNMETERED_RETRIEVAL"
            return tuple(initial)
        if budget.repair_rounds or budget.repair_calls:
            state = "ALREADY_REPAIRED"
            return tuple(initial)
        remaining = AgentExecutionBudget.remaining_seconds()
        if remaining is None or remaining < (55 if mixed else 40):
            state = "SKIPPED_TIME"
            return tuple(initial)
        # Deterministic repair costs zero Chat. Base Hybrid may invoke existing
        # planner attempts; its real Gateway admissions remain authoritative.
        if not enforced_request_headroom(planner_attempts * 8192, downstream_reserve):
            state = "SKIPPED_BUDGET"
            return tuple(initial)
        with repair_retrieval_round():
            attempted, state = True, "REPAIR_ATTEMPTED"
            repaired_context = scope.context.model_copy(update={"user_goal": plan.query})
            try:
                async with asyncio.timeout(5):
                    hits = await retrieval.search_evidence_baseline(scope.media_id, repaired_context, chunks=scope.chunks)
            except (BudgetExceededError, DeadlineExceededError):
                raise
            except TimeoutError:
                AgentExecutionBudget.check("Feedback retrieval timeout")
                state = "RETRIEVAL_TIMEOUT"
                return tuple(initial)
            except Exception:
                state = "RETRIEVAL_FAILED"
                return tuple(initial)
            if budget.repair_calls != 1:
                state = "UNMETERED_RETRIEVAL"
                return tuple(initial)
            try:
                additional = scope.checked_hits(hits)
            except (ValueError, TypeError):
                state = "INVALID_EVIDENCE"
                rejected = min(8, len(hits)) if isinstance(hits, (tuple, list)) else 1
                return tuple(initial)
            merged, added = merge_evidence(initial, additional)
            state = "EVIDENCE_ADDED" if added else "NO_NEW_EVIDENCE"
            return merged
    except asyncio.CancelledError:
        state = "CANCELLED"
        raise
    except (BudgetExceededError, DeadlineExceededError):
        state = "TERMINAL_BUDGET_OR_DEADLINE"
        raise
    finally:
        observation = RepairObservation(state, mode, eligible, attempted, added, rejected, unhandled, plan,
            (time.perf_counter() - start) * 1000)
        try:
            if _capture.get() is not None:
                _capture.get().append(observation)
            logging.getLogger("dovideo.feedback_retrieval").info("feedback state=%s mode=%s eligible=%s attempted=%s new_segments=%d latency_ms=%.3f",
                state, mode.value, eligible, attempted, added, observation.latency_ms)
        except Exception:
            pass


@contextmanager
def capture_feedback_retrieval():
    records = []
    token = _capture.set(records)
    try:
        yield records
    finally:
        _capture.reset(token)
