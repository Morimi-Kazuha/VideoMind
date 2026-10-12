"""Offline evaluation safeguards; no real credentials or network."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
from reflection_validation_support import load_chat_environment, RecordingHttp, attach_review, CHAT_NAMES
from run_r35_reflection_validation import run
from argparse import Namespace
from dovideo.infrastructure.providers.http import ProviderHttpResponse


def test_explicit_environment_only_chat_names_and_preserve_process(monkeypatch,tmp_path):
    for name in CHAT_NAMES: monkeypatch.delenv(name,raising=False)
    p=tmp_path/'fake.env'; p.write_text("DOVIDEO_MODEL_API_KEY='fixture-secret'\nDOVIDEO_MODEL_MODEL=fixture\nOTHER_PROJECT_SECRET=forbidden\nDOVIDEO_SQL_PASSWORD=ignored\n",encoding='utf-8')
    monkeypatch.setenv('DOVIDEO_MODEL_MODEL','inherited')
    result=load_chat_environment([p])
    import os
    assert os.environ['DOVIDEO_MODEL_MODEL']=='inherited'
    assert os.environ['DOVIDEO_MODEL_API_KEY']=='fixture-secret'
    assert 'fixture-secret' not in json.dumps(result)
    assert 'OTHER_PROJECT_SECRET' not in os.environ and 'DOVIDEO_SQL_PASSWORD' not in os.environ
    assert p.read_text(encoding='utf-8').startswith("DOVIDEO_MODEL_API_KEY='fixture-secret'")
    monkeypatch.delenv('DOVIDEO_MODEL_API_KEY')


@pytest.mark.asyncio
async def test_transport_never_records_headers_secret_or_arbitrary_error_metadata():
    transport=RecordingHttp('fixture-secret')
    class Client:
        async def post(self,*a,**kw):
            return ProviderHttpResponse(400,{'error':{'code':400,'message':'fixture-secret rejected','metadata':{'Authorization':'fixture-secret'}}})
    transport.client=Client()
    await transport.post('https://example.test',headers={'Authorization':'fixture-secret'})
    saved=json.dumps(transport.records)
    assert 'fixture-secret' not in saved and 'Authorization' not in saved
    assert transport.records[0]['safe_provider_error']['message']=='[REDACTED] rejected'


@pytest.mark.asyncio
async def test_usage_refusal_and_raw_output_are_distinct():
    transport=RecordingHttp('fixture-secret')
    class Client:
        async def post(self,*a,**kw):
            return ProviderHttpResponse(200,{'choices':[{'message':{'content':'{}','refusal':'cannot grade'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':42,'completion_tokens':4,'total_tokens':46,'private_metadata':'excluded',
                    'completion_tokens_details':{'reasoning_tokens':3,'private_reasoning':'not recorded'}}})
    transport.client=Client(); await transport.post('https://example.test')
    r=transport.records[0]
    assert r['raw_structured_response']=='{}' and r['refusal']=='cannot grade'
    assert r['usage']=={'prompt_tokens':42,'completion_tokens':4,'total_tokens':46}
    assert r['reasoning_tokens']==3 and 'private_reasoning' not in json.dumps(r)


def test_reviews_have_no_independent_human_claim_and_keep_original_labels():
    sidecar=json.loads((ROOT/'tests/fixtures/agentic-rag/r3/sidecar-v1.json').read_text(encoding='utf-8'))
    original=json.loads(json.dumps(sidecar))
    reviewed=attach_review(sidecar)
    for a,b in zip(original['cases'],reviewed['cases']):
        assert a['input']==b['input'] and a['expected_status']==b['expected_status']
        assert not b['review']['independent_human_review'] and b['review']['necessary_facts']
    assert all(c['expected_status'] is None for c in reviewed['cases'] if c['id'].startswith('tc-'))


@pytest.mark.asyncio
@pytest.mark.parametrize('limit',[0,31])
async def test_campaign_call_limit_enforced_before_any_network(tmp_path,limit):
    args=Namespace(max_calls=limit,max_total_tokens=25000,output=tmp_path/'output')
    with pytest.raises(ValueError): await run(args)
    assert not args.output.exists()
