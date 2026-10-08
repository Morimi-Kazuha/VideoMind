"""LLM-assisted bounded multi-query retrieval over the existing Hybrid RAG.

Plans are search hints, never evidence. The caller owns media and chunk scope.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from dovideo.domain import VideoChunk, VideoEvidenceHit
from .errors import BudgetExceededError, DeadlineExceededError
from .execution_budget import AgentExecutionBudget
from .retrieval import MAX_USER_HITS, vector_scope
from .retrieval_documents import normalize_text, segment_identity
from .retrieval_observation import capture_retrieval, observe_retrieval


class RetrievalRoute(str, Enum):
    SINGLE_HYBRID = "SINGLE_HYBRID"
    BOUNDED_MULTI_QUERY = "BOUNDED_MULTI_QUERY"


ReasonCode = Literal["SINGLE_FACT", "COMPARISON", "TEMPORAL_CHANGE", "MULTI_CONDITION", "CAUSAL_CHAIN"]
Query = Annotated[StrictStr, Field(min_length=2, max_length=500)]


class RoutingSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    retrieval_route: RetrievalRoute
    reason_code: ReasonCode
    sub_queries: Annotated[list[Query], Field(max_length=3)]

    @model_validator(mode="after")
    def valid_plan(self):
        queries = [normalize_text(q) for q in self.sub_queries]
        if any(len(q) < 2 for q in queries) or len(set(queries)) != len(queries):
            raise ValueError("empty or duplicate subquery")
        multi = self.retrieval_route == RetrievalRoute.BOUNDED_MULTI_QUERY
        if multi and (len(queries) < 2 or self.reason_code == "SINGLE_FACT"):
            raise ValueError("invalid complex plan")
        if not multi and (queries or self.reason_code != "SINGLE_FACT"):
            raise ValueError("invalid single plan")
        return self


class RetrievalRoutingPort(Protocol):
    async def suggest_retrieval(self, query: str) -> RoutingSuggestion: ...


@dataclass(frozen=True)
class AdaptiveRetrievalSettings:
    enabled: bool = False
    max_queries: int = 3
    max_candidates: int = MAX_USER_HITS
    planning_timeout_seconds: float = 5.0

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be boolean")
        for value, ceiling in ((self.max_queries, 3), (self.max_candidates, MAX_USER_HITS)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("adaptive limit outside application bounds")
        if (isinstance(self.planning_timeout_seconds, bool)
                or not 0 < self.planning_timeout_seconds <= 10):
            raise ValueError("planning timeout outside application bounds")


@dataclass(frozen=True)
class RetrievalDecision:
    route: RetrievalRoute
    reason: str
    fallback: str
    queries: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalObservation:
    decision: RetrievalDecision
    retrieval_call_count: int
    candidate_count: int
    unique_evidence_count: int
    coverage_status: str
    subquery_candidate_counts: tuple[int, ...]
    route_latency_ms: float


_baseline: ContextVar[bool] = ContextVar("adaptive_baseline", default=False)
_observer: ContextVar[object | None] = ContextVar("adaptive_observer", default=None)
_logger = logging.getLogger("dovideo.adaptive_retrieval")


@contextmanager
def baseline_retrieval():
    """Critic and policy-authorized tool calls keep one retrieval per request."""
    token = _baseline.set(True)
    try:
        yield
    finally:
        _baseline.reset(token)


@contextmanager
def capture_adaptive_retrieval():
    """Request-local evaluation trace; query text never enters telemetry."""
    observations = []
    token = _observer.set(observations.append)
    try:
        yield observations
    finally:
        _observer.reset(token)


def complexity_reason(query: str) -> str:
    """Conservative lexical gate: uncertain/simple requests keep baseline."""
    text = normalize_text(query)
    if re.search(r"后来|最初.*最终|前后|转向|替换|改成|改为|initially|later|switch", text):
        return "TEMPORAL_CHANGE"
    if re.search(r"比较|相比|分别|区别|compare|versus|\bvs\b|difference between", text):
        return "COMPARISON"
    if re.search(r"同时|并且|以及|既.*又|哪些.*为什么|原因.*结果|both|and why", text):
        return "MULTI_CONDITION"
    if re.search(r"前因后果|因果链|导致.*如何|causal chain", text):
        return "CAUSAL_CHAIN"
    return "SINGLE_FACT"


class RetrievalRoutingPolicy:
    def decide(self, query, suggestion, settings):
        # Revalidate even typed objects: model_construct/copy bypass validators.
        suggestion = RoutingSuggestion.model_validate(suggestion.model_dump())
        queries = tuple(q.strip() for q in suggestion.sub_queries)
        if suggestion.retrieval_route == RetrievalRoute.SINGLE_HYBRID:
            return RetrievalDecision(RetrievalRoute.SINGLE_HYBRID, "SINGLE_FACT", "NONE", (query,))
        if len(queries) > settings.max_queries or settings.max_candidates < len(queries):
            raise ValueError("insufficient bounded retrieval capacity")
        original = normalize_text(query)
        # Extractive decomposition is intentionally restrictive: no generated
        # entity, claim, identity, version or new factual premise is accepted.
        if any(normalize_text(q) not in original or normalize_text(q) == original for q in queries):
            raise ValueError("subquery is not a proper original query span")
        return RetrievalDecision(suggestion.retrieval_route, suggestion.reason_code, "NONE", queries)


class AdaptiveRetrievalService:
    """Composition wrapper: no ranking algorithm, public DTO or tool changes."""
    def __init__(self, baseline, planner, settings=None, telemetry=None):
        self.baseline = baseline
        self.planner = planner
        self.settings = settings or AdaptiveRetrievalSettings()
        self.telemetry = telemetry
        self.policy = RetrievalRoutingPolicy()

    async def index(self, media_id, chunks):
        return await self.baseline.index(media_id, chunks)

    async def retrieve(self, media_id, goal, chunks):
        if not self.settings.enabled or _baseline.get():
            return await self.baseline.retrieve(media_id, goal, chunks)
        started = time.perf_counter()
        chunks = tuple(chunks)
        vector_scope(chunks)
        decision = await self._decision(goal or "")
        if decision.route == RetrievalRoute.SINGLE_HYBRID:
            segments = await self.baseline.retrieve(media_id, goal, chunks)
            self._record(decision, (len(segments),), len(segments), len(segments), started)
            return segments
        hits = await self._multi_search(media_id, chunks, decision, started)
        by_hit = {(s.segment_id, s.start_ms, s.end_ms, s.transcript,
                   tuple(dict.fromkeys(t.strip() for t in s.ocr_texts if t.strip()))): s
                  for c in chunks for s in c.raw_segments}
        return tuple(by_hit[(h.segment_id, h.start_ms, h.end_ms, h.transcript, tuple(h.ocr_texts))]
                     for h in hits)

    async def _decision(self, query):
        reason = complexity_reason(query)
        single = lambda fallback: RetrievalDecision(RetrievalRoute.SINGLE_HYBRID, reason, fallback, (query,))
        if reason == "SINGLE_FACT":
            return single("NONE")
        if self.settings.max_queries < 2 or len(query) > 2000:
            return single("CAPACITY")
        remaining = AgentExecutionBudget.remaining_seconds()
        if remaining is not None and remaining <= self.settings.planning_timeout_seconds:
            return single("TIME_HEADROOM")
        try:
            async with asyncio.timeout(self.settings.planning_timeout_seconds):
                suggestion = await self.planner.suggest_retrieval(query)
            AgentExecutionBudget.check("Adaptive planning")
            return self.policy.decide(query, suggestion, self.settings)
        except (BudgetExceededError, DeadlineExceededError):
            raise
        except TimeoutError:
            AgentExecutionBudget.check("Adaptive planning timeout")
            return single("PLANNING_TIMEOUT")
        except (ValueError, TypeError):
            return single("INVALID_PLAN")
        except Exception:
            return single("PROVIDER_FAILURE")

    async def search(self, media_id, query, chunks):
        if not self.settings.enabled or _baseline.get():
            return await self.baseline.search(media_id, query, chunks)
        started = time.perf_counter()
        chunks = tuple(chunks)
        vector_scope(chunks)  # fail closed before planning, including sparse fallback
        decision = await self._decision(query or "")
        if decision.route == RetrievalRoute.SINGLE_HYBRID:
            hits = await self.baseline.search(media_id, query, chunks)
            self._record(decision, (len(hits),), len(hits), len(hits), started)
            return hits
        return await self._multi_search(media_id, chunks, decision, started)

    async def _multi_search(self, media_id, chunks, decision, started):
        batches = []
        # The exact same media_id and immutable chunks go to every invocation.
        # Capture hides intermediate rankings from existing first-result probes.
        with capture_retrieval():
            for subquery in decision.queries:
                AgentExecutionBudget.check("Adaptive retrieval")
                async with asyncio.timeout(AgentExecutionBudget.remaining_seconds()):
                    batch = await self.baseline.search(media_id, subquery, chunks)
                AgentExecutionBudget.check("Adaptive retrieval")
                batches.append(self._trusted_hits(batch, chunks))
        merged = []
        merged_identities = []
        seen = set()
        for rank in range(max((len(b) for b in batches), default=0)):
            for batch in batches:
                if rank < len(batch):
                    hit, identity = batch[rank]
                    if identity not in seen:
                        seen.add(identity)
                        merged.append(hit)
                        merged_identities.append(identity)
        hits = tuple(merged[:self.settings.max_candidates])
        # Coverage is measured after the shared cap, including shared segments.
        retained = set(merged_identities[:self.settings.max_candidates])
        counts = tuple(sum(identity in retained for _, identity in b) for b in batches)
        observe_retrieval(lambda: hits)
        self._record(decision, counts, sum(len(b) for b in batches), len(hits), started)
        return hits

    @staticmethod
    def _trusted_hits(hits, chunks):
        allowed = {}
        for chunk in chunks:
            for segment in chunk.raw_segments:
                revision = segment.source_revision or chunk.source_revision
                if chunk.source_revision and revision != chunk.source_revision:
                    continue
                key = (revision, chunk.chunk_id, segment.segment_id, segment.start_ms,
                       segment.end_ms, segment.transcript, tuple(dict.fromkeys(t.strip() for t in segment.ocr_texts if t.strip())),
                       segment.source_item_ids)
                allowed[key] = segment_identity(segment)
        result = []
        for hit in hits[:MAX_USER_HITS]:
            if not isinstance(hit, VideoEvidenceHit):
                continue
            key = (hit.source_revision, hit.chunk_id, hit.segment_id, hit.start_ms, hit.end_ms,
                   hit.transcript, hit.ocr_texts, hit.source_item_ids)
            if key in allowed:
                result.append((hit, allowed[key]))
        return tuple(result)

    def _record(self, decision, counts, candidates, unique, started):
        covered = sum(n > 0 for n in counts)
        coverage = ("NO_EVIDENCE" if not covered else "ALL_SUBQUERIES_HAVE_CANDIDATES"
                    if covered == len(counts) else "PARTIAL_CANDIDATES")
        observation = RetrievalObservation(decision, len(counts), candidates, unique, coverage,
                                           counts, (time.perf_counter() - started) * 1000)
        callback = _observer.get()
        if callback:
            callback(observation)
        if self.telemetry:
            for name, value in (("subquery_count", len(decision.queries) if decision.route == RetrievalRoute.BOUNDED_MULTI_QUERY else 0),
                                ("retrieval_call_count", len(counts)), ("candidate_count", candidates),
                                ("unique_evidence_count", unique), ("route_latency_ms", observation.route_latency_ms)):
                self.telemetry.observe(name, value)
            for name, value in (("adaptive_route", decision.route.value), ("routing_reason", decision.reason),
                                ("routing_fallback", decision.fallback), ("coverage_status", coverage)):
                self.telemetry.increment(name + "." + value)
        _logger.info("adaptive_route=%s routing_reason=%s routing_fallback=%s subquery_count=%d "
                     "retrieval_call_count=%d candidate_count=%d unique_evidence_count=%d "
                     "coverage_status=%s route_latency_ms=%.2f", decision.route.value, decision.reason,
                     decision.fallback, len(decision.queries) if decision.route == RetrievalRoute.BOUNDED_MULTI_QUERY else 0,
                     len(counts), candidates, unique, coverage, observation.route_latency_ms)
