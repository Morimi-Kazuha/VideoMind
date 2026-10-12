"""Offline fixture ports around the real Follow-up / Hybrid / Gateway path.

Initial evidence is an explicitly controlled projection, not a claim about
natural retrieval recall. Supplemental Hybrid, provenance and BM25 are real
application code; vector/reranker/model responses and Usage are fixtures.
"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dovideo.application.adaptive_retrieval import AdaptiveRetrievalService, AdaptiveRetrievalSettings, RoutingSuggestion
from dovideo.application.chunking import chunk_windows
from dovideo.application.follow_up import GroundedFollowUpService
from dovideo.application.long_context import LongVideoContextService
from dovideo.application.retrieval import VideoEvidenceRetrievalService
from dovideo.application.retrieval_repair import FeedbackRetrievalMode
from dovideo.application.evidence_reflection import ReflectionMode
from dovideo.application.follow_up_budget import FollowUpBudgetSettings, FollowUpBudgetMode
from dovideo.application.retrieval_need import RetrievalNeedSettings, RetrievalNeedMode
from dovideo.application.sparse_retrieval import BM25Retriever
from dovideo.application.ports.retrieval import RerankerResult
from dovideo.application.value_objects import VectorHit
from dovideo.domain import VideoContext, VideoSegment, VideoChunk, VideoEvidenceHit, TemporalObservation, SourceItemIdentity, PROVENANCE_VERSION, CHUNKING_CONTRACT_VERSION
from dovideo.domain.provenance import content_digest, segment_id_for, source_item_id_for, chunk_id_for
from dovideo.infrastructure.providers import ProviderConfig, ProviderHttpResponse, OpenAICompatibleChatClient
from dovideo.infrastructure.providers.evidence_reflection import EvidenceReflectionModelAdapter
from dovideo.infrastructure.providers.follow_up import GroundedFollowUpModelAdapter, DirectFollowUpModelAdapter, MixedKnowledgeModelAdapter
from dovideo.infrastructure.providers.conversation_memory import ConversationModelAdapter
from dovideo.application.conversation_memory import ConversationMemoryService
from dovideo.infrastructure.conversation_memory import InMemoryConversationMemoryStore
from dovideo.presentation.composition import LocalRetrievalPlanner
from dovideo.infrastructure.r4_runtime import _StrictRetrievalService


def context_and_chunks(texts):
    revision = hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest()
    segments, observations = [], []
    for i, text in enumerate(texts):
        start, end = i * 600000, i * 600000 + 60000
        sid = segment_id_for(revision, start, end, i)
        item = SourceItemIdentity(source_item_id=source_item_id_for(revision, sid, "ASR", i, start, end, text),
            source_revision=revision, segment_id=sid, source_type="ASR", ordinal=i, timestamp_ms=start, end_ms=end,
            content_digest=content_digest(text), provenance_version=PROVENANCE_VERSION)
        segments.append(VideoSegment(start_ms=start, end_ms=end, transcript=text, segment_id=sid,
            source_revision=revision, source_items=(item,), provenance_version=PROVENANCE_VERSION))
        observations.append(TemporalObservation(source_item=item, text=text))
    context = VideoContext(source="fixture://r4-media-42", source_revision=revision, provenance_version=PROVENANCE_VERSION,
        segments=tuple(segments), observations=tuple(observations))
    chunks = tuple(VideoChunk(start_ms=start, end_ms=end, raw_segments=raw,
        segment_summary="\n".join(s.transcript for s in raw), embedding=(1.0,),
        source_revision=revision, chunk_id=chunk_id_for(revision, start, end), chunking_version=CHUNKING_CONTRACT_VERSION)
        for start, end, raw in chunk_windows(context.segments))
    return context, chunks


def hit_for(segment, chunks):
    chunk = next(c for c in chunks if segment in c.raw_segments)
    source = "ASR+OCR" if segment.transcript.strip() and segment.ocr_texts else "OCR" if segment.ocr_texts else "ASR"
    return VideoEvidenceHit(start_ms=segment.start_ms, end_ms=segment.end_ms, source=source, snippet=segment.transcript[:180],
        transcript=segment.transcript, ocr_texts=tuple(dict.fromkeys(t.strip() for t in segment.ocr_texts if t.strip())),
        source_revision=segment.source_revision, chunk_id=chunk.chunk_id, segment_id=segment.segment_id,
        source_item_ids=segment.source_item_ids)


def insufficient(span, *, refs=(), key="video_fact", code="PARTIAL_FACT_COVERAGE"):
    return {"status": "INSUFFICIENT", "reason_code": "EVIDENCE_GAPS", "covered_requirements": [],
        "missing_requirements": [{"requirement_key": key, "question_span": span, "candidate_references": list(refs), "reason_code": code}],
        "candidate_references": list(refs)}


class FixtureHttp:
    def __init__(self, proposal, counts):
        self.proposal, self.counts, self.payloads = proposal, counts, []
        self.reflection_error = None
        self.planner_failures = 0
        self.bad_answer = False
    async def post(self, url, **kw):
        payload = kw["json"]
        self.payloads.append(payload)
        system = payload["messages"][0]["content"]
        if system == "fixture planner":
            self.counts["planner_http"] += 1
            if self.planner_failures:
                self.planner_failures -= 1
                return ProviderHttpResponse(503, {"error": {"message": "fixture failure"}})
            value = {"ok": True}
        elif "Grade semantic sufficiency" in system:
            self.counts["reflection_http"] += 1
            if self.reflection_error:
                raise self.reflection_error
            value = self.proposal
        elif "You have not viewed" in system:
            self.counts["direct_http"] += 1
            value = {"answer": "这是独立的通用知识回答。"}
        elif "sourceQuotes" in payload["messages"][-1]["content"]:
            self.counts["knowledge_http"] += 1
            value = {"knowledge_blocks": [{"kind": "MODEL_KNOWLEDGE", "text": "以下是一般知识分析，视频事实仍受有限证据约束。"}]}
        elif "Resolve references" in payload["messages"][-1]["content"]:
            self.counts["rewrite_http"] += 1
            value = {"standalone_query": self.standalone_query, "needs_clarification": False, "clarification_question": ""}
        elif "Merge previous summary" in payload["messages"][-1]["content"]:
            self.counts["summary_http"] += 1
            value = {"topics": [], "entities": [], "key_points": [], "unresolved_questions": [], "summary_text": "既有视频讨论。"}
        else:
            self.counts["answer_http"] += 1
            inp = json.loads(payload["messages"][-1]["content"].split("Input as JSON:\n")[-1])
            evidence, quotes = [], []
            for candidate in inp["retrievedSourceCandidates"][:5]:
                channel = "ASR" if candidate["asrExcerpt"].strip() else "OCR"
                text = candidate["asrExcerpt"] if channel == "ASR" else candidate["ocrExcerpts"][0]
                quote = text.split("\n")[0][:160]
                if not quote.strip():
                    continue
                timestamp = candidate["startMs"]
                for observation in candidate.get("sourceObservations", []):
                    if observation["source"] == channel and quote in observation["excerpt"]:
                        timestamp = max(timestamp, observation["timestampMs"])
                        break
                quotes.append(quote)
                evidence.append({"candidateIndex": candidate["candidateIndex"], "timestampMs": timestamp,
                    "source": channel, "content": quote, "claim": quote})
            value = {"answer": "\n".join(quotes), "evidence": evidence}
            if self.bad_answer:
                value["evidence"][0]["content"] = "invented quote"
        return ProviderHttpResponse(200, {"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}], "usage": {"total_tokens": 20}})


async def harness(question, proposal, *, mode=FeedbackRetrievalMode.ENABLED, context=None, chunks=None,
                  initial_segments=None, texts=None, parts=(), memory=False, budget_limit=500000, attempts=1):
    if context is None:
        context, chunks = context_and_chunks(texts or ("作者选择 B。", "本段说明基本设置。", "本段说明操作界面。", "作者选择 B 的原因是减少内存占用。"))
    counts = Counter()
    initial_segments = tuple(initial_segments or context.segments[:1])
    initial = tuple(hit_for(s, chunks) for s in initial_segments)
    http = FixtureHttp(proposal, counts)
    chat = OpenAICompatibleChatClient(ProviderConfig(base_url="https://fixture.test/v1", model="fixture",
        max_attempts=attempts, retry_delay_seconds=0), client=http)

    class Planner(LocalRetrievalPlanner):
        async def plan_retrieval(self, query):
            counts["planner_logical"] += 1
            await chat.complete(({"role": "system", "content": "fixture planner"}, {"role": "user", "content": query}), stage="RETRIEVAL_PLANNER")
            return await super().plan_retrieval(query)
        async def suggest_retrieval(self, query):
            counts["m2_planner"] += 1
            return RoutingSuggestion(retrieval_route="BOUNDED_MULTI_QUERY", reason_code="COMPARISON", sub_queries=list(parts))
    class Embedding:
        async def embed(self, query):
            counts["embedding"] += 1
            return (1.0,) + (0.0,) * 1023
    class Index:
        async def search(self, media_id, vector, *, limit, **scope):
            counts["vector_query_fixture"] += 1
            return tuple(VectorHit(start_ms=c.start_ms, end_ms=c.end_ms, score=1 / (i + 1), chunk_id=c.chunk_id, source_revision=c.source_revision,
                chunking_version=c.chunking_version) for i, c in enumerate(chunks[:limit]))
    class Sparse(BM25Retriever):
        def rank(self, *a, **kw):
            counts["bm25"] += 1
            return super().rank(*a, **kw)
    class Reranker:
        async def rerank(self, query, documents):
            counts["reranker_fixture"] += 1
            # Literal term matching, then original rank. This is a fixture,
            # not live Cross-Encoder behavior or a production replacement.
            terms = query.split()
            scores = [(d, sum(t in d.text for t in terms)) for d in documents]
            ordered = sorted(scores, key=lambda row: -row[1])
            return tuple(RerankerResult(d.candidate_id, float(score)) for d, score in ordered)
    planner = Planner()
    class Telemetry:
        def increment(self, key, amount=1):
            counts["telemetry_" + key] += amount
        def observe(self, key, value):
            pass
        def counter_value(self, key):
            return counts["telemetry_" + key]
    hybrid = _StrictRetrievalService(planner, Embedding(), Index(), sparse=Sparse(), reranker=Reranker(), telemetry=Telemetry())
    class NoChunking:
        async def build(self, *a, **kw):
            raise AssertionError("reuse durable chunks; never reprocess/index")
    class InitialProjection(LongVideoContextService):
        async def search_evidence(self, media_id, selected_context, *, chunks=None, **kw):
            result = await super().search_evidence(media_id, selected_context, chunks=chunks, **kw)
            return initial if selected_context.user_goal == question else result
    class Checkpoint:
        async def load_context(self, media_id):
            return context
        async def load_chunks(self, media_id):
            return chunks
        async def load_result(self, key):
            return None
    adaptive = AdaptiveRetrievalService(hybrid, planner, AdaptiveRetrievalSettings(enabled=bool(parts)))
    long = InitialProjection(NoChunking(), adaptive)
    mem = ConversationMemoryService(InMemoryConversationMemoryStore(write_sources=True), ConversationModelAdapter(chat)) if memory else None
    service = GroundedFollowUpService(Checkpoint(), long, GroundedFollowUpModelAdapter(chat),
        reflection_model=EvidenceReflectionModelAdapter(chat), reflection_mode=ReflectionMode.SHADOW,
        feedback_mode=mode, budget_settings=FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED, budget_limit),
        direct_model=DirectFollowUpModelAdapter(chat), mixed_knowledge_model=MixedKnowledgeModelAdapter(chat), mixed_enabled=True,
        retrieval_need_settings=RetrievalNeedSettings(RetrievalNeedMode.ENABLED), memory=mem)
    return service, context, chunks, initial, counts, http, hybrid
