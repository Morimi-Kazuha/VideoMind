from dovideo.infrastructure.providers.config import ProviderConfig
from dovideo.infrastructure.providers.model import OpenAICompatibleChatClient
from dovideo.infrastructure.r4_runtime import R4AgentTelemetry


def test_chat_usage_capture_keeps_exact_stage_counts_and_missing_cost() -> None:
    telemetry = R4AgentTelemetry(store=object())
    client = OpenAICompatibleChatClient(
        ProviderConfig(base_url="https://example.test/v1", model="model-a"),
        usage_sink=telemetry,
    )
    with telemetry.capture_chat_usage() as records:
        client._record_provider_usage(
            {"usage": {"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18}},
            stage="PLANNER",
        )
    assert len(records) == 1
    assert records[0]["stage"] == "PLANNER"
    assert records[0]["inputTokens"] == 13
    assert records[0]["outputTokens"] == 5
    assert records[0]["totalTokens"] == 18
    assert records[0]["providerReportedCost"] is None


def test_missing_token_breakdown_is_not_inferred() -> None:
    telemetry = R4AgentTelemetry(store=object())
    client = OpenAICompatibleChatClient(
        ProviderConfig(base_url="https://example.test/v1", model="model-a"),
        usage_sink=telemetry,
    )
    with telemetry.capture_chat_usage() as records:
        client._record_provider_usage({"usage": {"total_tokens": 18}}, stage="CRITIC")
    assert records[0]["inputTokens"] is None
    assert records[0]["outputTokens"] is None
    assert records[0]["totalTokens"] == 18
