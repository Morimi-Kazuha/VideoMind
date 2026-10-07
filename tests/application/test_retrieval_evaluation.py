import asyncio
from pathlib import Path
import sys

import pytest

from dovideo.application.evaluation_runner import (AgentLoopEvaluationAdapter, EvaluationRunner, EvaluationRunnerConfig,
    MappingEvaluationSourceResolver, RetrievalEvaluationAdapter, project_retrieval_hits, calculate_retrieval_metrics)
from dovideo.application.chunking import VideoChunkingService
from dovideo.application.retrieval import VideoEvidenceRetrievalService
from dovideo.application.retrieval_observation import capture_retrieval
from dovideo.domain import AgentState
from dovideo.infrastructure.providers.local_embedding import LocalTfidfEmbeddingAdapter
from dovideo.infrastructure.providers.summary import LocalChunkSummaryAdapter
from dovideo.presentation.composition import InMemoryVectorIndex, LocalRetrievalPlanner

sys.path.insert(0, str(Path(__file__).parents[2] / "tools"))
from run_retrieval_comparison import prepared_fixture, compare


def services(artifact):
    embedding = LocalTfidfEmbeddingAdapter(max_features=2048).fit(
        [s.transcript + " " + " ".join(s.ocr_texts) for s in artifact.context.segments])
    return VideoChunkingService(LocalChunkSummaryAdapter(), embedding), VideoEvidenceRetrievalService(
        LocalRetrievalPlanner(), embedding, InMemoryVectorIndex())


@pytest.mark.asyncio
async def test_actual_hits_project_rank_source_ids_and_keep_one_segment_candidate():
    dataset, artifact = prepared_fixture()
    chunking, retrieval = services(artifact)
    chunks = await chunking.build(artifact.context.segments)
    await retrieval.index(1, chunks)
    case = dataset.case("ocr-01")
    hits = await retrieval.search(1, case.query, chunks)
    projected = project_retrieval_hits(hits, artifact.context)
    assert len(projected) == len(hits)
    assert [p.rank for p in projected] == list(range(1, len(hits) + 1))
    assert projected[0].source_item_ids == hits[0].source_item_ids
    assert projected[0].source_type == "ASR+OCR"
    assert calculate_retrieval_metrics(case.expected_evidence_refs, projected).recall_at_k[1] == 1
    from dovideo.application.evaluation_contracts import ExpectedEvidenceRef
    segment = next(s for s in artifact.context.segments if s.segment_id == projected[0].segment_id)
    refs = tuple(ExpectedEvidenceRef(source_revision=artifact.source_revision,
                                     source_item_id=item.source_item_id, source_type=item.source_type) for item in segment.source_items)
    assert calculate_retrieval_metrics(refs, projected).recall_at_k[1] == 1
    assert project_retrieval_hits((hits[0].model_copy(update={"source_revision": "b" * 64}),), artifact.context) == ()


@pytest.mark.asyncio
async def test_agent_adapter_observes_initial_ranking_without_an_extra_retrieval():
    dataset, artifact = prepared_fixture()
    chunking, retrieval = services(artifact)
    chunks = await chunking.build(artifact.context.segments)
    await retrieval.index(1, chunks)
    calls = []
    class Agent:
        async def run(self, context, **kwargs):
            calls.append(context.user_goal)
            await retrieval.retrieve(1, context.user_goal, chunks)
            await retrieval.search(1, "separate critic query", chunks)
            return AgentState(goal=context.user_goal)
    observation = await AgentLoopEvaluationAdapter(Agent()).execute(dataset.case("technical-01").execution_input(),
        artifact=artifact, strategy=None, trial_index=0, timeout_seconds=10)
    assert len(calls) == 1
    assert observation.retrieved_evidence[0].start_ms == 7 * 60_000
    assert observation.retrieved_evidence[0].rank == 1


@pytest.mark.asyncio
async def test_contextvar_isolation_and_cleanup():
    _, artifact = prepared_fixture()
    chunking, retrieval = services(artifact)
    chunks = await chunking.build(artifact.context.segments)
    async def run(query):
        with capture_retrieval() as batches:
            await asyncio.sleep(0)
            hits = await retrieval.search(None, query, chunks)
        assert batches == [hits]
        return hits[0].start_ms
    assert await asyncio.gather(run("blue heron"), run("QKV")) == [11 * 60_000, 18 * 60_000]
    with capture_retrieval() as batches:
        assert batches == []


@pytest.mark.asyncio
async def test_retrieval_only_runner_without_fake_answer_or_private_text(tmp_path):
    dataset, artifact = prepared_fixture()
    chunking, retrieval = services(artifact)
    runner = EvaluationRunner(RetrievalEvaluationAdapter(chunking, retrieval),
        MappingEvaluationSourceResolver({artifact.media_ref: artifact}), repo_root=tmp_path)
    result = await runner.run(dataset, EvaluationRunnerConfig(retrieval_only=True,
        case_filter=("technical-01",), artifact_output=str(tmp_path / "out")))
    assert result.run.successful_count == 1
    m = result.results[0].deterministic_metrics
    assert m.retrieval_recall_at_k_by_k["1"] == 1
    assert m.schema_valid is None and m.evidence_guard_pass is None
    raw = (tmp_path / "out/results.jsonl").read_text()
    assert dataset.case("technical-01").query not in raw
    assert "materializing the attention matrix" not in raw
    assert "expectedEvidenceRefs" not in raw
    assert result.run.environment["evaluation_scope"] == "retrieval"


@pytest.mark.asyncio
async def test_comparison_metrics_are_deterministic(tmp_path):
    first = await compare(tmp_path / "first")
    second = await compare(tmp_path / "second")
    assert first == second
    assert first["classification"] == "SYNTHETIC"
    assert first["noAnswer"] == "NOT_MEASURED"
