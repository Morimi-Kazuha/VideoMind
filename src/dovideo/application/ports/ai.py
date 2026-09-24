"""Role-specific ASR/OCR/LLM/embedding ports."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from dovideo.domain import (
    AgentPlan,
    AnalysisResult,
    ChunkSummary,
    CriticResult,
    VideoContext,
    VideoRetrievalIntent,
    VideoSegment,
)

from ..value_objects import OcrObservation, ReadableSource, TranscriptSpan

if TYPE_CHECKING:
    from ..tool_contracts import ExecutorTurn, ToolResult


class TranscriptionPort(Protocol):
    """Convert a readable media source into timestamped ASR spans."""

    async def transcribe(
        self,
        source: ReadableSource,
        *,
        trace_id: str | None = None,
    ) -> tuple[TranscriptSpan, ...]:
        ...


AsrPort = TranscriptionPort


class OcrPort(Protocol):
    """Recognize text from one timestamped visual source/frame."""

    async def recognize(
        self,
        source: ReadableSource,
        *,
        timestamp_ms: int,
        trace_id: str | None = None,
    ) -> OcrObservation:
        ...


class AudioSegmentTranscriptionPort(Protocol):
    """Recognize exactly one already-segmented audio file."""

    async def transcribe_segment(
        self,
        audio_path: Path,
        *,
        trace_id: str | None = None,
    ) -> str | None:
        ...


class FrameOcrPort(Protocol):
    """Recognize exactly one extracted frame."""

    async def recognize_frame(
        self,
        image_path: Path,
        *,
        trace_id: str | None = None,
    ) -> str | None:
        ...


class PlannerPort(Protocol):
    """Planner role: initial plan, repair, and Critic-directed replan."""

    async def plan(self, context: VideoContext, *, instruction: str = "") -> AgentPlan:
        ...

    async def repair_plan(
        self,
        context: VideoContext,
        invalid_plan: AgentPlan,
        *,
        instruction: str = "",
    ) -> AgentPlan:
        ...

    async def replan(
        self,
        context: VideoContext,
        current_plan: AgentPlan,
        critique: CriticResult,
        *,
        instruction: str = "",
    ) -> AgentPlan:
        ...


class ExecutorPort(Protocol):
    """Executor role: produce one structured analysis draft."""

    async def execute(
        self,
        context: VideoContext,
        plan: AgentPlan,
        previous_critique: CriticResult | None = None,
        *,
        instruction: str = "",
    ) -> AnalysisResult:
        ...


class ExecutorTurnPort(Protocol):
    """Tool-aware Executor boundary kept separate from ``ExecutorPort``."""

    async def execute_turn(
        self,
        context: VideoContext,
        plan: AgentPlan,
        previous_critique: CriticResult | None = None,
        *,
        instruction: str = "",
    ) -> ExecutorTurn:
        ...

    async def continue_after_tool(
        self,
        context: VideoContext,
        plan: AgentPlan,
        tool_result: ToolResult,
        previous_critique: CriticResult | None = None,
        *,
        instruction: str = "",
        tools_available: bool = True,
    ) -> ExecutorTurn:
        ...


class CriticPort(Protocol):
    """Critic role: inspect a draft without rewriting it."""

    async def critique(
        self,
        context: VideoContext,
        plan: AgentPlan,
        result: AnalysisResult,
        *,
        instruction: str = "",
    ) -> CriticResult:
        ...


class RetrievalPlannerPort(Protocol):
    """Rewrite a user goal into semantic/visual retrieval clues."""

    async def plan_retrieval(self, goal: str) -> VideoRetrievalIntent:
        ...


class ChunkSummaryPort(Protocol):
    """Summarize one chunk's raw segments for retrieval."""

    async def summarize_chunk(self, segments: Sequence[VideoSegment]) -> ChunkSummary:
        """Return a domain ``ChunkSummary`` for raw segment inputs."""
        ...


class EmbeddingPort(Protocol):
    """Generate one vector for one text input, matching the Java utility."""

    async def embed(self, text: str) -> tuple[float, ...]:
        ...


__all__ = [
    "AudioSegmentTranscriptionPort",
    "AsrPort",
    "ChunkSummaryPort",
    "CriticPort",
    "EmbeddingPort",
    "ExecutorPort",
    "ExecutorTurnPort",
    "FrameOcrPort",
    "OcrPort",
    "PlannerPort",
    "RetrievalPlannerPort",
    "TranscriptionPort",
]
