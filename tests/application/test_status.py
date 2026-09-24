from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from dovideo.application import (
    AnalysisRequest,
    AnalysisStatusQuery,
    DispatchDisposition,
    MediaRef,
    OcrObservation,
    ReadableSource,
    TaskKey,
    TraceContext,
    TranscriptSpan,
)
from dovideo.application.ports import (
    EmbeddingPort,
    TranscriptionPort,
)
from dovideo.domain import (
    AgentState,
    AnalysisMode,
    AnalysisResult,
    CriticResult,
    TaskStage,
    TaskStatusState,
)


class InMemoryStatusCheckpoint:
    def __init__(self) -> None:
        self.results: dict[TaskKey, AgentState] = {}
        self.stages: dict[TaskKey, TaskStage] = {}
        self.load_result_calls: list[TaskKey] = []
        self.load_stage_calls: list[TaskKey] = []

    async def load_result(self, key: TaskKey) -> AgentState | None:
        self.load_result_calls.append(key)
        return self.results.get(key)

    async def load_stage(self, key: TaskKey) -> TaskStage | None:
        self.load_stage_calls.append(key)
        return self.stages.get(key)


class InMemoryActivity:
    def __init__(self) -> None:
        self.active: set[TaskKey] = set()
        self.calls: list[TaskKey] = []

    async def is_active(self, key: TaskKey) -> bool:
        self.calls.append(key)
        return key in self.active


def query_fixture() -> tuple[AnalysisStatusQuery, InMemoryStatusCheckpoint, InMemoryActivity]:
    checkpoint = InMemoryStatusCheckpoint()
    activity = InMemoryActivity()
    return AnalysisStatusQuery(checkpoint, activity), checkpoint, activity


@pytest.mark.asyncio
async def test_terminal_result_wins_over_active_and_stage() -> None:
    query, checkpoint, activity = query_fixture()
    key = TaskKey(7, "goal")
    checkpoint.results[key] = AgentState(
        goal="goal",
        result=AnalysisResult(title="done", conclusions=["supported"]),
        critique=CriticResult(passed=True),
        round=1,
    )
    checkpoint.stages[key] = TaskStage.EXECUTOR_STARTED
    activity.active.add(key)

    status = await query.current(7, "goal")

    assert status.state is TaskStatusState.COMPLETED
    assert status.message == "任务完成"
    assert status.result.startswith("## done")
    # The short circuit mirrors AnalysisStatusService: stage/activity are not
    # consulted after a terminal result is found.
    assert checkpoint.load_stage_calls == []
    assert activity.calls == []


@pytest.mark.asyncio
async def test_terminal_result_without_passing_critic_keeps_warning() -> None:
    query, checkpoint, _ = query_fixture()
    key = TaskKey(8, "goal")
    checkpoint.results[key] = AgentState(
        goal="goal",
        result=AnalysisResult(title="done", conclusions=["draft"]),
        critique=CriticResult(passed=False),
    )

    status = await query.current(8, "goal")

    assert status.state is TaskStatusState.COMPLETED
    assert "人工核验" in status.message
    assert status.result.startswith("> **结果提示：**")


@pytest.mark.asyncio
async def test_active_without_stage_is_queued() -> None:
    query, _, activity = query_fixture()
    activity.active.add(TaskKey(1, "goal"))

    status = await query.current(1, "goal")

    assert status.state is TaskStatusState.QUEUED
    assert status.message == "任务已排队"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "message"),
    [
        (TaskStage.VIDEO_CONTEXT, "正在解析视频语音和关键画面"),
        (TaskStage.CONTEXT_COMPLETED, "正在解析视频语音和关键画面"),
        (TaskStage.CHUNKS_COMPLETED, "正在检索与目标相关的视频证据"),
        (TaskStage.PLAN_COMPLETED, "Planner 已完成任务拆解"),
        (TaskStage.EXECUTOR_STARTED, "Executor 正在生成结构化产物"),
        (TaskStage.EXECUTOR_COMPLETED, "Executor 正在生成结构化产物"),
        (TaskStage.CRITIC_STARTED, "Critic 正在核验结论和证据"),
        (TaskStage.CRITIC_RETRY_REQUIRED, "正在根据 Critic 反馈补充证据"),
        (TaskStage.EVIDENCE_REFRESHED, "正在根据 Critic 反馈补充证据"),
        (TaskStage.RETRYING, "任务执行异常，正在自动重试"),
        (TaskStage.ANALYSIS_COMPLETED, "正在分析视频"),
    ],
)
async def test_active_stage_is_processing_with_java_message(stage: TaskStage, message: str) -> None:
    query, checkpoint, activity = query_fixture()
    key = TaskKey(2, "goal")
    checkpoint.stages[key] = stage
    activity.active.add(key)

    status = await query.current(2, "goal")

    assert status.state is TaskStatusState.PROCESSING
    assert status.message == message


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [TaskStage.BUDGET_EXHAUSTED, TaskStage.FAILED, TaskStage.DEAD_LETTERED])
async def test_inactive_failure_stages_are_failed(stage: TaskStage) -> None:
    query, checkpoint, _ = query_fixture()
    checkpoint.stages[TaskKey(3, "goal")] = stage

    status = await query.current(3, "goal")

    assert status.state is TaskStatusState.FAILED
    if stage is TaskStage.BUDGET_EXHAUSTED:
        assert status.message == "Agent 已达到本次任务预算，请调整目标后重试"
    else:
        assert status.message == "分析失败，请稍后重试"


@pytest.mark.asyncio
async def test_inactive_non_failure_stage_is_not_started() -> None:
    query, checkpoint, _ = query_fixture()
    checkpoint.stages[TaskKey(4, "goal")] = TaskStage.COMPLETED

    status = await query.current(4, "goal")

    assert status.state is TaskStatusState.NOT_STARTED
    assert status.message == "尚未提交分析任务"


@pytest.mark.asyncio
async def test_general_mode_is_default_and_explicit_mode_is_part_of_task_key() -> None:
    query, _, activity = query_fixture()
    activity.active.add(TaskKey(5, "goal", AnalysisMode.LEARNING))

    default_status = await query.current(5, "goal")
    learning_status = await query.current(5, "goal", "learning")

    assert default_status.state is TaskStatusState.NOT_STARTED
    assert learning_status.state is TaskStatusState.QUEUED
    assert activity.calls == [
        TaskKey(5, "goal", AnalysisMode.GENERAL),
        TaskKey(5, "goal", AnalysisMode.LEARNING),
    ]


@pytest.mark.asyncio
async def test_stage_query_uses_general_when_mode_is_omitted() -> None:
    query, checkpoint, _ = query_fixture()
    key = TaskKey(6, "goal")
    checkpoint.stages[key] = TaskStage.RETRIEVAL

    assert await query.stage(6, "goal") is TaskStage.RETRIEVAL
    assert checkpoint.load_stage_calls == [key]


def test_application_values_are_frozen_and_typed() -> None:
    key = TaskKey(9, "  goal ", None)
    assert key.goal == "goal"
    assert key.mode is AnalysisMode.GENERAL
    assert hash(key) == hash(TaskKey(9, "goal", AnalysisMode.GENERAL))
    with pytest.raises(FrozenInstanceError):
        key.goal = "changed"  # type: ignore[misc]

    media = MediaRef(9, "video.mp4", filename="video.mp4")
    request = AnalysisRequest(media, "goal", AnalysisMode.REVIEW, request_id="req-1")
    assert request.task_key == TaskKey(9, "goal", AnalysisMode.REVIEW)
    assert request.task_key.mode is AnalysisMode.REVIEW
    assert ReadableSource("file:///video.mp4").uri.startswith("file:")
    assert TranscriptSpan(0, 1, " text ").text == "text"
    assert OcrObservation(2, "ocr", "frame.jpg").frame_ref == "frame.jpg"
    trace = TraceContext("trace-1", request.task_key)
    assert trace.task_key == request.task_key


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TaskKey(1, " "),
        lambda: MediaRef(1, " "),
        lambda: ReadableSource(" "),
        lambda: TranscriptSpan(2, 1),
        lambda: OcrObservation(-1),
    ],
)
def test_application_value_boundaries_reject_invalid_input(factory) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, source: ReadableSource, *, trace_id: str | None = None) -> tuple[TranscriptSpan, ...]:
        self.calls += 1
        return (TranscriptSpan(0, 1, source.uri),)


class FakeEmbedder:
    async def embed(self, text: str) -> tuple[float, ...]:
        return (float(len(text)),)


@pytest.mark.asyncio
async def test_async_ports_run_with_fakes_without_network_or_provider_imports() -> None:
    transcriber: TranscriptionPort = FakeTranscriber()
    embedder: EmbeddingPort = FakeEmbedder()

    spans = await transcriber.transcribe(ReadableSource("memory://video"), trace_id="trace")
    vector = await embedder.embed("goal")

    assert spans == (TranscriptSpan(0, 1, "memory://video"),)
    assert vector == (4.0,)

