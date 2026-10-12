"""Offline aggregate audit of preserved runs, usage and current admission gates."""
import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import statistics
import sys

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'src'))
from run_r3_evidence_reflection import read,save
from dovideo.application.evidence_reflection import EvidenceReflection,ReflectionMode,capture_evidence_reflection
from dovideo.application.execution_budget import AgentExecutionBudget
from dovideo.application.follow_up_budget import FollowUpBudgetMode,FollowUpBudgetSettings,follow_up_budget_scope
from dovideo.infrastructure.providers.evidence_reflection import EvidenceReflectionModelAdapter


def classify(row):
    records=row['http']
    if any(r.get('refusal') for r in records): return 'MODEL_REFUSAL'
    if any(r.get('finish_reason')=='length' for r in records): return 'OUTPUT_TOKEN_LIMIT'
    if any(r.get('status_code')==404 for r in records): return 'ROUTING_HTTP_404'
    return row['outcome']


async def analyze(out):
    groups=['provider-probe','provider-probe-documented-tag','live-frozen-v1','longer-wait-diagnostic-v1']
    rows=[]
    for group in groups:
        for row in read(out/group/'results.json'):
            rows.append({**row,'group':group,'failure_class':classify(row)})
    actual=[(r,h['usage']) for r in rows for h in r['http'] if h['usage'] is not None]
    estimates=[r for r in rows if r['budget'] and r['budget']['usage_sources'].get('ESTIMATE')]
    costs=[u['cost'] for _,u in actual if 'cost' in u]
    original=[r for r in rows if r['group'] in {'provider-probe-documented-tag','live-frozen-v1'}]
    diagnostics=[r for r in rows if r['group']=='longer-wait-diagnostic-v1']
    gap_findings=[]
    for r in rows:
        if r['assessment']:
            gap_findings.append({'id':r['id'],'status':r['prediction'],'assessment':r['assessment'],
                'literal_check':'Complete B reason is explicitly present' if r['id']=='complete' else 'B adoption is present; no reason is provided',
                'r4_actionability':'NO_RETRIEVAL_NEEDED' if r['id']=='complete' else 'REAL_CAUSAL_GAP_IN_SCOPE; recoverability from video not established'})
    save(out/'aggregate.json',{'http_attempts':sum(r['http_attempts'] for r in rows),
        'charged_ledger_tokens':sum(r['budget']['charged_chat_tokens'] for r in rows if r['budget']),
        'provider_reported_tokens':sum(u['total_tokens'] for _,u in actual),'provider_input_tokens':sum(u['prompt_tokens'] for _,u in actual),
        'provider_output_tokens':sum(u['completion_tokens'] for _,u in actual),
        'responses_with_usage':len(actual),'sent_attempts_without_usage':len(estimates),
        'conservative_estimate_tokens':sum(r['budget']['charged_chat_tokens'] for r in estimates),
        'reported_cost_partial_usd':sum(costs),'billing_for_unreported_attempts':'NOT_MEASURED',
        'estimated_input_over_actual_ratios':[round(r['estimated_input_tokens']/u['prompt_tokens'],4) for r,u in actual],
        'failure_classes':dict(Counter(r['failure_class'] for r in rows)),
        'original_case_count':len(original),'original_policy_results':dict(Counter(r['outcome'] for r in original)),
        'original_skipped_budget_ratio':sum(r['outcome']=='SKIPPED_BUDGET' for r in original)/len(original),
        'original_attempts':sum(r['http_attempts'] for r in original),'original_semantic_dto_successes':sum(bool(r['assessment']) for r in original),
        'diagnostic_results':dict(Counter(r['failure_class'] for r in diagnostics)),
        'diagnostic_latencies_ms':[r['latency_ms'] for r in diagnostics],
        'validated_status_counts':dict(Counter(r['prediction'] for r in rows if r['assessment'])),
        'false_sufficient_observed_developer_set':sum(r['expected_status']=='INSUFFICIENT' and r['prediction']=='SUFFICIENT' for r in rows if r['assessment']),
        'false_insufficient_observed_developer_set':sum(r['expected_status']=='SUFFICIENT' and r['prediction']=='INSUFFICIENT' for r in rows if r['assessment']),
        'confusion_scope':'Only two valid judgments: one sufficient positive, one causal-negative. Unavailable outputs excluded and separately counted. No generalized accuracy.',
        'gap_findings':gap_findings,'live_shadow_population_skipped_ratio':'NOT_MEASURED'})
    save(out/'per-case-comparison.json',[{'id':c['id'],'question':c['input']['question'],'review':c['review'],
        'original':next((r for r in original if r['id']==c['id']),None),
        'longer_wait':next((r for r in diagnostics if r['id']==c['id']),None)}
        for c in read(out/'frozen-review-v1.json')['cases']])
    class ProbeModel:
        async def assess(self,*a,**kw):
            return {'status':'UNKNOWN','reason_code':'UNCERTAIN_SEMANTIC_SUPPORT','covered_requirements':[],
                    'missing_requirements':[],'candidate_references':[]}
    inp=read(out/'frozen-review-v1.json')['cases'][0]['input']
    messages=EvidenceReflectionModelAdapter.messages(inp['question'],tuple(inp['candidates']))
    grid=[]
    # Executes the actual R3 gate with a no-network model, not a new formula.
    for route in ['VIDEO','MIXED']:
        for memory in [False,True]:
            for attempts in [1,3]:
                for ceiling in [25000,50000,200000]:
                    for seconds in [39,40,54,55,60]:
                        with AgentExecutionBudget.open(seconds*1000,monotonic=lambda:0),follow_up_budget_scope(FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED,ceiling)),capture_evidence_reflection() as records:
                            await EvidenceReflection(ProbeModel(),ReflectionMode.SHADOW).observe(inp['question'],tuple(inp['candidates']),mixed=route=='MIXED',messages=messages,
                                answer_reserve=attempts*(30000+(8000 if route=='MIXED' else 0)+(12000 if memory else 0)),time_reserve=35+(15 if route=='MIXED' else 0))
                        grid.append({'route':route,'memory':memory,'downstream_attempts':attempts,'token_ceiling':ceiling,'remaining_seconds':seconds,'outcome':records[0].outcome})
    save(out/'admission-grid.json',{'type':'DETERMINISTIC_SCENARIOS_NOT_LIVE_POPULATION','already_charged_tokens':0,'input':'frozen complete example','grid':grid})
    print(json.dumps(read(out/'aggregate.json'),ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--output',required=True,type=Path)
    asyncio.run(analyze(p.parse_args().output))
