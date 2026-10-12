"""Reader-before-writer and explicit rollback with unchanged M1 identity."""
import json
from uuid import uuid4
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from dovideo.application.conversation_memory import (
    ConversationState, ConversationTurn, MemoryConflict, RollingSummary,
    successful_turn, summary_source_kinds, RequestReceipt, question_digest,
)
from dovideo.infrastructure.conversation_memory import (
    InMemoryConversationMemoryStore, encode_conversation_state, legacy_reader_projection,
    memory_source_write_enabled_from_environment,
)
from test_m1_conversation_memory import harness, ask, summary


# Frozen old strict reader field sets; no new metadata accepted.
class OldTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str
    question: str
    answer: str
    created_at: str
    source_revision: str


class OldSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topics: list[str]
    entities: list[str]
    key_points: list[str]
    unresolved_questions: list[str]
    summary_text: str


def turn(kind="LEGACY_UNKNOWN"):
    return successful_turn(str(uuid4()), "讨论缓存", "缓存是讨论上下文", "rev", kind)


def test_old_json_defaults_unknown_and_default_encoding_has_exact_legacy_shape():
    state = ConversationState(turns=[turn()], summary=summary())
    payload = json.loads(encode_conversation_state(state))
    assert "answer_kind" not in payload["turns"][0]
    assert "source_kinds" not in payload["summary"]
    OldTurn.model_validate(payload["turns"][0])
    OldSummary.model_validate(payload["summary"])
    decoded = ConversationState.model_validate(payload)
    assert decoded.turns[0].answer_kind == "LEGACY_UNKNOWN"
    assert decoded.summary.source_kinds == []
    assert decoded.prompt_context()["recentTurns"] == [{"question": "讨论缓存", "answer": "缓存是讨论上下文"}]


@pytest.mark.parametrize("kind", ["VIDEO_GROUNDED", "MODEL_KNOWLEDGE", "MIXED", "LEGACY_UNKNOWN"])
def test_new_reader_preserves_each_source_and_unknown_is_not_video(kind):
    state = ConversationState(turns=[turn(), turn(kind)], summary=summary().model_copy(update={"source_kinds": [kind]}))
    recovered = ConversationState.model_validate_json(encode_conversation_state(state, write_sources=True))
    assert recovered.turns[1].answer_kind == kind
    assert recovered.summary.source_kinds == [kind]
    assert recovered.turns[0].answer_kind != "VIDEO_GROUNDED"


def test_invalid_source_construct_is_rejected_at_persistence_boundary():
    state = ConversationState(turns=[turn().model_copy(update={"answer_kind": "FAKE"})])
    with pytest.raises(ValidationError):
        encode_conversation_state(state, write_sources=True)


def test_rollback_projection_preserves_history_receipts_version_and_refuses_knowledge():
    state = ConversationState(version=7, turns=[turn("VIDEO_GROUNDED"), turn()],
        receipts=[RequestReceipt(request_id=str(uuid4()), question_digest=question_digest("讨论缓存"))],
        summary=summary().model_copy(update={"source_kinds": ["VIDEO_GROUNDED", "LEGACY_UNKNOWN"]}))
    tagged = json.loads(encode_conversation_state(state, write_sources=True))
    with pytest.raises(ValidationError):
        OldTurn.model_validate(tagged["turns"][0])
    with pytest.raises(ValidationError):
        OldSummary.model_validate(tagged["summary"])
    projected = json.loads(legacy_reader_projection(state))
    OldTurn.model_validate(projected["turns"][0])
    OldSummary.model_validate(projected["summary"])
    assert projected["version"] == 7 and projected["receipts"] == tagged["receipts"]
    assert [t["answer"] for t in projected["turns"]] == [t["answer"] for t in tagged["turns"]]
    for kind in ["MODEL_KNOWLEDGE", "MIXED"]:
        with pytest.raises(ValueError, match="compatible rollback reader"):
            legacy_reader_projection(ConversationState(turns=[turn(kind)]))


def test_summary_union_retains_unknown_and_previous_provenance():
    previous = summary().model_copy(update={"source_kinds": ["MODEL_KNOWLEDGE"]})
    assert summary_source_kinds(previous, [turn(), turn("VIDEO_GROUNDED"), turn("MIXED")]) == [
        "MODEL_KNOWLEDGE", "LEGACY_UNKNOWN", "VIDEO_GROUNDED", "MIXED"]
    assert summary_source_kinds(summary(), [turn()]) == []


@pytest.mark.asyncio
async def test_summary_labels_are_app_owned_and_model_cannot_declare_sources():
    h = harness(InMemoryConversationMemoryStore(write_sources=True))
    for i in range(10):
        await ask(h, f"请解释数据库 {i}")
    state = await h[4].store.load(h[5])
    assert state.summary.source_kinds == ["VIDEO_GROUNDED"]
    request = [c for c in h[3].calls if c[0] == "ROLLING_SUMMARY"][0]
    assert request[1]["olderTurns"][0]["answer_kind"] == "VIDEO_GROUNDED"
    assert "Do not output source labels" in request[2][-1]["content"]
    class ClaimingChat:
        async def complete(self, messages, *, stage):
            return {**summary().model_dump(), "source_kinds": ["VIDEO_GROUNDED"]}
    from dovideo.infrastructure.providers.conversation_memory import ConversationModelAdapter
    with pytest.raises(ValidationError):
        await ConversationModelAdapter(ClaimingChat()).summarize(None, [turn("MODEL_KNOWLEDGE")])


@pytest.mark.asyncio
async def test_new_writer_default_off_and_off_writer_preserves_existing_tagged_record():
    assert not memory_source_write_enabled_from_environment({})
    h = harness()
    await ask(h)
    state = await h[4].store.load(h[5])
    assert "answer_kind" not in json.loads(encode_conversation_state(state))["turns"][0]
    store = h[4].store
    assert await store.acquire(h[5], "token")
    tagged = state.model_copy(update={"version": state.version + 1, "turns": [turn("MODEL_KNOWLEDGE")]})
    with pytest.raises(MemoryConflict):
        await store.commit(h[5], state.version, tagged, "token")
    assert await store.load(h[5]) == state
    await store.release(h[5], "token")
    assert await store.acquire(h[5], "next")
    await store.release(h[5], "next")
    store.write_sources = True
    assert await store.acquire(h[5], "tagged-writer")
    assert await store.commit(h[5], state.version, tagged, "tagged-writer")
    await store.release(h[5], "tagged-writer")
    store.write_sources = False
    recovered = await store.load(h[5])
    assert recovered.turns[0].answer_kind == "MODEL_KNOWLEDGE"
    assert await store.acquire(h[5], "old-writer")
    with pytest.raises(MemoryConflict):
        await store.commit(h[5], recovered.version, recovered.model_copy(update={"version": recovered.version+1}), "old-writer")
    assert await store.load(h[5]) == recovered
    await store.release(h[5], "old-writer")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["MODEL_KNOWLEDGE", "MIXED", "LEGACY_UNKNOWN"])
async def test_history_is_continuity_only_and_cannot_support_wrong_citations(kind):
    from dovideo.application.follow_up import FollowUpFailure
    h = harness(InMemoryConversationMemoryStore(write_sources=True))
    memory, identity = h[4:]
    assert await memory.store.acquire(identity, "seed")
    await memory.save_verified(identity, ConversationState(), "seed", str(uuid4()), "Redis 用什么？",
                               "错误历史：Redis 使用磁盘保存数据。", answer_kind=kind)
    await memory.store.release(identity, "seed")
    before = await memory.store.load(identity)
    h[3].bad_evidence = True
    with pytest.raises(FollowUpFailure, match="evidence_rejected"):
        await ask(h, "它为什么用磁盘？")
    assert await memory.store.load(identity) == before
    payload = h[3].calls[-1][1]
    assert "answer_kind" not in payload["retrievedSourceCandidates"][0]
    if kind != "LEGACY_UNKNOWN":
        assert payload["conversationContext"]["recentTurns"][0]["answer_kind"] == kind
