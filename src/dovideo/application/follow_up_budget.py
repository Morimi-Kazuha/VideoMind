"""Request-local chat-attempt accounting using the existing deadline and estimates.

OFF does no new accounting. SHADOW measures hypothetical admissions. ENFORCED
requires an explicit, calibrated server limit. No prompts or model output are
retained. Missing usage keeps the full input/output reservation conservatively.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
import logging
import math

from .errors import BudgetExceededError, DeadlineExceededError
from .execution_budget import AgentExecutionBudget


class FollowUpBudgetExceededError(BudgetExceededError):
    """Terminal request admission/settlement failure, never optional work."""


class FollowUpRequestDeadlineExceededError(DeadlineExceededError, BudgetExceededError):
    """Retain timeout semantics while crossing legacy retrieval budget guards."""


class FollowUpBudgetMode(str, Enum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ENFORCED = "ENFORCED"


@dataclass(frozen=True)
class FollowUpBudgetSettings:
    mode: FollowUpBudgetMode = FollowUpBudgetMode.OFF
    max_chat_tokens: int | None = None

    def __post_init__(self):
        if not isinstance(self.mode, FollowUpBudgetMode):
            raise ValueError("invalid Follow-up budget mode")
        if self.max_chat_tokens is not None and (type(self.max_chat_tokens) is not int or not 1 <= self.max_chat_tokens <= 10**9):
            raise ValueError("invalid Follow-up token limit")
        if self.mode == FollowUpBudgetMode.ENFORCED and self.max_chat_tokens is None:
            raise ValueError("ENFORCED requires an explicit token limit")


def estimate_chat_input(messages):
    size = sum(len(str(m.get("role", "")).encode("utf-8")) + len(str(m.get("content", "")).encode("utf-8")) for m in messages)
    return max(1, (size + 1) // 2)


_STAGE_RESERVES = {"EVIDENCE_REFLECTION": 1024, "CHUNK_SUMMARY": 4096, "RETRIEVAL_PLANNER": 1024, "ADAPTIVE_RETRIEVAL_PLANNER": 1024,
    "PLANNER": 2048, "PLANNER_REPAIR": 2048, "REPLANNER": 2048, "EXECUTOR": 8192,
    "EXECUTOR_REPAIR": 8192, "EXECUTOR_TURN": 8192, "EXECUTOR_CONTINUATION": 8192, "CRITIC": 16000,
    "MIXED_KNOWLEDGE": 2048, "DIRECT_FOLLOW_UP": 2048, "QUERY_REWRITE": 512, "FOLLOW_UP": 4096, "ROLLING_SUMMARY": 2048}


def stage_output_estimate(stage):
    return _STAGE_RESERVES.get(stage.upper(), 8192)


def _reported_number(usage, *names):
    if not isinstance(usage, Mapping):
        return None
    for name in names:
        value = usage.get(name)
        if (not isinstance(value, bool) and isinstance(value, (int, float))
                and 0 <= value <= 10**9 and math.isfinite(value)):
            return math.ceil(value)
    return None


@dataclass(frozen=True)
class RequestAdmission:
    owner: "FollowUpRequestBudget"
    call_id: int
    stage: str
    input_estimate: int
    output_reserve: int


class FollowUpRequestBudget:
    def __init__(self, settings):
        self.settings = settings
        self.charged_tokens = 0
        self.reserved_tokens = 0
        self.pending = {}
        self.next_id = 0
        self.events = []
        self.attempts = Counter()
        self.denials = 0
        self.overruns = 0
        self.components = Counter()
        self.component_tokens = Counter()
        self.estimated_sent_tokens = 0
        self.usage_sources = Counter()

    def record_event(self, event):
        # Aggregate counters remain complete; detail retention is bounded.
        if len(self.events) < 256:
            self.events.append(event)

    def admit(self, messages, stage, output_cap):
        check_request_deadline("Follow-up model admission")
        input_estimate = estimate_chat_input(messages)
        output = output_cap if type(output_cap) is int and output_cap > 0 else stage_output_estimate(stage)
        reserve = input_estimate + output
        ceiling = self.settings.max_chat_tokens
        before = self.charged_tokens + self.reserved_tokens
        allowed = None if ceiling is None else before + reserve <= ceiling
        safe_stage = stage if stage in _STAGE_RESERVES else "OTHER"
        if allowed is False:
            self.denials += 1
        self.record_event({"kind": "admission", "stage": safe_stage, "input_estimate": input_estimate,
                            "output_reserve": output, "cumulative_before": before, "allowed": allowed})
        if allowed is False and self.settings.mode == FollowUpBudgetMode.ENFORCED:
            raise FollowUpBudgetExceededError("追问超过模型用量预算，请缩小问题后重试")
        self.next_id += 1
        receipt = RequestAdmission(self, self.next_id, safe_stage, input_estimate, output)
        self.pending[receipt.call_id] = receipt
        self.reserved_tokens += reserve
        return receipt

    def finish(self, receipt, *, sent, usage=None, preserve_termination=False):
        if receipt.owner is not self or self.pending.get(receipt.call_id) is not receipt:
            raise ValueError("unknown or already settled request admission")
        del self.pending[receipt.call_id]
        reserve = receipt.input_estimate + receipt.output_reserve
        self.reserved_tokens -= reserve
        actual = 0
        source = "UNSENT"
        if sent:
            self.attempts[receipt.stage] += 1
            self.estimated_sent_tokens += reserve
            total = _reported_number(usage or {}, "total_tokens", "totalTokens")
            incoming = _reported_number(usage or {}, "prompt_tokens", "promptTokens")
            outgoing = _reported_number(usage or {}, "completion_tokens", "completionTokens")
            if incoming is not None and outgoing is not None:
                total = max(total or 0, incoming + outgoing)
            if total is not None:
                actual, source = total, "PROVIDER"
            else:
                actual, source = reserve, "ESTIMATE"
            self.charged_tokens += actual
            self.usage_sources[source] += 1
        self.record_event({"kind": "settlement", "stage": receipt.stage, "tokens": actual,
                            "usage_source": source, "reservation": reserve, "cumulative_total": self.charged_tokens})
        ceiling = self.settings.max_chat_tokens
        if ceiling is not None and self.charged_tokens > ceiling:
            self.overruns += 1
            if self.settings.mode == FollowUpBudgetMode.ENFORCED and not preserve_termination:
                raise FollowUpBudgetExceededError("追问超过模型用量预算，请缩小问题后重试")

    def snapshot(self):
        return {"mode": self.settings.mode.value, "limit": self.settings.max_chat_tokens,
            "charged_chat_tokens": self.charged_tokens, "reserved_chat_tokens": self.reserved_tokens,
            "chat_http_attempts": sum(self.attempts.values()), "stages": dict(self.attempts),
            "component_calls": dict(self.components), "reported_component_tokens": dict(self.component_tokens),
            "estimated_sent_chat_tokens": self.estimated_sent_tokens, "usage_sources": dict(self.usage_sources),
            "hypothetical_denials": self.denials, "overruns": self.overruns, "events": list(self.events)}


_current: ContextVar[FollowUpRequestBudget | None] = ContextVar("follow_up_chat_budget", default=None)
_capture: ContextVar[object | None] = ContextVar("follow_up_budget_capture", default=None)
_logger = logging.getLogger("dovideo.follow_up_budget")


def check_request_deadline(stage):
    try:
        AgentExecutionBudget.check(stage)
    except DeadlineExceededError as exc:
        if _current.get() is not None:
            raise FollowUpRequestDeadlineExceededError(str(exc)) from None
        raise


def record_request_component(component, *, usage=None, attempt=True):
    """Non-chat observations never enter chat admission or the task ledger."""
    budget = _current.get()
    if budget is None:
        return
    if component not in {"embedding", "qdrant_query", "bm25", "reranker"}:
        raise ValueError("unknown request component")
    if attempt:
        budget.components[component] += 1
    total = _reported_number(usage or {}, "total_tokens", "totalTokens")
    if total is not None:
        budget.component_tokens[component] += total


def admit_request_model_call(messages, stage, output_cap):
    budget = _current.get()
    return None if budget is None else budget.admit(messages, stage, output_cap)


def optional_model_headroom(messages, stage, output_cap, downstream_reserve):
    """Optional work requires measurable headroom even in budget SHADOW.

    The downstream reserve is application-owned and conservatively covers
    original answer/knowledge/summary attempts. OFF or uncalibrated limits skip.
    """
    check_request_deadline(stage)
    budget = _current.get()
    if budget is None or budget.settings.max_chat_tokens is None:
        return False
    return (budget.charged_tokens + budget.reserved_tokens + estimate_chat_input(messages)
            + output_cap + downstream_reserve <= budget.settings.max_chat_tokens)


def finish_request_model_call(receipt, **kwargs):
    if receipt is not None:
        receipt.owner.finish(receipt, **kwargs)


def enforced_request_headroom(additional_reserve, downstream_reserve):
    """R4 optional retrieval requires an enforced, measurable shared ledger."""
    check_request_deadline("Feedback retrieval admission")
    budget = _current.get()
    return bool(budget is not None and budget.settings.mode == FollowUpBudgetMode.ENFORCED
        and budget.settings.max_chat_tokens is not None
        and budget.charged_tokens + budget.reserved_tokens + additional_reserve + downstream_reserve
            <= budget.settings.max_chat_tokens)


@contextmanager
def follow_up_budget_scope(settings):
    existing = _current.get()
    if existing is not None or settings.mode == FollowUpBudgetMode.OFF:
        yield existing
        return
    budget = FollowUpRequestBudget(settings)
    token = _current.set(budget)
    try:
        yield budget
    finally:
        _current.reset(token)
        snapshot = budget.snapshot()
        callback = _capture.get()
        if callback is not None:
            callback(snapshot)
        try:
            _logger.info("follow_up_budget mode=%s chat_tokens=%d chat_http_attempts=%d hypothetical_denials=%d overruns=%d estimated_sent_tokens=%d usage_sources=%s stages=%s components=%s component_tokens=%s",
                settings.mode.value, budget.charged_tokens, sum(budget.attempts.values()), budget.denials, budget.overruns,
                budget.estimated_sent_tokens, dict(budget.usage_sources),
                dict(budget.attempts), dict(budget.components), dict(budget.component_tokens))
        except Exception:
            pass  # observation must not replace a result or terminal exception


@contextmanager
def capture_follow_up_budget():
    records = []
    token = _capture.set(records.append)
    try:
        yield records
    finally:
        _capture.reset(token)
