"""Follow-up-only accounting at the physical Hybrid ranking entry."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging

from .follow_up_budget import FollowUpBudgetExceededError
from .execution_budget import AgentExecutionBudget
from .retrieval_documents import normalize_text


class RetrievalBudgetExceededError(FollowUpBudgetExceededError):
    """Terminal request limit; never a video absence or optional success."""


@dataclass
class RetrievalRequestBudget:
    enforced: bool = True
    initial_calls: int = 0
    repair_calls: int = 0
    repair_rounds: int = 0
    queries: list[str] = field(default_factory=list, repr=False)

    def consume(self, query):
        AgentExecutionBudget.check("Hybrid admission")
        repair = _phase.get() == "REPAIR"
        if self.enforced and (self.initial_calls + self.repair_calls >= 4 or (self.repair_calls >= 1 if repair else self.initial_calls >= 3)):
            raise RetrievalBudgetExceededError("追问超过检索次数预算")
        if repair and self.repair_rounds != 1:
            raise RetrievalBudgetExceededError("补检索缺少请求级准入")
        if repair:
            self.repair_calls += 1
        else:
            self.initial_calls += 1
        self.queries.append(normalize_text(query or ""))

    def snapshot(self):
        return {"initial_base_calls": self.initial_calls, "repair_base_calls": self.repair_calls,
                "total_base_calls": self.initial_calls + self.repair_calls, "repair_rounds": self.repair_rounds}


_current = ContextVar("follow_up_retrieval_budget", default=None)
_phase = ContextVar("follow_up_retrieval_phase", default="INITIAL")
_capture = ContextVar("follow_up_retrieval_budget_capture", default=None)


def current_retrieval_budget():
    return _current.get()


def admit_base_retrieval(query):
    if (budget := _current.get()) is not None:
        budget.consume(query)


@contextmanager
def retrieval_budget_scope(enabled=False, *, enforced=True):
    if not enabled or _current.get() is not None:
        yield _current.get()
        return
    budget = RetrievalRequestBudget(enforced=enforced)
    token = _current.set(budget)
    phase = _phase.set("INITIAL")
    try:
        yield budget
    finally:
        _phase.reset(phase)
        _current.reset(token)
        snapshot = budget.snapshot()
        try:
            if _capture.get() is not None:
                _capture.get().append(snapshot)
            logging.getLogger("dovideo.retrieval_budget").info("retrieval_budget initial=%d repair=%d total=%d rounds=%d",
                budget.initial_calls, budget.repair_calls, budget.initial_calls + budget.repair_calls, budget.repair_rounds)
        except Exception:
            pass  # Observation must not replace answer or terminal exception.


@contextmanager
def repair_retrieval_round():
    budget = _current.get()
    if budget is None or budget.repair_rounds or budget.repair_calls:
        raise RetrievalBudgetExceededError("补检索不可重复或脱离请求预算")
    budget.repair_rounds += 1
    token = _phase.set("REPAIR")
    try:
        yield budget
    finally:
        _phase.reset(token)


@contextmanager
def capture_retrieval_budget():
    records = []
    token = _capture.set(records)
    try:
        yield records
    finally:
        _capture.reset(token)
