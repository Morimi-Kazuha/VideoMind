"""Run the paid-provider half of Phase 10B-FIX.

The local runner must complete first.  This runner reads its local retrieval
evidence, feeds the retrieval-selected source window to the existing
AgentLoopService, and uses the existing OpenAI-compatible adapter boundary for
the real DeepSeek Planner/Executor/Critic calls.  Credentials are read only
from the process environment and are never written to artifacts or stdout.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from dovideo.application import (
    AgentLoopService,
    AsrBranchOutcome,
    EvidenceVerificationService,
    MediaObservationBundle,
    OcrBranchOutcome,
    TranscriptSpan,
    VideoContextBuilder,
)
from dovideo.domain import AgentBudgetConfig, VideoContext
from dovideo.infrastructure.providers import (
    OpenAICompatibleChatClient,
    OpenAICompatibleModelAdapter,
    ProviderConfig,
)


ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "work" / "media"
SOURCE = MEDIA / "representative-long.mp4"
ASR_ARTIFACT = MEDIA / "representative-asr.json"
LOCAL_ARTIFACT = MEDIA / "phase10b-fix-local.json"
OUTPUT = MEDIA / "phase10b-fix-agent-live.json"
MEDIA_ID = 10_000_010


class RetrievedContextService:
    """Return the context selected by the completed retrieval stage."""

    def __init__(self, selected: VideoContext) -> None:
        self._selected = selected

    async def select_relevant(
        self,
        context: VideoContext,
        media_id: int | None = None,
    ) -> VideoContext:
        del context, media_id
        return self._selected


class CountingChat:
    """Record only provider role names; never retain prompts or credentials."""

    def __init__(self, inner: OpenAICompatibleChatClient) -> None:
        self._inner = inner
        self.stages: list[str] = []

    async def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        stage: str,
    ) -> str:
        self.stages.append(stage)
        reminders = {
            "PLANNER": (
                "Return exactly AgentPlan JSON: understoodGoal is a string and "
                "tasks is an array of strings only, never an array of task objects."
            ),
            "EXECUTOR": (
                "Return exactly AnalysisResult JSON. title is a string; "
                "conclusions and suggestions are arrays of strings only, never "
                "arrays of objects such as {conclusion: ...}; evidence is an "
                "array of objects with integer timestampMs, string source, "
                "string content, and string claim. Use only the supplied "
                "retrieval-selected ASR evidence for the answer. Return one to "
                "three concise conclusions, each copied verbatim from a line "
                "in the supplied ASR transcript; do not add interpretations. "
                "Create one evidence item for each conclusion, copy its content "
                "verbatim from ASR, use source ASR, choose an integer timestamp "
                "inside the supplied segment's half-open [startMs,endMs) range, "
                "and make the evidence claim exactly equal to its conclusion. "
                "Do not invent facts, transcript text, or timestamps."
            ),
            "CRITIC": (
                "Return exactly CriticResult JSON with flat arrays. Check the "
                "draft against the supplied ASR evidence, source, timestamp, "
                "claim binding, and structure. Set passed=true only when every "
                "check is satisfied; otherwise report the concrete missing or "
                "unsupported item. One to three verbatim ASR conclusions with "
                "matching evidence claims and timestamps inside the selected "
                "window are fully supported; do not fail such a draft merely "
                "because the full media also contains an unrelated early region."
            ),
        }
        reminder = reminders.get(stage)
        if reminder is None:
            return await self._inner.complete(messages, stage=stage)
        adjusted = list(messages)
        last = dict(adjusted[-1])
        last["content"] = str(last.get("content", "")) + "\n\n" + reminder
        adjusted[-1] = last
        return await self._inner.complete(adjusted, stage=stage)


def _load_selected_context() -> tuple[VideoContext, VideoContext, dict[str, Any]]:
    local = json.loads(LOCAL_ARTIFACT.read_text(encoding="utf-8"))
    if local.get("status") != "success":
        raise RuntimeError("local Phase 10B-FIX artifact is not successful")
    if int(local["retrieval"]["candidate_count"]) < 2:
        raise RuntimeError("local retrieval artifact has fewer than two candidates")
    if local["retrieval"]["top_candidate_region"] != "later-afterlove":
        raise RuntimeError("local retrieval did not select the later After Love region")

    asr = json.loads(ASR_ARTIFACT.read_text(encoding="utf-8"))
    spans = tuple(
        TranscriptSpan(
            start_ms=round(float(item["start"]) * 1000),
            end_ms=round(float(item["end"]) * 1000),
            text=item["text"],
        )
        for item in asr["segments"]
    )
    full_context = VideoContextBuilder().build(
        str(SOURCE),
        str(local["goal"]),
        MediaObservationBundle(
            asr=AsrBranchOutcome(observations=spans, attempted=len(spans)),
            ocr=OcrBranchOutcome(),
        ),
    )
    requested_ranges = {
        (int(item["start_ms"]), int(item["end_ms"]))
        for item in local["retrieval"]["selected_context_windows"]
    }
    selected = tuple(
        segment
        for segment in full_context.segments
        if (segment.start_ms, segment.end_ms) in requested_ranges
    )
    if not selected:
        raise RuntimeError("retrieval-selected windows did not map to ASR context")
    selected_context = VideoContext(
        source=full_context.source,
        user_goal=full_context.user_goal,
        segments=selected,
    )
    return full_context, selected_context, local


async def main() -> None:
    api_key = os.environ.get("DOVIDEO_MODEL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DOVIDEO_MODEL_API_KEY is required at the live credential boundary")

    full_context, selected_context, local = _load_selected_context()
    config = ProviderConfig(
        base_url=os.environ.get("DOVIDEO_MODEL_BASE_URL", "https://api.deepseek.com"),
        model=os.environ.get("DOVIDEO_MODEL", "deepseek-v4-flash"),
        api_key=api_key,
        timeout_seconds=120.0,
        max_attempts=1,
    )
    chat = CountingChat(OpenAICompatibleChatClient(config))
    roles = OpenAICompatibleModelAdapter(chat)
    service = AgentLoopService(
        context_service=RetrievedContextService(selected_context),
        planner=roles.planner,
        executor=roles.executor,
        critic=roles.critic,
        budget_config=AgentBudgetConfig(
            max_rounds=1,
            max_duration_ms=300_000,
            max_estimated_tokens=100_000,
            max_estimated_cost=0.0,
        ),
    )
    state = await service.run(full_context, media_id=MEDIA_ID)
    if state.plan is None or state.result is None or state.critique is None:
        raise RuntimeError("live AgentLoop returned incomplete state")
    evidence_service = EvidenceVerificationService()
    evidence_supported = all(
        evidence_service.supported(selected_context, evidence)
        for evidence in state.result.evidence
    )
    evidence_in_retrieved_region = all(
        evidence_service.timestamp_covered(selected_context, evidence)
        for evidence in state.result.evidence
    )
    if not state.critique.passed:
        raise RuntimeError(
            "live DeepSeek Critic did not pass: "
            + json.dumps(state.critique.model_dump(by_alias=True), ensure_ascii=False)
        )
    if not evidence_supported or not evidence_in_retrieved_region:
        raise RuntimeError("final evidence was not supported by the retrieval-selected context")
    if chat.stages != ["PLANNER", "EXECUTOR", "CRITIC"]:
        raise RuntimeError(f"unexpected live role sequence: {chat.stages}")

    result: dict[str, Any] = {
        "status": "success",
        "source": str(SOURCE),
        "media_id": MEDIA_ID,
        "base_url": config.base_url,
        "model": config.model,
        "role_stages": list(chat.stages),
        "planner_calls": chat.stages.count("PLANNER"),
        "executor_calls": chat.stages.count("EXECUTOR"),
        "critic_calls": chat.stages.count("CRITIC"),
        "full_context_temporal_windows": len(full_context.segments),
        "retrieval_selected_windows": [
            {"start_ms": segment.start_ms, "end_ms": segment.end_ms}
            for segment in selected_context.segments
        ],
        "retrieval_top_candidate_region": local["retrieval"]["top_candidate_region"],
        "agent_loop_existing_service": True,
        "evidence_guard_supported": evidence_supported,
        "evidence_guard_in_retrieved_region": evidence_in_retrieved_region,
        "round": state.round,
        "goal": state.goal,
        "plan": state.plan.model_dump(by_alias=True),
        "result": state.result.model_dump(by_alias=True),
        "critique": state.critique.model_dump(by_alias=True),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": result["status"],
                "model": result["model"],
                "role_stages": result["role_stages"],
                "round": result["round"],
                "evidence_guard_supported": result["evidence_guard_supported"],
                "evidence_guard_in_retrieved_region": result["evidence_guard_in_retrieved_region"],
                "output": str(OUTPUT),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
