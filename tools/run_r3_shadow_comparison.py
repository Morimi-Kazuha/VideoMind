"""Reproduce fixture HTTP costs and latency; no semantic accuracy claims."""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests/application')]
from test_r3_evidence_reflection import reflected,Q,MIXED_Q,QUESTION,ask,ReflectionMode,capture_follow_up_budget,capture_evidence_reflection


async def run(output):
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists(): raise FileExistsError(output)
    results=[]
    for q in [Q,MIXED_Q,QUESTION]:
        pair=[]
        for mode in [ReflectionMode.OFF,ReflectionMode.SHADOW]:
            timings=[]; records=[]; outcomes=[]; answers=[]
            for _ in range(25):
                h,http,calls=reflected(mode)
                with capture_follow_up_budget() as usage,capture_evidence_reflection() as obs:
                    t=time.perf_counter(); answers.append(await ask(h,q)); timings.append((time.perf_counter()-t)*1000)
                records.append(usage[0]); outcomes.extend(o.outcome for o in obs)
                assert len(h[2].queries)==int(q!=QUESTION)
            pair.append({'mode':mode.value,'n':25,'median_ms':statistics.median(timings),'p95_ms':sorted(timings)[23],
                'chat_attempts':sum(x['chat_http_attempts'] for x in records),'chat_tokens':sum(x['charged_chat_tokens'] for x in records),
                'outcomes':outcomes,'answer':answers[0]})
        assert pair[0]['answer']==pair[1]['answer']
        for arm in pair: del arm['answer']
        results.append({'route':{Q:'VIDEO',MIXED_Q:'MIXED',QUESTION:'DIRECT'}[q],'arms':pair,'answer_equal':True})
    output.write_text(json.dumps({'kind':'FIXTURE_HTTP_NOT_SEMANTIC_ACCURACY','results':results},indent=2),encoding='utf-8')
    print('Saved',output)


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--output',required=True,type=Path)
    asyncio.run(run(p.parse_args().output))
