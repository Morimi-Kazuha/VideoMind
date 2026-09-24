from __future__ import annotations

import pytest
from pydantic import ValidationError

from dovideo.application import (
    AgentRole,
    ExecutorTurn,
    ExecutorTurnKind,
    GetContextWindowArguments,
    GetSegmentArguments,
    ModelToolRequest,
    PolicyDecision,
    SearchEvidenceArguments,
    TaskKey,
    ToolCall,
    ToolName,
    ToolPolicy,
    ToolPolicyContext,
    ToolPolicyReasonCode,
    ToolRegistry,
    ToolResult,
    ToolResultStatus,
    V1_TOOL_NAMES,
)
from dovideo.domain import AnalysisEvidence, AnalysisMode, AnalysisResult


def _context(**overrides: object) -> ToolPolicyContext:
    values: dict[str, object] = {
        "task_key": TaskKey(7, "inspect the lecture", AnalysisMode.REVIEW),
        "media_id": 7,
        "mode": AnalysisMode.REVIEW,
        "agent_role": AgentRole.EXECUTOR,
        "media_duration_ms": 120_000,
    }
    values.update(overrides)
    return ToolPolicyContext(**values)


def _request(tool_name: str, arguments: dict[str, object]) -> ModelToolRequest:
    return ModelToolRequest(tool_name=tool_name, arguments=arguments)


def test_registry_has_exact_static_v1_allowlist_and_deterministic_lookup() -> None:
    registry = ToolRegistry()

    assert registry.names() == V1_TOOL_NAMES == (
        "video.search_evidence",
        "video.get_segment",
        "video.get_context_window",
    )
    assert registry.resolve("video.search_evidence") is not None
    assert registry.resolve("shell") is None
    assert registry.resolve("filesystem.read") is None
    assert not hasattr(registry, "register")


def test_model_request_is_strict_and_cannot_carry_authoritative_identity() -> None:
    request = _request(
        "video.search_evidence",
        {"query": "topic", "limit": 2},
    )
    assert request.tool_name == "video.search_evidence"
    assert request.arguments == {"query": "topic", "limit": 2}

    with pytest.raises(ValidationError):
        ModelToolRequest(
            tool_name="video.search_evidence",
            arguments={"query": "topic"},
            media_id=99,
        )


def test_registered_argument_models_are_typed_and_reject_extra_fields() -> None:
    assert SearchEvidenceArguments(query="topic", limit=2).limit == 2
    assert GetSegmentArguments(timestamp_ms=10).timestamp_ms == 10
    assert GetContextWindowArguments(
        timestamp_ms=10,
        before_ms=20,
        after_ms=30,
    ).after_ms == 30

    with pytest.raises(ValidationError):
        SearchEvidenceArguments(query="topic", media_id=7)
    with pytest.raises(ValidationError):
        GetSegmentArguments(timestamp_ms="10")
    with pytest.raises(ValidationError):
        GetContextWindowArguments(timestamp_ms=10, unexpected=True)


def test_tool_call_requires_application_owned_call_id_and_matching_schema() -> None:
    registry = ToolRegistry()
    request = _request("video.get_segment", {"timestamp_ms": 10})
    call = registry.create_call(request, call_id="application-call-1")

    assert isinstance(call, ToolCall)
    assert call.call_id == "application-call-1"
    assert call.tool_name is ToolName.GET_SEGMENT
    assert isinstance(call.validated_arguments, GetSegmentArguments)

    with pytest.raises(ValidationError):
        ToolCall(
            tool_name=ToolName.GET_SEGMENT,
            validated_arguments=GetSegmentArguments(timestamp_ms=10),
        )

    with pytest.raises(ValidationError):
        ToolCall(
            call_id="call-1",
            tool_name=ToolName.GET_SEGMENT,
            validated_arguments=SearchEvidenceArguments(query="wrong"),
        )


def test_executor_turn_requires_exactly_one_branch() -> None:
    result = AnalysisResult(
        title="title",
        conclusions=["claim"],
        evidence=[AnalysisEvidence(timestampMs=1, source="ASR", content="claim")],
    )
    final = ExecutorTurn(
        kind=ExecutorTurnKind.FINAL,
        final_result=result,
    )
    requested = ExecutorTurn(
        kind=ExecutorTurnKind.TOOL_REQUEST,
        tool_request=_request("video.search_evidence", {"query": "topic"}),
    )

    assert final.final_result == result
    assert requested.tool_request is not None

    with pytest.raises(ValidationError):
        ExecutorTurn(kind=ExecutorTurnKind.FINAL)
    with pytest.raises(ValidationError):
        ExecutorTurn(
            kind=ExecutorTurnKind.TOOL_REQUEST,
            tool_request=requested.tool_request,
            final_result=result,
        )


def test_tool_policy_allows_valid_requests_for_each_v1_schema() -> None:
    policy = ToolPolicy()

    search = policy.evaluate(
        _request("video.search_evidence", {"query": "topic", "limit": 2}),
        _context(),
    )
    segment = policy.evaluate(
        _request("video.get_segment", {"timestamp_ms": 10}),
        _context(),
    )
    window = policy.evaluate(
        _request(
            "video.get_context_window",
            {"timestamp_ms": 10, "before_ms": 1_000, "after_ms": 2_000},
        ),
        _context(),
    )

    assert search == segment == window
    assert search.decision is PolicyDecision.ALLOW
    assert search.allowed
    assert search.reason_code is None


def test_tool_policy_denies_unknown_tool_and_wrong_role() -> None:
    policy = ToolPolicy()

    unknown = policy.evaluate(
        _request("shell", {}),
        _context(),
    )
    wrong_role = policy.evaluate(
        _request("video.search_evidence", {"query": "topic"}),
        _context(agent_role=AgentRole.CRITIC),
    )

    assert unknown.reason_code is ToolPolicyReasonCode.TOOL_NOT_ALLOWED
    assert wrong_role.reason_code is ToolPolicyReasonCode.ROLE_NOT_ALLOWED
    assert not unknown.allowed
    assert not wrong_role.allowed


def test_tool_policy_denies_auto_or_mismatched_trusted_mode() -> None:
    policy = ToolPolicy()

    auto = policy.evaluate(
        _request("video.search_evidence", {"query": "topic"}),
        _context(mode="AUTO"),
    )
    mismatched = policy.evaluate(
        _request("video.search_evidence", {"query": "topic"}),
        _context(mode=AnalysisMode.GENERAL),
    )

    assert auto.reason_code is ToolPolicyReasonCode.MODE_NOT_ALLOWED
    assert mismatched.reason_code is ToolPolicyReasonCode.MODE_NOT_ALLOWED


def test_tool_policy_denies_malformed_arguments_and_identity_substitution() -> None:
    policy = ToolPolicy()

    malformed = policy.evaluate(
        {
            "tool_name": "video.get_segment",
            "arguments": {"timestamp_ms": "10"},
        },
        _context(),
    )
    alternate_media = policy.evaluate(
        _request(
            "video.search_evidence",
            {"query": "topic", "media_id": 99},
        ),
        _context(),
    )
    alternate_task = policy.evaluate(
        _request(
            "video.search_evidence",
            {"query": "topic", "task_key": "other"},
        ),
        _context(),
    )

    assert malformed.reason_code is ToolPolicyReasonCode.INVALID_ARGUMENTS
    assert alternate_media.reason_code is ToolPolicyReasonCode.MEDIA_IDENTITY_MISMATCH
    assert alternate_task.reason_code is ToolPolicyReasonCode.TASK_IDENTITY_MISMATCH


def test_tool_policy_denies_context_identity_mismatch() -> None:
    decision = ToolPolicy().evaluate(
        _request("video.search_evidence", {"query": "topic"}),
        _context(media_id=99),
    )

    assert decision.reason_code is ToolPolicyReasonCode.MEDIA_IDENTITY_MISMATCH


@pytest.mark.parametrize("timestamp_ms", [-1, 120_000, 999_999])
def test_tool_policy_denies_segment_timestamp_outside_half_open_media_range(
    timestamp_ms: int,
) -> None:
    decision = ToolPolicy().evaluate(
        _request("video.get_segment", {"timestamp_ms": timestamp_ms}),
        _context(),
    )

    assert decision.reason_code is ToolPolicyReasonCode.TIMESTAMP_OUT_OF_BOUNDS


def test_tool_policy_denies_invalid_context_window_bounds() -> None:
    policy = ToolPolicy()
    negative_duration = policy.evaluate(
        _request(
            "video.get_context_window",
            {"timestamp_ms": 10, "before_ms": -1},
        ),
        _context(),
    )
    oversized = policy.evaluate(
        _request(
            "video.get_context_window",
            {"timestamp_ms": 10, "before_ms": 60_000, "after_ms": 1},
        ),
        _context(),
    )

    assert negative_duration.reason_code is ToolPolicyReasonCode.INVALID_ARGUMENTS
    assert oversized.reason_code is ToolPolicyReasonCode.RESULT_LIMIT_INVALID


@pytest.mark.parametrize("limit", [0, -1, 9])
def test_tool_policy_denies_search_result_limit_outside_existing_retrieval_bound(
    limit: int,
) -> None:
    decision = ToolPolicy().evaluate(
        _request("video.search_evidence", {"query": "topic", "limit": limit}),
        _context(),
    )

    assert decision.reason_code is ToolPolicyReasonCode.RESULT_LIMIT_INVALID


def test_tool_policy_denies_per_round_or_total_budget_exhaustion() -> None:
    policy = ToolPolicy()
    per_round = policy.evaluate(
        _request("video.search_evidence", {"query": "topic"}),
        _context(tool_calls_this_round=4),
    )
    total = policy.evaluate(
        _request("video.search_evidence", {"query": "topic"}),
        _context(tool_calls_total=12),
    )
    zero_round_limit = policy.evaluate(
        _request("video.search_evidence", {"query": "topic"}),
        _context(per_round_limit=0),
    )
    zero_total_limit = policy.evaluate(
        _request("video.search_evidence", {"query": "topic"}),
        _context(total_limit=0),
    )

    assert per_round.reason_code is ToolPolicyReasonCode.BUDGET_EXHAUSTED
    assert total.reason_code is ToolPolicyReasonCode.BUDGET_EXHAUSTED
    assert zero_round_limit.reason_code is ToolPolicyReasonCode.BUDGET_EXHAUSTED
    assert zero_total_limit.reason_code is ToolPolicyReasonCode.BUDGET_EXHAUSTED


def test_tool_result_is_bounded_and_never_carries_raw_exception() -> None:
    success = ToolResult(
        call_id="call-1",
        tool_name=ToolName.SEARCH_EVIDENCE,
        status=ToolResultStatus.SUCCESS,
        payload={"items": [{"timestamp_ms": 10}]},
    )
    truncated = ToolResult(
        call_id="call-1",
        tool_name=ToolName.SEARCH_EVIDENCE,
        status=ToolResultStatus.TRUNCATED,
        payload={"items": []},
        truncated=True,
    )
    array_payload = ToolResult(
        call_id="call-1",
        tool_name=ToolName.SEARCH_EVIDENCE,
        status=ToolResultStatus.SUCCESS,
        payload=[{"timestamp_ms": 10}],
    )

    assert success.payload == {"items": [{"timestamp_ms": 10}]}
    assert truncated.truncated
    assert array_payload.payload == [{"timestamp_ms": 10}]

    with pytest.raises(ValidationError):
        ToolResult(
            call_id="call-1",
            tool_name=ToolName.SEARCH_EVIDENCE,
            status=ToolResultStatus.FAILED,
            payload={"exception": RuntimeError("secret")},
        )
    with pytest.raises(ValidationError):
        ToolResult(
            call_id="call-1",
            tool_name=ToolName.SEARCH_EVIDENCE,
            status=ToolResultStatus.TRUNCATED,
            payload={},
        )
