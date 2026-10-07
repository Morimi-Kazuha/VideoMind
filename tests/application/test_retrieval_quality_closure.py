"""Meaningful safety/equivalence checks for eval-only stage instrumentation."""
from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "tools"))
from run_retrieval_quality_closure import (attach_trace, summarize_case, RecordingVector,
    StructuralMetrics, load_environment, reranker_changes, safe_failure)
from run_retrieval_comparison import prepared_fixture, baseline_services, BASELINE_SHA
from dovideo.application.chunking import VideoChunkingService
from dovideo.application.retrieval import VideoEvidenceRetrievalService
from dovideo.infrastructure.providers.local_embedding import LocalTfidfEmbeddingAdapter
from dovideo.infrastructure.providers.summary import LocalChunkSummaryAdapter
from dovideo.presentation.composition import InMemoryVectorIndex, LocalRetrievalPlanner


@pytest.mark.asyncio
@pytest.mark.parametrize("baseline", [False, True])
async def test_instrumentation_preserves_all_twelve_rankings_and_sanitizes_gold(baseline):
    dataset, artifact = prepared_fixture()
    embedding = LocalTfidfEmbeddingAdapter(max_features=2048).fit(
        [s.transcript + " " + " ".join(s.ocr_texts) for s in artifact.context.segments])
    chunk_type, service_type = baseline_services(BASELINE_SHA) if baseline else (VideoChunkingService, VideoEvidenceRetrievalService)
    chunks = await chunk_type(LocalChunkSummaryAdapter(), embedding).build(artifact.context.segments)
    plain = service_type(LocalRetrievalPlanner(), embedding, InMemoryVectorIndex())
    metrics = StructuralMetrics()
    vector = RecordingVector(InMemoryVectorIndex())
    traced = service_type(LocalRetrievalPlanner(), embedding, vector, metrics)
    trace = attach_trace(traced, baseline=baseline)
    await plain.index(1, chunks)
    await traced.index(1, chunks)
    rows = []
    for case in dataset.cases:
        expected = await plain.search(1, case.query, chunks)
        actual = await traced.search(1, case.query, chunks)
        assert actual == expected
        row = summarize_case(case, chunks, trace, actual, vector, {}, baseline=baseline, reranker_on=False)
        encoded = json.dumps(row)
        assert case.query not in encoded
        assert all(s.transcript not in encoded for s in artifact.context.segments if s.transcript)
        assert all(text not in encoded for s in artifact.context.segments for text in s.ocr_texts)
        assert all(0 <= r["asrMatch"] <= 1 and 0 <= r["ocrMatch"] <= 1 for r in row["segmentRanking"])
        rows.append(row)
    assert len(rows) == 12
    if not baseline:
        for case_id in ("semantic-01", "distractor-02"):
            row = next(r for r in rows if r["caseId"] == case_id)
            assert row["expected"][0]["segmentRank"] == 6
            assert row["expected"][0]["final3Hit"]
        overlapping = next(r for r in rows if r["caseId"] == "boundary-01")
        assert len(overlapping["expected"][0]["parents"]) == 2
        assert len({r["segmentId"] for r in overlapping["segmentRanking"]}) == len(overlapping["segmentRanking"])


def test_environment_files_never_override_inherited_credentials_or_expose_values(tmp_path, monkeypatch):
    monkeypatch.setenv("DOVIDEO_EMBEDDING_API_KEY", "inherited-secret")
    path = tmp_path / "local.env"
    path.write_text('DOVIDEO_EMBEDDING_API_KEY=file-secret\nDOVIDEO_RERANKER_MODEL="BAAI/bge-reranker-v2-m3"\nUNRELATED_SECRET=ignored\n')
    monkeypatch.delenv("DOVIDEO_RERANKER_MODEL", raising=False)
    monkeypatch.delenv("UNRELATED_SECRET", raising=False)
    result = load_environment([path])
    import os
    assert os.environ["DOVIDEO_EMBEDDING_API_KEY"] == "inherited-secret"
    assert os.environ["DOVIDEO_RERANKER_MODEL"] == "BAAI/bge-reranker-v2-m3"
    assert "UNRELATED_SECRET" not in os.environ
    assert "secret" not in json.dumps(result)
    # Undo the eval loader's process-local setting as well.
    monkeypatch.delenv("DOVIDEO_RERANKER_MODEL")


def test_reranker_rescue_and_harm_count_each_expected_target():
    def row(case_id, ids, expected):
        return {"caseId": case_id, "stages": {"final": [{"chunkId": i} for i in ids]},
                "expected": [{"final3Hit": hit} for hit in expected]}
    off = [row("rescue", ["a", "b", "c"], [False]), row("harm", ["a", "b", "c"], [True]),
           row("same", ["a", "b", "c"], [True])]
    on = [row("rescue", ["d", "b", "c"], [True]), row("harm", ["d", "b", "c"], [False]),
          row("same", ["a", "b", "c"], [True])]
    result = reranker_changes(off, on)
    assert result["counts"] == {"changedTop3": 2, "rescued": 1, "harmed": 1}
    assert result["unchanged"] == ["same"]


def test_errors_do_not_persist_provider_payloads_or_credentials():
    assert safe_failure(RuntimeError("secret transcript credential")) == {"status": "FAILED", "errorType": "RuntimeError"}


def test_authorized_runtime_inheritance_keeps_explicit_settings_and_never_serializes_keys():
    from run_retrieval_quality_closure import inherit_reranker_configuration
    values = {"DOVIDEO_EMBEDDING_API_KEY": "embedding-private-value",
              "DOVIDEO_RERANKER_API_KEY": " ", "DOVIDEO_RERANKER_URL": " ", "DOVIDEO_RERANKER_MODEL": ""}
    metadata = inherit_reranker_configuration(values)
    assert values["DOVIDEO_RERANKER_API_KEY"] == values["DOVIDEO_EMBEDDING_API_KEY"]
    assert values["DOVIDEO_RERANKER_URL"] == "https://api.siliconflow.cn/v1/rerank"
    assert values["DOVIDEO_RERANKER_MODEL"] == "BAAI/bge-reranker-v2-m3"
    assert "private-value" not in json.dumps(metadata)
    explicit = {"DOVIDEO_EMBEDDING_API_KEY": "embedding-private-value", "DOVIDEO_RERANKER_API_KEY": "dedicated-private-value",
                "DOVIDEO_RERANKER_URL": "https://example.test/rerank", "DOVIDEO_RERANKER_MODEL": "explicit-model"}
    before = explicit.copy()
    result = inherit_reranker_configuration(explicit)
    assert explicit == before
    assert result["defaultedSettings"] == []
    assert result["credentialSource"] == "DOVIDEO_RERANKER_API_KEY"


@pytest.mark.asyncio
async def test_live_arm_orchestration_with_test_doubles_verifies_scope_cleanup_and_three_arms(tmp_path):
    """Only test doubles in pytest's temporary folder; no live quality claim."""
    from run_retrieval_quality_closure import run_arms
    from dovideo.infrastructure.vector.qdrant import JsonHttpResponse
    from dovideo.application.ports.retrieval import RerankerResult
    dataset, artifact = prepared_fixture()
    local = LocalTfidfEmbeddingAdapter(max_features=1024).fit(
        [s.transcript + " " + " ".join(s.ocr_texts) for s in artifact.context.segments])
    class TestEmbedding:
        async def embed(self, text):
            values = await local.embed(text)
            return (*values, *((0.0,) * (1024 - len(values))))
    class TestVector(InMemoryVectorIndex):
        collection = "pytest_test_double"
        async def upsert(self, media_id, chunks):
            values = {c.chunk_id: c for c in self._chunks.get(media_id, ())}
            values.update({c.chunk_id: c for c in chunks})
            self._chunks[media_id] = tuple(values.values())
        async def _send(self, method, path, body):
            assert path.endswith("/points/count")
            media_id = body["filter"]["must"][0]["match"]["value"]
            return JsonHttpResponse(200, {"result": {"count": len(self._chunks.get(media_id, ()))}})
    class TestReranker:
        calls = 0
        async def rerank(self, query, documents):
            self.calls += 1
            return tuple(RerankerResult(d.candidate_id, 1 / (i + 1)) for i, d in enumerate(documents))
    vector, reranker = TestVector(), TestReranker()
    await run_arms(tmp_path, {"gitSha": "test-double-only"},
                   {"qdrant": {}, "reranker": {"status": "PASS"}}, TestEmbedding(), vector, reranker)
    comparison = json.loads((tmp_path / "comparison.json").read_text())
    assert all(a["successfulCases"] == 12 for a in comparison["arms"].values())
    assert all(a["cleanup"]["remainingPoints"] == 0 for a in comparison["arms"].values())
    assert reranker.calls == 12
    scopes = json.loads((tmp_path / "scope-verification.json").read_text())
    assert len(scopes) == 2 and all(r["staleProbeCount"] == 2 for r in scopes)
    assert not vector._chunks
    diagnostics = json.loads((tmp_path / "ranking-diagnostics.json").read_text())
    assert len(diagnostics["C"]) == 12
    assert all(r["candidateCounts"]["reranker"] > 0 for r in diagnostics["C"])
    assert all(r["candidateCounts"]["reranker"] == 0 for r in diagnostics["B"])


@pytest.mark.asyncio
async def test_provider_fallback_case_is_failed_and_not_strict_live_success():
    from run_retrieval_quality_closure import DiagnosticAdapter
    dataset, artifact = prepared_fixture()
    embedding = LocalTfidfEmbeddingAdapter(max_features=2048).fit([s.transcript for s in artifact.context.segments])
    chunks = await VideoChunkingService(LocalChunkSummaryAdapter(), embedding).build(artifact.context.segments)
    class FailingVector(InMemoryVectorIndex):
        async def search(self, *args, **kwargs):
            raise ConnectionError("private provider error")
    vector = RecordingVector(FailingVector())
    metrics = StructuralMetrics()
    service = VideoEvidenceRetrievalService(LocalRetrievalPlanner(), embedding, vector, metrics)
    trace = attach_trace(service)
    adapter = DiagnosticAdapter(service, chunks, trace, vector, metrics, dataset,
                                media_id=1, baseline=False, reranker_on=False)
    # A failure before a row exists must not shift later case identities.
    original_search = service.search
    async def fail_before_row(*args, **kwargs):
        raise ValueError("failure before ranking")
    service.search = fail_before_row
    with pytest.raises(ValueError, match="failure before ranking"):
        await adapter.execute(dataset.cases[0].execution_input(), artifact=artifact, strategy=None,
                              trial_index=0, timeout_seconds=10)
    service.search = original_search
    with pytest.raises(RuntimeError, match="strict live provider fallback"):
        await adapter.execute(dataset.cases[1].execution_input(), artifact=artifact, strategy=None,
                              trial_index=0, timeout_seconds=10)
    assert adapter.rows[0]["caseId"] == dataset.cases[1].case_id
    assert adapter.rows[0]["providerStatus"] == "FALLBACK_USED"
    assert adapter.rows[0]["vectorStoreFallbacksDelta"] == 1
