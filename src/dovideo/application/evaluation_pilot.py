"""V1 pilot evaluation-only sidecar and scoring. Historical X3 stays frozen.

Structural resolution and literal variants are diagnostics, never semantic truth.
No production prompt or Agent adapter is imported by this module.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dovideo.domain import VideoContext
from .evaluation_contracts import EvaluationDataset
from .evaluation_runner import EvaluationRetrievedEvidence, calculate_retrieval_metrics, evidence_matches


class PilotFactAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    fact_id: str
    sufficient_refs: tuple[str, ...]
    related_only_refs: tuple[str, ...] = ()
    support_rule: Literal["ALL", "ANY"] = "ALL"
    verification_notes: str
    undetermined_premises: tuple[str, ...] = ()


class PilotCaseAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str
    answerability: Literal["ANSWERABLE", "NO_ANSWER_IN_VIDEO", "UNDETERMINED"]
    annotation_status: Literal["DRAFT", "NEEDS_REVIEW", "HUMAN_APPROVED"] = "NEEDS_REVIEW"
    annotation_notes: str
    fact_annotations: tuple[PilotFactAnnotation, ...] = ()
    underlying_fact_groups: tuple[str, ...] = ()
    absence_review_scope: str | None = None
    human_reviewer: str | None = None
    human_review_record: str | None = None

    @model_validator(mode="after")
    def human_identity_required(self):
        if self.annotation_status == "HUMAN_APPROVED" and not (self.human_reviewer and self.human_review_record):
            raise ValueError("human approval requires an explicit reviewer and review record")
        if self.answerability == "NO_ANSWER_IN_VIDEO" and not self.absence_review_scope:
            raise ValueError("source absence requires a full-source review scope")
        return self


class PilotAnnotations(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sidecar_version: Literal["v1-pilot-annotations-v1"] = "v1-pilot-annotations-v1"
    dataset_version: str
    dataset_digest: str
    source_revision: str
    media_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generator_identity: str
    verifier_identity: str
    verification_independence: str
    frozen: bool = False
    cases: tuple[PilotCaseAnnotation, ...]

    @model_validator(mode="after")
    def freeze_requires_review(self):
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("duplicate sidecar case")
        if self.frozen and any(c.annotation_status != "HUMAN_APPROVED" for c in self.cases):
            raise ValueError("freeze requires every case to be human approved")
        return self


def validate_pilot(dataset: EvaluationDataset, annotations: PilotAnnotations,
                   context: VideoContext, *, duration_ms: int) -> dict:
    """Resolve exact authoritative source identities, timestamps and annotation links.

This establishes resolvability, NOT the semantic sufficiency of a cited source.
Media/context content digests are checked separately by the tool before this call.
"""
    if (dataset.dataset_version != annotations.dataset_version or
        dataset.dataset_digest != annotations.dataset_digest or
        context.source_revision != annotations.source_revision or
        dataset.source_revision_set != (context.source_revision,)):
        raise ValueError("dataset/sidecar/context identity mismatch")
    if {c.case_id for c in dataset.cases} != {c.case_id for c in annotations.cases}:
        raise ValueError("sidecar must cover exactly the dataset cases")
    by_case = {c.case_id: c for c in annotations.cases}
    by_item = {o.source_item.source_item_id: o for o in context.observations}
    if len(by_item) != len(context.observations):
        raise ValueError("duplicate source item identity")
    counts = {"cases": len(dataset.cases), "resolved_evidence_refs": 0,
              "answerable": 0, "no_answer": 0, "human_approved": 0,
              "structurally_valid_cases": 0, "semantic_gold_verified": "NOT_MEASURED"}
    for case in dataset.cases:
        meta = by_case[case.case_id]
        if case.media_ref != context.source:
            raise ValueError("media_ref must resolve to the frozen context")
        if case.execution_input().keys() != {"media_ref", "query", "mode"}:
            raise ValueError("gold leaked into execution projection")
        refs = {r.ref_id: r for r in case.expected_evidence_refs}
        if None in refs or len(refs) != len(case.expected_evidence_refs):
            raise ValueError("pilot refs require unique ref_id")
        if meta.answerability == "ANSWERABLE":
            counts["answerable"] += 1
            if not (refs and case.required_facts and case.reference_answer):
                raise ValueError("answerable case must have facts, answer and exact evidence")
        elif meta.answerability == "NO_ANSWER_IN_VIDEO":
            counts["no_answer"] += 1
            if refs or case.required_facts or case.expected_temporal_regions:
                raise ValueError("no-answer must not contain fabricated positive gold")
        if {f.fact_id for f in case.required_facts} != {f.fact_id for f in meta.fact_annotations}:
            raise ValueError("fact support annotations must cover every required fact")
        for fact in meta.fact_annotations:
            linked = set(next(f for f in case.required_facts if f.fact_id == fact.fact_id).linked_evidence_refs)
            if not fact.sufficient_refs or not set(fact.sufficient_refs) <= linked:
                raise ValueError("facts require sufficient refs linked by X3 RequiredFact")
            if not set(fact.related_only_refs) <= refs.keys():
                raise ValueError("related refs must resolve")
            if set(fact.related_only_refs) & set(fact.sufficient_refs):
                raise ValueError("related-only evidence cannot count as sufficient")
        for ref in refs.values():
            observation = by_item.get(ref.source_item_id)
            if ref.match_level.value != "SOURCE_ITEM" or observation is None:
                raise ValueError("pilot gold must resolve an exact SourceItem")
            item = observation.source_item
            if (item.source_revision != case.source_revision or item.segment_id != ref.segment_id or
                item.source_type != ref.source_type.value or item.timestamp_ms != ref.timestamp_ms):
                raise ValueError("gold source identity or timestamp mismatch")
            if ref.start_ms != item.timestamp_ms or ref.end_ms != (item.end_ms or item.timestamp_ms + 1):
                raise ValueError("gold range must match original source observation")
            if not 0 <= item.timestamp_ms < duration_ms:
                raise ValueError("gold evidence is outside the actual media")
            if item.end_ms and item.end_ms > duration_ms + 500:
                raise ValueError("gold ASR span exceeds actual media")
            counts["resolved_evidence_refs"] += 1
        for region in case.expected_temporal_regions:
            if region.end_ms > duration_ms or region.start_ms < 0:
                raise ValueError("gold temporal region exceeds actual media")
        counts["human_approved"] += int(meta.annotation_status == "HUMAN_APPROVED")
        counts["structurally_valid_cases"] += 1
    return counts


def literal_variant_diagnostic(facts, answer: str) -> dict:
    """Explicit equivalent phrase matches; does NOT handle negation or entailment."""
    def normalized(text):
        return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())
    output = normalized(answer)
    hits = {f.fact_id: any(normalized(v) and normalized(v) in output
        for v in (f.description, *f.acceptable_variants)) for f in facts if f.required}
    return {"method": "v1-literal-variant-diagnostic", "per_fact": hits,
        "matched_fraction": sum(hits.values()) / len(hits) if hits else None,
        "semantic_fact_coverage": "NOT_MEASURED",
        "limitation": "Literal diagnostic only; negation, ambiguity, entailment require a recorded semantic review."}


def score_pilot_retrieval(case, meta: PilotCaseAnnotation, retrieved) -> dict:
    retrieved = tuple(retrieved)
    if meta.answerability == "NO_ANSWER_IN_VIDEO":
        return {"method": "v1-pilot-metrics-v1", "retrieved_count": len(retrieved),
            "ranks": [r.rank for r in retrieved], "retrieval_metrics": "NOT_APPLICABLE",
            "source_absence": "DRAFT_PENDING_HUMAN_REVIEW",
            "refusal_correctness": "NOT_MEASURED",
            "note": "Retrieval returns related candidates even if the source lacks the answer; no Agent was run."}
    metrics = calculate_retrieval_metrics(case.expected_evidence_refs, retrieved,
        k_values=(1, 3, 5), expected_temporal_regions=case.expected_temporal_regions,
        source_revision=case.source_revision)
    refs = {r.ref_id: r for r in case.expected_evidence_refs}
    fact_coverage = {}
    for k in (1, 3, 5):
        ranked = sorted(retrieved, key=lambda r: r.rank or 0)[:k]
        hits = {}
        for fact in meta.fact_annotations:
            supported = [any(evidence_matches(refs[ref], candidate) for candidate in ranked)
                         for ref in fact.sufficient_refs]
            hits[fact.fact_id] = all(supported) if fact.support_rule == "ALL" else any(supported)
        fact_coverage[str(k)] = {"per_fact": hits,
            "coverage": sum(hits.values()) / len(hits) if hits else None,
            "all_facts_retrieved": all(hits.values()) if hits else None}
    return {"method": "v1-pilot-metrics-v1", "retrieved_count": len(retrieved),
        "recall_at_k": metrics.recall_at_k, "precision_at_k": metrics.precision_at_k,
        "mrr": metrics.mrr, "temporal_hit": metrics.temporal_hit,
        "temporal_coverage": metrics.temporal_coverage,
        "fact_evidence_recall_at_k": fact_coverage,
        "semantic_fact_coverage": "NOT_MEASURED", "factual_correctness": "NOT_MEASURED",
        "answer_completeness": "NOT_MEASURED", "unsupported_new_facts": "NOT_MEASURED",
        "citation_precision_recall": "NOT_MEASURED", "claim_entailment": "NOT_MEASURED",
        "timestamp_accuracy": "NOT_MEASURED",
        "note": "SourceItem availability inside returned minute windows; no pinpoint citation or answer quality claim."}
