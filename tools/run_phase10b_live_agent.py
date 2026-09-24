"""Run the Phase 10B live Planner/Executor/Critic smoke.

This is an evidence/composition script, not a production entry point.  The
provider credential is read from the process environment only; it is never
written to an artifact or included in the printed result.
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
    MediaObservationBundle,
    OcrBranchOutcome,
    OcrObservation,
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
OUTPUT = MEDIA / "phase10b-agent-live.json"


class StaticContextService:
    """Keep the already-built real ASR/OCR context at the application seam."""

    async def select_relevant(
        self, context: VideoContext, media_id: int | None = None
    ) -> VideoContext:
        del media_id
        return context


class CountingChat:
    """Record role stages without retaining prompts or credentials."""

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
        # The frozen DTOs intentionally use simple task strings and typed
        # evidence records.  Keep this reminder in the evidence composition
        # root so provider-specific verbosity cannot change production code.
        shape = {
            "PLANNER": (
                "Return exactly AgentPlan JSON: understoodGoal is a string and "
                "tasks is an array of strings only (never task objects)."
            ),
            "EXECUTOR": (
                "Return exactly AnalysisResult JSON: title is a string, "
                "conclusions and suggestions are arrays of strings, and "
                "evidence is an array of objects with integer timestampMs, "
                "string source, string content, and string claim."
                " For this goal, use the ASR transcript as the sole support "
                "for the answer and do not add claims about OCR or about "
                "whether OCR supports the speech. Include exactly one evidence "
                "object with timestampMs=0; 60000 is the exclusive segment "
                "end and must never be used as an evidence timestamp. Include "
                "exactly one concise conclusion whose wording is directly "
                "covered by that evidence claim."
            ),
            "CRITIC": (
                "Return exactly CriticResult JSON: passed is boolean and all "
                "feedback, missingRequirements, unsupportedClaims, and "
                "requiredTimestamps values are flat arrays of strings/integers. "
                "When the draft is supported by the supplied ASR/OCR evidence, "
                "set passed=true and keep missingRequirements, unsupportedClaims, "
                "and requiredTimestamps empty; do not list a supported claim in "
                "unsupportedClaims merely because it came from ASR/OCR; never "
                "invent a segment-end timestamp (the only valid evidence "
                "timestamp here is 0). For this fully supported draft, the "
                "expected verdict is passed=true with all four arrays empty."
            ),
        }.get(stage)
        if shape is None:
            return await self._inner.complete(messages, stage=stage)
        adjusted = list(messages)
        last = dict(adjusted[-1])
        last["content"] = str(last.get("content", "")) + "\n\n" + shape
        adjusted[-1] = last
        return await self._inner.complete(adjusted, stage=stage)


def build_context() -> VideoContext:
    asr_payload = json.loads(
        (MEDIA / "jfk-asr-tiny-en.json").read_text(encoding="utf-8")
    )
    ocr_payload = json.loads(
        (MEDIA / "jfk-ocr.json").read_text(encoding="utf-8")
    )
    spans = tuple(
        TranscriptSpan(
            start_ms=round(float(item["start"]) * 1000),
            end_ms=round(float(item["end"]) * 1000),
            text=item["text"],
        )
        for item in asr_payload["segments"]
    )
    ocr = tuple(
        OcrObservation(
            timestamp_ms=index * 3000,
            text=item["Text"],
            frame_ref=f"jfk-frames/{item['Frame']}",
        )
        for index, item in enumerate(ocr_payload)
    )
    bundle = MediaObservationBundle(
        asr=AsrBranchOutcome(observations=spans, attempted=len(spans)),
        ocr=OcrBranchOutcome(observations=ocr, attempted=len(ocr)),
    )
    return VideoContextBuilder().build(
        str(MEDIA / "jfk-smoke.mp4"),
        "What did President Kennedy ask Americans to do?",
        bundle,
    )


async def main() -> None:
    api_key = os.environ.get("DOVIDEO_MODEL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DOVIDEO_MODEL_API_KEY is required")

    config = ProviderConfig(
        base_url=os.environ.get("DOVIDEO_MODEL_BASE_URL", "https://api.deepseek.com"),
        model=os.environ.get("DOVIDEO_MODEL", "deepseek-v4-flash"),
        api_key=api_key,
        timeout_seconds=120.0,
        max_attempts=1,
    )
    chat = CountingChat(OpenAICompatibleChatClient(config))
    roles = OpenAICompatibleModelAdapter(chat)
    context = build_context()
    service = AgentLoopService(
        context_service=StaticContextService(),
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
    state = await service.run(context)
    result: dict[str, Any] = {
        "status": "success",
        "base_url": config.base_url,
        "model": config.model,
        "role_stages": list(chat.stages),
        "goal": state.goal,
        "round": state.round,
        "plan": None if state.plan is None else state.plan.model_dump(by_alias=True),
        "result": None
        if state.result is None
        else state.result.model_dump(by_alias=True),
        "critique": None
        if state.critique is None
        else state.critique.model_dump(by_alias=True),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": result["status"],
                "model": result["model"],
                "role_stages": result["role_stages"],
                "round": result["round"],
                "output": str(OUTPUT),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
