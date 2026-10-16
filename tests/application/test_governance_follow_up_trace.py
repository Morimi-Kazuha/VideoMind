"""Frozen pre-governance behavior, including calls and persisted memory."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from test_m1_conversation_memory import harness, ask
from test_r2a_follow_up_integration import wire
from dovideo.application.follow_up_budget import FollowUpBudgetSettings


def digest(value):
    def normalize(item):
        if isinstance(item, dict):
            # Wall-clock receipt creation is the sole nondeterministic value.
            return {key: normalize(value) for key, value in item.items() if key != "created_at"}
        if isinstance(item, (list, tuple)):
            return [normalize(value) for value in item]
        return item
    return hashlib.sha256(json.dumps(normalize(value), sort_keys=True,
        ensure_ascii=False).encode()).hexdigest()


async def collect_trace(service_class=None):
    h = list(harness())
    h[5] = replace(h[5], conversation_id=str(UUID(int=42)))
    if service_class is not None:
        h[0] = service_class(h[1], h[2], h[0]._model, memory=h[4])
    http = wire(h, FollowUpBudgetSettings())
    answers = []
    for index in range(10):
        question = "它和 MySQL 相比有什么不同？" if index == 1 else f"请解释数据库 {index}"
        answers.append(await ask(h, question, request_id=str(UUID(int=1000 + index))))
    state = await h[4].store.load(h[5])
    return {
        "answers_sha256": digest(answers),
        "http_payloads_sha256": digest(http.calls),
        "retrieval_queries_sha256": digest(h[2].queries),
        "persisted_memory_sha256": digest(state.model_dump(mode="json")),
        "model_stages": [call[0] for call in h[3].calls],
        "memory_version": state.version,
    }


@pytest.mark.asyncio
async def test_follow_up_trace_matches_480b4be():
    baseline = json.loads(Path("tests/fixtures/governance/follow-up-trace.json").read_text(encoding="utf-8"))
    assert await collect_trace() == baseline["trace"]
