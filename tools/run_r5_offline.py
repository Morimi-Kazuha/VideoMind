"""R5 cached-source A/B. Natural application ranking with fixture external ports.

No Provider network, extraction, reindexing, or production configuration reads.
Draft source-excerpt visibility is a proxy, never semantic answer accuracy.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import nullcontext
import json
from pathlib import Path
import time

from r4_validation_support import ROOT, harness, insufficient, FeedbackRetrievalMode
from run_r4_feedback_comparison import read, save, digest, cached_cases
from dovideo.application.long_context import LongVideoContextService
from dovideo.application.retrieval_need import RetrievalNeedMode, RetrievalNeedSettings
from dovideo.application.evidence_reflection import ReflectionMode, capture_evidence_reflection
from dovideo.application.retrieval_repair import capture_feedback_retrieval
from dovideo.application.retrieval_budget import capture_retrieval_budget, retrieval_budget_scope
from dovideo.application.follow_up_budget import capture_follow_up_budget
from dovideo.domain import AnalysisMode, VideoContext, VideoChunk


def targets(case, context):
    observations = {o.source_item.source_item_id: o for o in context.observations}
    refs = {r['refId']: r for r in case['expectedEvidenceRefs']}
    return [dict(fact_id=f['factId'], description=f['description'], alternatives=[
        dict(segment_id=refs[r]['segmentId'], source_item_id=refs[r]['sourceItemId'],
             source_revision=refs[r]['sourceRevision'],
             excerpt=observations[refs[r]['sourceItemId']].text[:80])
        for r in f['linkedEvidenceRefs']]) for f in case['requiredFacts']]


def visibility(goals, projection, context):
    segments = {s.segment_id: s for s in context.segments}
    def visible(ref):
        s = segments[ref['segment_id']]
        return any(p['startMs'] == s.start_ms and p['endMs'] == s.end_ms and
                   ref['excerpt'] in (p['asrExcerpt'] + ' '.join(p['ocrExcerpts']))
                   for p in projection)
    return {g['fact_id']: any(visible(r) for r in g['alternatives']) for g in goals}


async def arm(case, context, chunks, *, need, reflection, feedback):
    h = await harness(case['query'], case['assessment_fixture'], context=context, chunks=chunks,
                      mode=feedback, budget_limit=150000, attempts=3)
    service, _, _, _, counts, http, hybrid = h
    # Remove R4's controlled-first-segment override. Keep identical existing
    # Strict Hybrid / BM25 / RRF / fixture vector/reranker stack in all arms.
    service._retrieval = LongVideoContextService(service._retrieval._chunking,
                                                service._retrieval._retrieval)
    service._retrieval_need_settings = RetrievalNeedSettings(need)
    service._reflection.mode = reflection
    error, answer = None, None
    start = time.perf_counter()
    measure = retrieval_budget_scope(True, enforced=False) if feedback == FeedbackRetrievalMode.OFF else nullcontext()
    with measure, capture_retrieval_budget() as retrieval, capture_follow_up_budget() as usage, capture_evidence_reflection() as refl, capture_feedback_retrieval() as repair:
        try:
            answer = await service.answer(42, case['query'], 'cached video evaluation', AnalysisMode.GENERAL)
        except Exception as exc:
            error = getattr(exc, 'code', type(exc).__name__)
    elapsed = (time.perf_counter() - start)*1000
    initial, final = [], []
    for payload in http.payloads:
        if 'Grade semantic sufficiency' in payload['messages'][0]['content']:
            initial = json.loads(payload['messages'][-1]['content'])['retrievedSourceCandidates']
        text = payload['messages'][-1]['content']
        if 'Input as JSON:\n' in text:
            value = json.loads(text.split('Input as JSON:\n')[-1])
            if 'retrievedSourceCandidates' in value:
                final = value['retrievedSourceCandidates']
    assert len(final) <= 8
    assert not retrieval or retrieval[0]['total_base_calls'] <= 4
    assert not retrieval or retrieval[0]['repair_base_calls'] <= 1
    return dict(error=error, markdown=answer, counts=dict(counts), final_projection=final,
                initial_projection=initial, linked_source_excerpt_visibility=visibility(case['targets'], final, context),
                retrieval=retrieval, budget=usage, reflection=[r.outcome for r in refl],
                feedback=[r.state for r in repair], fixture_e2e_ms=elapsed,
                provider_input_tokens=None, provider_output_tokens=None, provider_cost_usd=None)


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    base = ROOT/'work/evaluation/optimization-v1-20261009'
    cp, ch, ds = base/'media-cuda/context.json', base/'run-02/rag/chunks.json', base/'draft-v1/dataset.json'
    assert digest(cp) == read(base/'media-cuda/media-manifest.json')['artifactDigests']['context.json']
    context = VideoContext.model_validate(read(cp))
    chunks = tuple(VideoChunk.model_validate(c) for c in read(ch))
    prior, _ = cached_cases()
    proposals = {c['id']: c['proposal'] for c in prior}
    unknown = dict(status='UNKNOWN', reason_code='UNCERTAIN_SEMANTIC_SUPPORT', covered_requirements=[], missing_requirements=[], candidate_references=[])
    cases = read(ds)['cases']
    for c in cases:
        c['targets'] = targets(c, context)
        c['assessment_fixture'] = proposals.get(c['caseId'], unknown)
    cases += [dict(caseId='direct-general', query='Independent of the video, explain time complexity.', targets=[], assessment_fixture=unknown),
              dict(caseId='mixed-evaluation', query='Is the speaker technically correct?', targets=[], assessment_fixture=unknown)]
    save(output/'frozen-inputs.json', dict(cases=cases, source_hashes={str(p.relative_to(ROOT)):digest(p) for p in (cp,ch,ds)},
         independent_gold=False, deadline_seconds=60, token_ceiling=150000, attempts=3,
         m2_enabled=False, natural_initial_application_ranking=True, live_dense_and_reranker=False,
         assessment_note='Historical tc17/tc18 responses replayed unchanged; other cases UNKNOWN fixture. Not a new semantic grader.'))
    rows=[]
    for c in cases:
        kwargs=dict(case=c,context=context,chunks=chunks)
        a=await arm(**kwargs,need=RetrievalNeedMode.OFF,reflection=ReflectionMode.OFF,feedback=FeedbackRetrievalMode.OFF)
        b=await arm(**kwargs,need=RetrievalNeedMode.ENABLED,reflection=ReflectionMode.SHADOW,feedback=FeedbackRetrievalMode.ENABLED)
        off=await arm(**kwargs,need=RetrievalNeedMode.ENABLED,reflection=ReflectionMode.SHADOW,feedback=FeedbackRetrievalMode.OFF)
        assert off['initial_projection']==b['initial_projection']
        rows.append(dict(id=c['caseId'],architecture_A=a,architecture_B=b,r4_A=off,r4_B=b))
        print(c['caseId'],b['feedback'],b['error'],flush=True)
    save(output/'results.json',rows)
    save(output/'summary.json',dict(cases=len(rows),kind='OFFLINE_CACHE_NATURAL_APPLICATION_RANKING_FIXTURE_EXTERNAL_PORTS',
        counts={arm:dict(sum((Counter(r[arm]['counts']) for r in rows),Counter())) for arm in ('architecture_A','architecture_B','r4_A')},
        feedback_states=dict(Counter(s for r in rows for s in r['r4_B']['feedback'])),
        errors={arm:dict(Counter(r[arm]['error'] for r in rows if r[arm]['error'])) for arm in ('architecture_A','architecture_B','r4_A')},
        source_excerpt_visible={arm:sum(sum(r[arm]['linked_source_excerpt_visibility'].values()) for r in rows) for arm in ('architecture_A','architecture_B','r4_A')},
        source_excerpt_target_count=sum(len(c['targets']) for c in cases),
        new_paid_calls=0, semantic_answer_accuracy=None, real_cost_latency_usage=None,
        note='M2 OFF. 150k/3 attempts are explicit simulation conditions, not production calibration.'))
    # Read-only diagnosis over trusted raw sources; query windows are experimental
    # artifacts only, never substituted into production model input.
    refs=[r for c in cases for r in c.get('expectedEvidenceRefs',[])]
    items={o.source_item.source_item_id:o for o in context.observations}
    long_refs=[r for r in refs if len(items[r['sourceItemId']].text)>400]
    target=next(o for o in context.observations if '3n+1003' in o.text)
    offset=target.text.index('3n+1003'); begin=max(0,offset-180); window=target.text[begin:begin+400]
    assert window in target.text and '3n+1003' in window and len(window)<=400
    save(output/'projection-diagnosis.json',dict(linked_refs=len(refs),linked_observations_longer_than_400=len(long_refs),
        observations=len(context.observations),observations_longer_than_400=sum(len(o.text)>400 for o in context.observations),
        tc17=dict(source_item=target.source_item.model_dump(),formula_offset=offset,
                  old_prefix_contains_formula='3n+1003' in target.text[:400],experimental_window=window,
                  window_chars=len(window),original_containment=True,production_change=False),
        note='Long text frequency measures clipping exposure; it is not necessary-fact loss frequency. Query window needs Guard/observation budget design before rollout.'))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    asyncio.run(run(parser.parse_args().output))
