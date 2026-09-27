from __future__ import annotations

import asyncio
import json
import math

import pytest

from dovideo.application import InvalidResultError, validate_result
from dovideo.application.evidence import EvidenceVerificationService
from dovideo.domain import (
    AgentPlan,
    AnalysisEvidence,
    AnalysisResult,
    ChunkSummary,
    CriticResult,
    VideoContext,
    VideoSegment,
)
from dovideo.application.budget_usage import InMemoryAgentBudgetUsage
from dovideo.infrastructure.providers import (
    ChunkSummaryModelAdapter,
    CriticModelAdapter,
    EmbeddingResponseError,
    ExecutorModelAdapter,
    LocalChunkSummaryAdapter,
    ModelResponseError,
    OpenAICompatibleChatClient,
    OpenAICompatibleEmbeddingAdapter,
    PlannerModelAdapter,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderHttpResponse,
    ProviderTransientError,
    RetrievalPlannerModelAdapter,
)


class FakeHttpClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def post(self, url: str, *, headers, json, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "json": json,
                "timeout": timeout,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeChat:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def complete(self, messages, *, stage: str):
        self.calls.append({"messages": messages, "stage": stage})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class AttemptObserver:
    def __init__(self) -> None:
        self.attempts: list[dict[str, object]] = []

    def record_executor_structural_attempt(
        self,
        *,
        attempt: int,
        repair_triggered: bool,
        repair_succeeded: bool,
    ) -> None:
        self.attempts.append(
            {
                "executorStructuralAttempt": attempt,
                "executorStructuralRepairTriggered": repair_triggered,
                "executorStructuralRepairSucceeded": repair_succeeded,
            }
        )


def _config(**kwargs) -> ProviderConfig:
    values = {
        "base_url": "https://provider.invalid/v1",
        "model": "model-test",
        "api_key": "unit-test-key",
        "retry_delay_seconds": 0,
    }
    values.update(kwargs)
    return ProviderConfig(**values)


def _context() -> VideoContext:
    return VideoContext(
        source="memory://video",
        user_goal="explain the topic",
        segments=(
            VideoSegment(
                start_ms=0,
                end_ms=1_000,
                transcript="opening statement",
                ocr_texts=("slide",),
            ),
        ),
    )


def _plan() -> AgentPlan:
    return AgentPlan(understood_goal="explain", tasks=("find evidence",))


def _result() -> AnalysisResult:
    return AnalysisResult(
        title="title",
        conclusions=("opening statement",),
        evidence=(
            AnalysisEvidence(
                timestamp_ms=0,
                source="ASR",
                content="opening statement",
                claim="opening statement",
            ),
        ),
    )


def _executor_json() -> str:
    return json.dumps(
        {
            "title": "title",
            "conclusions": ["opening statement"],
            "evidence": [
                {
                    "timestampMs": 0,
                    "source": "ASR",
                    "content": "opening statement",
                    "claim": "opening statement",
                }
            ],
            "suggestions": [],
            "sections": [],
        }
    )


def test_provider_config_is_lazy_and_does_not_expose_api_key() -> None:
    assert ProviderConfig.from_environment({}) is None
    with pytest.raises(ProviderConfigurationError):
        ProviderConfig.from_environment({}, required=True)
    config = _config()
    assert "unit-test-key" not in repr(config)
    assert config.chat_url.endswith("/chat/completions")
    assert config.embeddings_url.endswith("/embeddings")
    with pytest.raises(ProviderConfigurationError):
        _config(timeout_seconds=math.inf)


@pytest.mark.asyncio
async def test_embedding_maps_openai_data_and_preserves_input_order() -> None:
    http = FakeHttpClient(
        [ProviderHttpResponse(200, {"data": [{"index": 0, "embedding": [1, 2.5]}]})]
    )
    adapter = OpenAICompatibleEmbeddingAdapter(_config(embedding_model="embed-test"), client=http)

    assert await adapter.embed("  query  ") == (1.0, 2.5)
    request = http.calls[0]
    assert request["url"] == "https://provider.invalid/v1/embeddings"
    assert request["json"] == {"model": "embed-test", "input": "  query  "}
    assert request["headers"]["Authorization"] == "Bearer unit-test-key"


@pytest.mark.asyncio
async def test_embedding_blank_and_malformed_vectors_have_explicit_boundaries() -> None:
    blank_http = FakeHttpClient([])
    blank_adapter = OpenAICompatibleEmbeddingAdapter(_config(), client=blank_http)
    assert await blank_adapter.embed(" \n") == ()
    assert blank_http.calls == []

    for payload in (
        {},
        {"data": []},
        {"data": [{"embedding": [float("nan")]}]},
        {"data": [{"embedding": ["bad"]}]},
    ):
        adapter = OpenAICompatibleEmbeddingAdapter(
            _config(), client=FakeHttpClient([ProviderHttpResponse(200, payload)])
        )
        with pytest.raises(EmbeddingResponseError):
            await adapter.embed("query")


@pytest.mark.asyncio
async def test_chat_client_maps_response_and_retries_transient_status() -> None:
    http = FakeHttpClient(
        [
            ProviderHttpResponse(503, {"error": "unavailable"}),
            ProviderHttpResponse(
                200,
                {"choices": [{"message": {"content": '{"ok": true}'}}]},
            ),
        ]
    )
    client = OpenAICompatibleChatClient(_config(max_attempts=2), client=http)
    value = await client.complete(
        ({"role": "user", "content": "hello"},), stage="TEST"
    )
    assert value == '{"ok": true}'
    assert len(http.calls) == 2
    assert http.calls[0]["json"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_chat_json_mode_prompt_explicitly_contains_provider_required_json_word() -> None:
    http = FakeHttpClient(
        [
            ProviderHttpResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"understoodGoal":"explain",'
                                    '"tasks":["find evidence"]}'
                                )
                            }
                        }
                    ]
                },
            )
        ]
    )
    client = OpenAICompatibleChatClient(_config(max_attempts=1), client=http)

    await PlannerModelAdapter(client).plan(_context())

    payload = http.calls[0]["json"]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["model"] == "model-test"
    assert any(
        "json" in message["content"]
        for message in payload["messages"]
        if message["role"] == "system"
    )
    planner_prompt = next(
        message["content"]
        for message in payload["messages"]
        if message["role"] == "user"
    )
    assert "JSON array" in planner_prompt
    assert "Never return task objects" in planner_prompt


@pytest.mark.asyncio
async def test_chat_client_forwards_provider_reported_usage_without_estimating() -> None:
    usage = InMemoryAgentBudgetUsage()
    http = FakeHttpClient(
        [
            ProviderHttpResponse(
                200,
                {
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "cost": 0.25},
                    "choices": [{"message": {"content": "{}"}}],
                },
            )
        ]
    )
    await OpenAICompatibleChatClient(
        _config(max_attempts=1), client=http, usage_sink=usage
    ).complete(({"role": "user", "content": "x"},), stage="TEST")
    assert usage.current_usage().estimated_tokens == 5
    assert usage.current_usage().estimated_cost == 0.25


@pytest.mark.asyncio
async def test_role_adapters_map_existing_dtos_and_mode_instruction() -> None:
    chat = FakeChat(
        [
            '{"understoodGoal":"explain","tasks":["find evidence"]}',
            {"understoodGoal": "explain", "tasks": ["find evidence"]},
            '{"title":"title","conclusions":["opening statement"],'
            '"evidence":[{"timestampMs":0,"source":"ASR",'
            '"content":"opening statement","claim":"opening statement"}]}',
            '{"passed":true,"feedback":[],"missingRequirements":[],'
            '"unsupportedClaims":[],"requiredTimestamps":[]}',
            '{"segmentSummary":"opening","keywords":["topic"]}',
            '{"semanticQuery":"topic","keywords":["topic"],"visualKeywords":[]}',
        ]
    )
    planner = PlannerModelAdapter(chat)
    executor = ExecutorModelAdapter(chat)
    critic = CriticModelAdapter(chat)
    summary = ChunkSummaryModelAdapter(chat)
    retrieval = RetrievalPlannerModelAdapter(chat)
    context = _context()
    plan = await planner.plan(context, instruction="use the review section")
    repaired = await planner.repair_plan(context, AgentPlan(tasks=()), instruction="repair")
    result = await executor.execute(context, plan)
    critique = await critic.critique(context, plan, result)
    chunk_summary = await summary.summarize_chunk(context.segments)
    intent = await retrieval.plan_retrieval("topic")

    assert plan == _plan()
    assert repaired == _plan()
    assert result == _result()
    assert critique == CriticResult(passed=True)
    assert chunk_summary == ChunkSummary(segment_summary="opening", keywords=("topic",))
    assert intent.semantic_query == "topic"
    assert chat.calls[0]["stage"] == "PLANNER"
    assert "use the review section" in chat.calls[0]["messages"][1]["content"]
    assert "Additional mode" not in chat.calls[1]["messages"][1]["content"] or "repair" in chat.calls[1]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_executor_prompt_declares_complete_analysis_result_contract() -> None:
    chat = FakeChat([_executor_json()])

    await ExecutorModelAdapter(chat).execute(_context(), _plan())

    prompt = chat.calls[0]["messages"][1]["content"]
    assert isinstance(prompt, str)
    for field in ("title", "conclusions", "evidence", "suggestions", "sections"):
        assert f'"{field}"' in prompt
    assert '"timestampMs"' in prompt
    assert '"source"' in prompt
    assert "ASR | OCR | ASR+OCR" in prompt
    assert '"claim"' in prompt


@pytest.mark.asyncio
async def test_executor_prompt_requires_exact_grounded_evidence_binding() -> None:
    chat = FakeChat([_executor_json()])

    await ExecutorModelAdapter(chat).execute(_context(), _plan())

    prompt = chat.calls[0]["messages"][1]["content"]
    assert isinstance(prompt, str)
    assert "Every conclusion must have at least one grounded evidence item" in prompt
    assert "character-for-character" in prompt
    assert "exactly one item in conclusions" in prompt
    assert "do not paraphrase" in prompt
    assert "summarize, shorten, translate" in prompt
    assert "verbatim excerpt" in prompt
    assert "substring under that same normalization" in prompt
    assert "timestampMs must fall within the VideoContext segment" in prompt
    assert "actual available evidence" in prompt


@pytest.mark.asyncio
async def test_critic_prompt_uses_the_same_evidence_binding_contract() -> None:
    chat = FakeChat(
        [
            json.dumps(
                {
                    "passed": True,
                    "feedback": [],
                    "missingRequirements": [],
                    "unsupportedClaims": [],
                    "requiredTimestamps": [],
                }
            )
        ]
    )

    await CriticModelAdapter(chat).critique(_context(), _plan(), _result())

    prompt = chat.calls[0]["messages"][1]["content"]
    assert isinstance(prompt, str)
    assert "Apply this same evidence-binding contract" in prompt
    assert "character-for-character" in prompt
    assert "Reject paraphrased or merely related claims" in prompt
    assert "timestampMs must fall within the VideoContext segment" in prompt
    assert '"unsupportedClaims" as arrays of strings' in prompt
    assert '"requiredTimestamps" as an array of integers' in prompt
    assert 'all four arrays must be empty' in prompt


@pytest.mark.asyncio
async def test_executor_valid_first_response_uses_one_provider_call() -> None:
    chat = FakeChat([_executor_json()])

    result = await ExecutorModelAdapter(chat).execute(_context(), _plan())

    assert result == _result()
    assert len(chat.calls) == 1
    assert [call["stage"] for call in chat.calls] == ["EXECUTOR"]


@pytest.mark.asyncio
async def test_executor_structural_failure_retries_once_and_preserves_contract() -> None:
    chat = FakeChat(
        [
            '{"title":"wrong shape","conclusions":[{"not":"a string"}]}',
            _executor_json(),
        ]
    )
    observer = AttemptObserver()

    result = await ExecutorModelAdapter(
        chat,
        diagnostic_observer=observer,
    ).execute(_context(), _plan())

    assert result == _result()
    assert len(chat.calls) == 2
    assert [call["stage"] for call in chat.calls] == ["EXECUTOR", "EXECUTOR_REPAIR"]
    first_prompt = chat.calls[0]["messages"][1]["content"]
    second_prompt = chat.calls[1]["messages"][1]["content"]
    assert isinstance(first_prompt, str)
    assert isinstance(second_prompt, str)
    assert not second_prompt.startswith(first_prompt)
    assert "InvalidDraft:" in second_prompt
    assert "wrong shape" in second_prompt
    assert "VideoContext:" not in second_prompt
    assert "previous output did not satisfy" in second_prompt
    assert observer.attempts == [
        {
            "executorStructuralAttempt": 1,
            "executorStructuralRepairTriggered": True,
            "executorStructuralRepairSucceeded": False,
        },
        {
            "executorStructuralAttempt": 2,
            "executorStructuralRepairTriggered": True,
            "executorStructuralRepairSucceeded": True,
        },
    ]


@pytest.mark.asyncio
async def test_executor_structural_failure_stops_after_second_response() -> None:
    wrong = '{"title":"wrong shape","conclusions":[{"not":"a string"}]}'
    chat = FakeChat([wrong, wrong, _executor_json()])

    with pytest.raises(ModelResponseError):
        await ExecutorModelAdapter(chat).execute(_context(), _plan())

    assert len(chat.calls) == 2
    assert [call["stage"] for call in chat.calls] == ["EXECUTOR", "EXECUTOR_REPAIR"]


@pytest.mark.asyncio
async def test_semantically_empty_dto_does_not_trigger_structural_repair() -> None:
    chat = FakeChat(
        [
            json.dumps(
                {
                    "title": "",
                    "conclusions": [],
                    "evidence": [],
                    "suggestions": [],
                    "sections": [],
                }
            )
        ]
    )

    result = await ExecutorModelAdapter(chat).execute(_context(), _plan())

    assert result.conclusions == ()
    assert result.evidence == ()
    assert len(chat.calls) == 1
    with pytest.raises(InvalidResultError):
        validate_result(result)


@pytest.mark.asyncio
async def test_semantically_invalid_evidence_does_not_trigger_structural_repair() -> None:
    payload = json.loads(_executor_json())
    payload["evidence"][0]["claim"] = "The presenter begins speaking"
    chat = FakeChat([json.dumps(payload)])
    observer = AttemptObserver()

    result = await ExecutorModelAdapter(
        chat,
        diagnostic_observer=observer,
    ).execute(_context(), _plan())
    critique = EvidenceVerificationService().enforce_evidence_bounds(
        _context(),
        result,
        CriticResult(passed=True),
    )

    assert len(chat.calls) == 1
    assert observer.attempts == [
        {
            "executorStructuralAttempt": 1,
            "executorStructuralRepairTriggered": False,
            "executorStructuralRepairSucceeded": False,
        }
    ]
    assert critique.passed is False
    assert critique.unsupported_claims == ("opening statement",)


@pytest.mark.asyncio
async def test_malformed_model_json_is_not_silently_business_repaired() -> None:
    chat = FakeChat(["not json"])
    with pytest.raises(ModelResponseError):
        await PlannerModelAdapter(chat).plan(_context())
    assert len(chat.calls) == 1

    chat = FakeChat(["{\"tasks\":[\"ok\"]}"])
    # Parsing succeeds as a DTO; executable structure remains the application's
    # AgentPolicy responsibility rather than being repaired by the adapter.
    plan = await PlannerModelAdapter(chat).plan(_context())
    assert not plan.is_execution_valid()
    assert len(chat.calls) == 1


@pytest.mark.asyncio
async def test_critic_dto_validation_keeps_structural_diagnostics() -> None:
    invalid = (
        '{"passed":false,"feedback":"rewrite",'
        '"missingRequirements":[],"unsupportedClaims":[],'
        '"requiredTimestamps":[]}'
    )
    chat = FakeChat(
        [invalid, invalid]
    )

    with pytest.raises(ModelResponseError) as caught:
        await CriticModelAdapter(chat).critique(_context(), _plan(), _result())

    message = str(caught.value)
    assert "CRITIC_REPAIR response did not match its DTO" in message
    assert '"payload_type":"dict"' in message
    assert '"feedback":"str"' in message
    assert '"loc":["feedback"]' in message
    assert '"type":"value_error"' in message
    assert '"msg":"Value error, expected a collection"' in message
    assert "rewrite" not in message
    assert [call["stage"] for call in chat.calls] == ["CRITIC", "CRITIC_REPAIR"]
    assert "VideoContext" not in chat.calls[1]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_critic_repairs_object_issue_entries_without_changing_verdict() -> None:
    invalid = json.dumps({
        "passed": False,
        "unsupportedClaims": [{"claim": "unsupported", "reason": "no timestamp"}],
    })
    repaired = json.dumps({
        "passed": False,
        "unsupportedClaims": ["unsupported: no timestamp"],
    })
    chat = FakeChat([invalid, repaired])

    critique = await CriticModelAdapter(chat).critique(_context(), _plan(), _result())

    assert critique.passed is False
    assert critique.unsupported_claims == ("unsupported: no timestamp",)
    assert "VideoContext" not in chat.calls[1]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_critic_repair_rejects_changed_verdict() -> None:
    invalid = '{"passed":false,"unsupportedClaims":[{"claim":"unsupported"}]}'
    repaired = '{"passed":true,"unsupportedClaims":["unsupported"]}'
    chat = FakeChat([invalid, repaired])

    with pytest.raises(ModelResponseError, match="changed the Critic verdict"):
        await CriticModelAdapter(chat).critique(_context(), _plan(), _result())


@pytest.mark.asyncio
async def test_critic_without_parseable_verdict_cannot_be_repaired_into_a_pass() -> None:
    chat = FakeChat(['{"feedback":"malformed"}'])

    with pytest.raises(ModelResponseError):
        await CriticModelAdapter(chat).critique(_context(), _plan(), _result())
    assert [call["stage"] for call in chat.calls] == ["CRITIC"]


@pytest.mark.asyncio
async def test_provider_local_timeout_and_cancellation_are_not_relabelled() -> None:
    timeout = TimeoutError("provider timeout")
    with pytest.raises(TimeoutError) as caught:
        await OpenAICompatibleEmbeddingAdapter(
            _config(), client=FakeHttpClient([timeout])
        ).embed("query")
    assert caught.value is timeout

    cancellation = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await OpenAICompatibleChatClient(
            _config(), client=FakeHttpClient([cancellation])
        ).complete(({"role": "user", "content": "x"},), stage="TEST")


@pytest.mark.asyncio
async def test_local_summary_is_ordered_bounded_and_empty_safe() -> None:
    adapter = LocalChunkSummaryAdapter(max_summary_chars=12, max_keywords=3)
    value = await adapter.summarize_chunk(
        (
            VideoSegment(start_ms=0, end_ms=1, transcript="first", ocr_texts=("screen",)),
            VideoSegment(start_ms=1, end_ms=2, transcript="second"),
        )
    )
    assert value.segment_summary == "first screen "[:12]
    assert value.keywords[:2] == ("first", "screen")
    assert await adapter.summarize_chunk(()) == ChunkSummary()


@pytest.mark.asyncio
async def test_auth_failure_does_not_include_credentials() -> None:
    adapter = OpenAICompatibleChatClient(
        _config(),
        client=FakeHttpClient([ProviderHttpResponse(401, {"secret": "never expose"})]),
    )
    with pytest.raises(Exception) as caught:
        await adapter.complete(({"role": "user", "content": "x"},), stage="TEST")
    assert "unit-test-key" not in str(caught.value)
    assert "never expose" not in str(caught.value)


@pytest.mark.asyncio
async def test_transient_exhaustion_is_typed_without_response_body() -> None:
    adapter = OpenAICompatibleChatClient(
        _config(max_attempts=1),
        client=FakeHttpClient(
            [ProviderHttpResponse(503, {"credential": "do not leak"})]
        ),
    )
    with pytest.raises(ProviderTransientError) as caught:
        await adapter.complete(({"role": "user", "content": "x"},), stage="TEST")
    assert "credential" not in str(caught.value)
