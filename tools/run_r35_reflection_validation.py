"""Instrumented small R3 validation. Reuses frozen inputs and the actual grader.

No retrieval, answer generation, prompt tuning or production policy overrides.
"""
import argparse
import asyncio
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from run_r3_evidence_reflection import read, save
from reflection_validation_support import load_chat_environment, public_config, RecordingHttp, attach_review
from dovideo.application.evidence_reflection import EvidenceReflection, EvidenceAssessment, ReflectionObservation, ReflectionMode, capture_evidence_reflection
from dovideo.application.follow_up_budget import FollowUpBudgetMode,FollowUpBudgetSettings,follow_up_budget_scope,capture_follow_up_budget,estimate_chat_input,optional_model_headroom
from dovideo.application.execution_budget import AgentExecutionBudget
from dovideo.application.errors import BudgetExceededError,DeadlineExceededError
from dovideo.infrastructure.providers import ProviderConfig,OpenAICompatibleChatClient
from dovideo.infrastructure.providers.evidence_reflection import EvidenceReflectionModelAdapter,SYSTEM


async def run(args):
    if not 1<=args.max_calls<=30 or not 1024<=args.max_total_tokens<=50000:
        raise ValueError("bounded campaign limit required")
    out=args.output; out.mkdir(parents=True,exist_ok=False)
    sources=load_chat_environment(args.env_file)
    if args.eval_provider_tag:
        # Same explicit project evaluation override as run_optimization_benchmark.
        import os
        os.environ['DOVIDEO_MODEL_PROVIDER_TAG']=args.eval_provider_tag
    sidecar=attach_review(read(args.sidecar))
    if args.case_ids:
        selected=set(args.case_ids.split(',')); sidecar['cases']=[c for c in sidecar['cases'] if c['id'] in selected]
        if len(sidecar['cases'])!=len(selected): raise ValueError('unknown case id')
    if len(sidecar['cases'])>args.max_calls: raise ValueError('too many cases')
    save(out/'reviewed-sidecar-v1.json',sidecar)
    config=ProviderConfig.from_environment()
    live=bool(config and config.api_key)
    transport=RecordingHttp(config.api_key if config else None)
    local_timeout=getattr(args,'diagnostic_timeout',5)
    adapter=EvidenceReflectionModelAdapter(OpenAICompatibleChatClient(replace(config,max_attempts=1,timeout_seconds=local_timeout),client=transport)) if live else None
    save(out/'configuration.json',{'provider':public_config(config),'sources':sources,'prompt_sha256':hashlib.sha256(SYSTEM.encode()).hexdigest(),
        'original_sidecar_sha256':hashlib.sha256(args.sidecar.read_bytes()).hexdigest(),
        'reviewed_sidecar_sha256':hashlib.sha256((out/'reviewed-sidecar-v1.json').read_bytes()).hexdigest(),
        'max_calls':args.max_calls,'campaign_token_ceiling':args.max_total_tokens,'per_case_deadline_seconds':60,'local_timeout_seconds':local_timeout,
        'evaluation_mode':'ORIGINAL_5S_POLICY' if local_timeout==5 else 'OFFLINE_LONGER_WAIT_DIAGNOSTIC',
        'policy':'offline reflection only; no downstream answer reserve needed; original Shadow gates analyzed separately'})
    results=[]; spent=0; attempts=0; stop=None
    for case in sidecar['cases']:
        inp=case['input']; candidates=tuple(inp['candidates'])
        messages=EvidenceReflectionModelAdapter.messages(inp['question'],candidates,mixed=inp['mixed'])
        estimated=estimate_chat_input(messages); start=len(transport.records); started=time.perf_counter()
        # Campaign and per-request limits use the same existing request ledger.
        remaining=args.max_total_tokens-spent
        observation=None; usage=None; terminal=None; time_before=None; time_after=None
        if stop:
            outcome=stop
        else:
            with capture_evidence_reflection() as obs,capture_follow_up_budget() as records:
                try:
                    with AgentExecutionBudget.open(60000),follow_up_budget_scope(FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED,max(1,remaining))):
                        time_before=AgentExecutionBudget.remaining_seconds()
                        if local_timeout==5:
                            await EvidenceReflection(adapter,ReflectionMode.SHADOW).observe(inp['question'],candidates,mixed=inp['mixed'],messages=messages,answer_reserve=0,time_reserve=0)
                        else:
                            # Explicitly authorized offline diagnostic. Same
                            # adapter/prompt/admission/cap, no production changes.
                            assessment=EvidenceAssessment(None); diagnostic_outcome='MODEL_UNAVAILABLE'
                            try:
                                if not candidates:
                                    diagnostic_outcome='NO_CANDIDATES'
                                elif adapter is not None and optional_model_headroom(messages,'EVIDENCE_REFLECTION',1024,0):
                                    async with asyncio.timeout(local_timeout):
                                        proposal=await adapter.assess(inp['question'],candidates,mixed=inp['mixed'])
                                    assessment=EvidenceAssessment(proposal); diagnostic_outcome='ASSESSED'
                                elif adapter is not None:
                                    diagnostic_outcome='SKIPPED_BUDGET'
                            except (BudgetExceededError,DeadlineExceededError): raise
                            except TimeoutError: diagnostic_outcome='TIMEOUT'
                            except (ValueError,TypeError): diagnostic_outcome='INVALID_RESPONSE'
                            except Exception: diagnostic_outcome='PROVIDER_FAILURE'
                            observation=ReflectionObservation(assessment,diagnostic_outcome,(time.perf_counter()-started)*1000)
                        time_after=AgentExecutionBudget.remaining_seconds()
                except (BudgetExceededError,DeadlineExceededError) as exc:
                    terminal=type(exc).__name__
                observation=obs[0] if obs else observation
                usage=records[0] if records else None
            outcome='TERMINAL_BUDGET_OR_DEADLINE' if terminal else observation.outcome
        http=transport.records[start:]; sent=len(http); attempts+=sent
        spent+=usage['charged_chat_tokens'] if usage else 0
        suggestion=observation.assessment.suggestion if observation else None
        admission=next((e for e in (usage or {}).get('events',[]) if e['kind']=='admission'),None)
        row={'id':case['id'],'label_origin':case['label_origin'],'review':case['review'],'expected_status':case['expected_status'],
            'input_sha256':hashlib.sha256(json.dumps(inp,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
            'outcome':outcome,'prediction':observation.assessment.status if observation else None,
            'assessment':suggestion.model_dump() if suggestion else None,'terminal':terminal,
            'request_remaining_seconds_at_entry':time_before,'request_remaining_seconds_after':time_after,'latency_ms':(time.perf_counter()-started)*1000,
            'estimated_input_tokens':estimated,'output_reserve':1024,'campaign_tokens_remaining_before':remaining,
            'admission':admission,'budget':usage,'http_attempts':sent,'http':http}
        results.append(row); save(out/'results.json',results)
        print(case['id'],outcome,row['prediction'],f"{row['latency_ms']:.0f}ms",flush=True)
        if any(h['status_code'] in {401,403} for h in http): stop='SKIPPED_PROVIDER_AUTH'
        if attempts>=args.max_calls: stop='SKIPPED_CALL_LIMIT'
        if spent>=args.max_total_tokens or terminal: stop='SKIPPED_CAMPAIGN_BUDGET'
    measured=[(c,r) for c,r in zip(sidecar['cases'],results) if c['expected_status'] and r['assessment']]
    matrix=Counter((c['expected_status'],r['prediction']) for c,r in measured)
    save(out/'summary.json',{'provider_configured':live,'http_attempts':attempts,'charged_tokens':spent,'outcomes':dict(Counter(r['outcome'] for r in results)),
        'assessed':sum(r['outcome']=='ASSESSED' for r in results),'developer_checked_assessed':len(measured),'independent_human_reviewed':0,
        'confusion':[{'expected':a,'predicted':b,'count':n} for (a,b),n in matrix.items()] if measured else None,
        'false_sufficient':sum(c['expected_status']=='INSUFFICIENT' and r['prediction']=='SUFFICIENT' for c,r in measured) if measured else None,
        'false_insufficient':sum(c['expected_status']=='SUFFICIENT' and r['prediction']=='INSUFFICIENT' for c,r in measured) if measured else None,
        'incomplete_judgments':sum(r['outcome'] not in {'ASSESSED','NO_CANDIDATES'} for r in results),
        'note':'Developer checked small set, not independent generalized accuracy. Non-assessed predictions UNKNOWN are not model semantic judgments.'})


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--output',required=True,type=Path); p.add_argument('--env-file',action='append',type=Path,default=[])
    p.add_argument('--sidecar',type=Path,default=ROOT/'docs/agentic-rag/r3-validation-v1/evaluation-final/sidecar-v1.json')
    p.add_argument('--case-ids'); p.add_argument('--max-calls',type=int,default=15); p.add_argument('--max-total-tokens',type=int,default=25000)
    p.add_argument('--eval-provider-tag', choices=['siliconflow/fp8'], help='Existing documented VideoMind offline evaluation provider override; process only')
    p.add_argument('--diagnostic-timeout', type=int, choices=[5,20], default=5, help='20s requires separate offline diagnostic authorization; never alters production')
    asyncio.run(run(p.parse_args()))
