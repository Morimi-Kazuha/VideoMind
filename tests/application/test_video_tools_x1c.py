from __future__ import annotations

import json
from collections import Counter

import pytest

from dovideo.application import (
    AgentBudgetConfig,
    AgentLoopService,
    ExecutorTurn,
    ExecutorTurnKind,
    MAX_TOOL_RESULT_BYTES,
    ModelToolRequest,
    TaskKey,
    ToolExecutionContext,
    ToolPolicy,
    ToolPolicyContext,
    ToolResultStatus,
    ToolRegistry,
    VideoReadOnlyToolExecutor,
    VideoSegmentResolver,
    VideoContextWindowResolver,
)
from dovideo.application.errors import DeadlineExceededError
from dovideo.domain import (
    AgentPlan,
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    CriticResult,
    VideoContext,
    VideoEvidenceHit,
    VideoSegment,
)


def _context(*segments: VideoSegment, goal: str = "explain") -> VideoContext:
    return VideoContext(
        source="memory://authorized-video",
        user_goal=goal,
        segments=segments,
    )


def _segment(
    start_ms: int,
    end_ms: int,
    transcript: str = "text",
    ocr_texts: tuple[str, ...] = (),
) -> VideoSegment:
    return VideoSegment(
        start_ms=start_ms,
        end_ms=end_ms,
        transcript=transcript,
        ocr_texts=ocr_texts,
    )


def _execution_context(context: VideoContext, media_id: int = 7) -> ToolExecutionContext:
    policy_context = ToolPolicyContext(
        task_key=TaskKey(media_id, context.user_goal, AnalysisMode.GENERAL),
        media_id=media_id,
        mode=AnalysisMode.GENERAL,
        media_duration_ms=max(
            (segment.end_ms for segment in context.segments),
            default=0,
        ),
    )
    return ToolExecutionContext.from_policy_context(policy_context, context)


def _call(
    tool_name: str,
    arguments: dict[str, object],
    call_id: str = "tool-call-1",
):
    request = ModelToolRequest(tool_name=tool_name, arguments=arguments)
    return ToolRegistry().create_call(request, call_id=call_id)


def _hit(start_ms: int, end_ms: int, text: str = "hit") -> VideoEvidenceHit:
    return VideoEvidenceHit(
        start_ms=start_ms,
        end_ms=end_ms,
        source="ASR",
        snippet=text,
        transcript=text,
        ocr_texts=("slide",),
    )


class SearchServiceFake:
    def __init__(
        self,
        hits: tuple[VideoEvidenceHit, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.hits = hits
        self.error = error
        self.calls: list[tuple[int, VideoContext, int | None]] = []

    async def search_evidence(
        self,
        media_id: int,
        context: VideoContext,
        *,
        limit: int | None = None,
    ) -> tuple[VideoEvidenceHit, ...]:
        self.calls.append((media_id, context, limit))
        if self.error is not None:
            raise self.error
        # Deliberately return all configured hits so the concrete executor
        # proves that its own deterministic projection bound is active.
        return self.hits


@pytest.mark.asyncio
async def test_search_evidence_reuses_current_context_query_and_limit() -> None:
    source_context = _context(_segment(0, 1_000, "authoritative"))
    search = SearchServiceFake((_hit(0, 1_000), _hit(1_000, 2_000)))
    executor = VideoReadOnlyToolExecutor(search_service=search)
    call = _call(
        "video.search_evidence",
        {"query": "needle", "limit": 1},
    )

    result = await executor.execute(
        call,
        _execution_context(source_context),
        remaining_deadline=10.0,
    )

    assert result.status is ToolResultStatus.TRUNCATED
    assert result.truncated
    assert result.payload["found"] is True
    assert len(result.payload["hits"]) == 1
    assert search.calls[0][0] == 7
    assert search.calls[0][1].user_goal == "needle"
    assert search.calls[0][1].segments == source_context.segments
    assert search.calls[0][2] == 1


@pytest.mark.asyncio
async def test_search_evidence_empty_result_is_success_and_contains_no_raw_state() -> None:
    context = _context(_segment(0, 1_000, "authoritative"))
    search = SearchServiceFake()
    executor = VideoReadOnlyToolExecutor(search_service=search)

    result = await executor.execute(
        _call("video.search_evidence", {"query": "missing", "limit": 8}),
        _execution_context(context),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.payload == {
        "found": False,
        "query": "missing",
        "limit": 8,
        "count": 0,
        "hits": [],
    }
    assert "embedding" not in json.dumps(result.payload)


@pytest.mark.asyncio
async def test_search_evidence_projection_is_bounded_and_explicitly_truncated() -> None:
    long_text = "证据" * 2_000
    hits = tuple(_hit(index * 1_000, index * 1_000 + 900, long_text) for index in range(10))
    executor = VideoReadOnlyToolExecutor(search_service=SearchServiceFake(hits))

    result = await executor.execute(
        _call("video.search_evidence", {"query": "q", "limit": 8}),
        _execution_context(_context(_segment(0, 10_000))),
    )

    assert result.status is ToolResultStatus.TRUNCATED
    assert result.truncated
    assert len(result.payload["hits"]) == 8
    assert len(result.payload["hits"][0]["snippet"]) == 512
    payload_bytes = len(
        json.dumps(result.payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    assert payload_bytes <= MAX_TOOL_RESULT_BYTES
    assert "embedding" not in json.dumps(result.payload)


@pytest.mark.asyncio
async def test_search_backend_failure_is_safe_failed_result_without_raw_exception() -> None:
    context = _context(_segment(0, 1_000))
    executor = VideoReadOnlyToolExecutor(
        search_service=SearchServiceFake(error=RuntimeError("connection secret"))
    )

    result = await executor.execute(
        _call("video.search_evidence", {"query": "q", "limit": 1}),
        _execution_context(context),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.payload is None
    assert "connection secret" not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timestamp_ms", "expected"),
    (
        (0, "first"),
        (500, "first"),
        (1_000, None),
        (1_500, "second"),
        (2_000, None),
    ),
)
async def test_get_segment_uses_deterministic_half_open_containment(
    timestamp_ms: int,
    expected: str | None,
) -> None:
    context = _context(
        _segment(0, 1_000, "first"),
        _segment(1_500, 2_000, "second"),
    )
    executor = VideoReadOnlyToolExecutor()

    result = await executor.execute(
        _call("video.get_segment", {"timestamp_ms": timestamp_ms}),
        _execution_context(context),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.payload["found"] is (expected is not None)
    if expected is None:
        assert result.payload["segment"] is None
        assert result.payload["segments"] == []
    else:
        assert result.payload["segment"]["transcript"] == expected
        assert len(result.payload["segments"]) == 1


@pytest.mark.asyncio
async def test_get_segment_preserves_deterministic_overlapping_matches() -> None:
    context = _context(
        _segment(500, 1_500, "later-start"),
        _segment(0, 1_000, "earlier-start"),
    )
    executor = VideoReadOnlyToolExecutor()

    result = await executor.execute(
        _call("video.get_segment", {"timestamp_ms": 700}),
        _execution_context(context),
    )

    assert result.payload["found"] is True
    assert [item["transcript"] for item in result.payload["segments"]] == [
        "earlier-start",
        "later-start",
    ]
    assert result.payload["segment"] is None


@pytest.mark.asyncio
async def test_context_window_clips_and_includes_interval_overlaps_in_order() -> None:
    context = _context(
        _segment(0, 1_000, "zero"),
        _segment(2_000, 3_000, "middle"),
        _segment(5_000, 7_000, "end"),
    )
    executor = VideoReadOnlyToolExecutor()

    centered = await executor.execute(
        _call(
            "video.get_context_window",
            {"timestamp_ms": 2_500, "before_ms": 1_000, "after_ms": 2_000},
        ),
        _execution_context(context),
    )
    at_start = await executor.execute(
        _call(
            "video.get_context_window",
            {"timestamp_ms": 0, "before_ms": 1_000, "after_ms": 1_000},
            call_id="tool-call-2",
        ),
        _execution_context(context),
    )
    at_end = await executor.execute(
        _call(
            "video.get_context_window",
            {"timestamp_ms": 6_500, "before_ms": 1_000, "after_ms": 5_000},
            call_id="tool-call-3",
        ),
        _execution_context(context),
    )

    assert centered.payload["requested_start_ms"] == 1_500
    assert centered.payload["requested_end_ms"] == 4_500
    assert [item["transcript"] for item in centered.payload["segments"]] == [
        "middle"
    ]
    assert [item["transcript"] for item in at_start.payload["segments"]] == ["zero"]
    assert [item["transcript"] for item in at_end.payload["segments"]] == ["end"]


@pytest.mark.asyncio
async def test_context_window_zero_width_is_normal_empty_success() -> None:
    context = _context(_segment(0, 1_000, "zero"))
    executor = VideoReadOnlyToolExecutor()

    result = await executor.execute(
        _call(
            "video.get_context_window",
            {"timestamp_ms": 500, "before_ms": 0, "after_ms": 0},
        ),
        _execution_context(context),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.payload["found"] is False
    assert result.payload["segments"] == []


@pytest.mark.asyncio
async def test_context_window_has_bounded_segment_count_and_truncation_flag() -> None:
    context = _context(
        *(_segment(index * 100, index * 100 + 90, str(index)) for index in range(12))
    )
    executor = VideoReadOnlyToolExecutor()

    result = await executor.execute(
        _call(
            "video.get_context_window",
            {"timestamp_ms": 500, "before_ms": 30_000, "after_ms": 30_000},
        ),
        _execution_context(context),
    )

    assert result.status is ToolResultStatus.TRUNCATED
    assert result.truncated
    assert len(result.payload["segments"]) == 8
    assert [item["transcript"] for item in result.payload["segments"]] == [
        str(index) for index in range(8)
    ]


def test_resolvers_are_application_level_and_do_not_load_media() -> None:
    context = _context(_segment(100, 200, "one"), _segment(300, 400, "two"))

    assert VideoSegmentResolver.resolve(context, 100)[0].transcript == "one"
    assert VideoSegmentResolver.resolve(context, 200) == ()
    assert VideoContextWindowResolver.resolve(context, 250, 100, 100) == (
        _segment(100, 200, "one"),
        _segment(300, 400, "two"),
    )


@pytest.mark.asyncio
async def test_unknown_dispatch_does_not_invoke_arbitrary_callable() -> None:
    context = _context(_segment(0, 1_000))
    executor = VideoReadOnlyToolExecutor()
    unknown = type(
        "UnknownCall",
        (),
        {
            "call_id": "tool-call-1",
            "tool_name": "shell",
            "validated_arguments": object(),
        },
    )()

    # A model cannot produce this through ToolCall validation.  The defensive
    # branch proves the concrete dispatcher does not call a dynamic attribute.
    with pytest.raises(TypeError):
        await executor.execute(unknown, _execution_context(context))


@pytest.mark.asyncio
async def test_executor_rejects_expired_remaining_deadline() -> None:
    context = _context(_segment(0, 1_000))
    executor = VideoReadOnlyToolExecutor()

    with pytest.raises(DeadlineExceededError):
        await executor.execute(
            _call("video.get_segment", {"timestamp_ms": 1}),
            _execution_context(context),
            remaining_deadline=0,
        )


class _TurnFake:
    def __init__(self, turns: list[ExecutorTurn]) -> None:
        self.turns = list(turns)
        self.continuations: list[object] = []

    async def execute_turn(self, context, plan, previous_critique=None, *, instruction=""):
        del context, plan, previous_critique, instruction
        return self.turns.pop(0)

    async def continue_after_tool(
        self,
        context,
        plan,
        tool_result,
        previous_critique=None,
        *,
        instruction="",
        tools_available=True,
    ):
        del context, plan, previous_critique, instruction
        self.continuations.append((tool_result, tools_available))
        return self.turns.pop(0)


class _ContextFake:
    async def select_relevant(self, context, media_id=None):
        del media_id
        return context


class _PlannerFake:
    async def plan(self, context, *, instruction=""):
        del context, instruction
        return AgentPlan(understoodGoal="explain", tasks=("ground",))

    async def repair_plan(self, context, invalid_plan, *, instruction=""):
        del context, invalid_plan, instruction
        return AgentPlan(understoodGoal="explain", tasks=("ground",))

    async def replan(self, context, current_plan, critique, *, instruction=""):
        del context, current_plan, critique, instruction
        return AgentPlan(understoodGoal="explain", tasks=("ground",))


class _ExecutorFake:
    async def execute(self, context, plan, previous_critique=None, *, instruction=""):
        del context, plan, previous_critique, instruction
        return _result()


class _CriticFake:
    def __init__(self) -> None:
        self.results: list[AnalysisResult | None] = []

    async def critique(self, context, plan, result, *, instruction=""):
        del context, plan, instruction
        self.results.append(result)
        return CriticResult(passed=True)


class _CheckpointFake:
    async def load_plan(self, key):
        del key
        return None

    async def save_plan(self, key, plan):
        del key, plan

    async def load_critic_state(self, key):
        del key
        return None

    async def save_execution_state(self, key, state):
        del key, state

    async def save_critic_state(self, key, state):
        del key, state

    async def save_result(self, key, state):
        del key, state


class _PublisherFake:
    async def publish(self, key, event):
        del key, event


class _TelemetryFake:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def increment(self, metric, amount=1, **kwargs):
        del kwargs
        self.counts[metric] += amount


def _result() -> AnalysisResult:
    return AnalysisResult(
        title="final",
        conclusions=("authoritative",),
        evidence=(
            AnalysisEvidence(
                timestamp_ms=100,
                source="ASR",
                content="authoritative",
                claim="authoritative",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_agent_loop_integrates_real_read_only_executor_then_critic_guard() -> None:
    context = _context(_segment(0, 1_000, "authoritative"))
    search = SearchServiceFake((_hit(0, 1_000, "authoritative"),))
    read_only_executor = VideoReadOnlyToolExecutor(search_service=search)
    turn = _TurnFake(
        [
            ExecutorTurn(
                kind=ExecutorTurnKind.TOOL_REQUEST,
                tool_request=ModelToolRequest(
                    tool_name="video.search_evidence",
                    arguments={"query": "authoritative", "limit": 1},
                ),
            ),
            ExecutorTurn(kind=ExecutorTurnKind.FINAL, final_result=_result()),
        ]
    )
    critic = _CriticFake()
    service = AgentLoopService(
        _ContextFake(),
        _PlannerFake(),
        _ExecutorFake(),
        _CheckpointFake(),
        _PublisherFake(),
        _TelemetryFake(),
        critic,
        executor_turn=turn,
        tool_executor=read_only_executor,
        tool_policy=ToolPolicy(),
        budget_config=AgentBudgetConfig(max_rounds=1, max_duration_ms=10_000),
    )

    state = await service.run(context, media_id=7)

    assert state.round == 1
    assert state.critique is not None and state.critique.passed
    assert len(critic.results) == 1
    assert turn.continuations[0][0].status is ToolResultStatus.SUCCESS
    assert turn.continuations[0][0].payload["hits"][0]["transcript"] == "authoritative"
    assert search.calls[0][0] == 7
    assert search.calls[0][1].user_goal == "authoritative"
