"""Original baseline Agent evaluation, kept separate from X3 enhancements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dovideo.domain import AgentFeedback, AgentState, AnalysisMode, VideoContext

from .evidence import EvidenceVerificationService


class AgentEvaluationService:
    """Implement the original seven-field evaluation metric map."""

    def __init__(self, verifier: EvidenceVerificationService | None = None) -> None:
        self._verifier = verifier or EvidenceVerificationService()

    def evaluate(
        self,
        context: VideoContext | None,
        state: AgentState | None,
        feedback: tuple[AgentFeedback, ...] = (),
    ) -> dict[str, Any]:
        result = None if state is None else state.result
        evidence = () if result is None else result.evidence
        conclusions = () if result is None else result.conclusions
        structured_valid = bool(
            result is not None
            and result.title.strip()
            and result.conclusions
            and result.evidence
        )
        timestamp_rate = (
            sum(self._verifier.timestamp_covered(context, item) for item in evidence)
            / len(evidence)
            if context is not None and evidence
            else 0
        )
        support_rate = (
            sum(self._verifier.supported(context, item) for item in evidence)
            / len(evidence)
            if context is not None and evidence
            else 0
        )
        claim_rate = (
            sum(
                any(self._verifier.supports_claim(context, claim, item) for item in evidence)
                for claim in conclusions
            )
            / len(conclusions)
            if context is not None and conclusions
            else 0
        )
        rated = [item for item in feedback if item.rating is not None]
        return {
            "structuredValid": structured_valid,
            "timestampCoverageRate": timestamp_rate,
            "evidenceSupportRate": support_rate,
            "claimEvidenceSupportRate": claim_rate,
            "criticPassed": bool(
                state is not None
                and state.critique is not None
                and state.critique.passed
            ),
            "userAcceptanceRate": (
                sum(item.rating > 0 for item in rated) / len(rated) if rated else 0
            ),
            "feedbackSamples": len(feedback),
        }

    def evaluate_task(
        self,
        context: VideoContext | None,
        state: AgentState | None,
        feedback: tuple[AgentFeedback, ...] = (),
    ) -> dict[str, Any]:
        return self.evaluate(context, state, feedback)


@dataclass(frozen=True, slots=True)
class GoldenEvaluationTask:
    """One explicit offline baseline task, matching the Java runner shape."""

    name: str
    context: VideoContext
    expected_keywords: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("evaluation task name is required")


class OfflineAgentEvaluationRunner:
    """Run supplied golden tasks through the existing AgentLoop boundary."""

    def __init__(
        self,
        agent_loop: Any,
        evaluation: AgentEvaluationService | None = None,
    ) -> None:
        self._agent_loop = agent_loop
        self._evaluation = evaluation or AgentEvaluationService()

    async def run(self, tasks: tuple[GoldenEvaluationTask, ...] | list[GoldenEvaluationTask]):
        results: list[dict[str, Any]] = []
        for task in tasks:
            try:
                state = await self._agent_loop.run(task.context)
                metrics = self._evaluation.evaluate_task(task.context, state)
                output = state.result.to_markdown() if state.result is not None else ""
                coverage = _keyword_coverage(output, task.expected_keywords)
                success = bool(
                    metrics["structuredValid"]
                    and metrics["claimEvidenceSupportRate"] >= 0.8
                    and coverage >= 0.8
                )
                results.append(
                    {
                        "name": task.name,
                        "success": success,
                        "keywordCoverage": coverage,
                        "metrics": metrics,
                    }
                )
            except Exception:
                results.append(
                    {
                        "name": task.name,
                        "success": False,
                        "keywordCoverage": 0.0,
                        "metrics": {},
                    }
                )
        return tuple(results)


def _keyword_coverage(output: str, expected: tuple[str, ...]) -> float:
    if not expected:
        return 1.0
    normalized = output.casefold()
    return sum(keyword.casefold() in normalized for keyword in expected) / len(expected)


__all__ = [
    "AgentEvaluationService",
    "GoldenEvaluationTask",
    "OfflineAgentEvaluationRunner",
]
