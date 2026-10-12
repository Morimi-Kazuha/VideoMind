"""Offline harness regressions; these are not real-provider benchmark results."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import run_optimization_benchmark as benchmark
import replay_v1_pilot_scores as replay_tool


def test_qdrant_environment_allowlist_and_inherited_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv('DOVIDEO_QDRANT_URL', 'http://inherited')
    monkeypatch.setenv('DOVIDEO_QDRANT_API_KEY', '')
    monkeypatch.delenv('DOVIDEO_QDRANT_API_KEY', raising=False)
    monkeypatch.delenv('DOVIDEO_UNAUTHORIZED_TEST_KEY', raising=False)
    config = tmp_path / 'authorized.env'
    config.write_text('DOVIDEO_QDRANT_URL=http://file\n'
        'DOVIDEO_QDRANT_API_KEY="test-qdrant"\n'
        'DOVIDEO_UNAUTHORIZED_TEST_KEY=never-load\n', encoding='utf-8')
    records = benchmark.environment([config], allowed_prefix='DOVIDEO_QDRANT_')
    assert benchmark.os.environ['DOVIDEO_QDRANT_URL'] == 'http://inherited'
    assert benchmark.os.environ['DOVIDEO_QDRANT_API_KEY'] == 'test-qdrant'
    assert 'DOVIDEO_UNAUTHORIZED_TEST_KEY' not in benchmark.os.environ
    assert set(records[0]['names']) == {'DOVIDEO_QDRANT_URL', 'DOVIDEO_QDRANT_API_KEY'}
    assert 'test-qdrant' not in json.dumps(records)


@pytest.mark.asyncio
async def test_missing_chat_config_reports_failure_without_masking_it(tmp_path, monkeypatch):
    for name in tuple(benchmark.os.environ):
        if name.startswith('DOVIDEO_'):
            monkeypatch.delenv(name)
    result = await benchmark.preflight(tmp_path)
    assert result['chat']['status'] == 'FAILED'
    assert result['chat']['errorType'] != 'UnboundLocalError'
    assert result['chat']['diagnostics'] == []


def test_reported_campaign_cost_is_not_presented_as_complete():
    limit = benchmark.CampaignLimit()
    limit.charge([{'totalTokens': 197, 'providerReportedCost': None}])
    assert limit.as_dict()['reportedOrLabelledUsageTokens'] == 197
    assert limit.as_dict()['costComplete'] is False
    limit.cost = 2
    with pytest.raises(RuntimeError, match='budget exhausted'):
        limit.admit()


def test_jev_transport_is_charged_once_and_missing_usage_stays_unknown():
    chat={'stage':'EXECUTOR','totalTokens':100,'providerReportedCost':0.02,'usageSource':'provider'}
    diagnostic={'kind':'jevRoutingTransport','decisionModel':'jev','routerModel':'jev-version',
        'inputTokens':9,'outputTokens':2,'usageCostUsd':0.001}
    records=[chat,{'stage':'ROUTER','totalTokens':11,'providerReportedCost':0.001}]
    canonical=benchmark.agent_usage_records(records,[diagnostic])
    assert len(canonical)==2
    assert canonical[1]['model']=='jev-version'
    limit=benchmark.CampaignLimit()
    limit.charge(canonical)
    assert limit.tokens==111
    assert limit.cost==pytest.approx(0.021)
    missing=benchmark.agent_usage_records([], [{'kind':'jevRoutingTransport','inputTokens':None,'outputTokens':None}])
    assert missing[0]['totalTokens'] is None
    assert missing[0]['usageSource']=='unreported'


def test_optimization_replay_detects_changed_score(tmp_path, monkeypatch):
    context = SimpleNamespace(source_revision='revision')
    case = SimpleNamespace(case_id='case')
    dataset = SimpleNamespace(dataset_digest='dataset', case=lambda _: case)
    annotations = SimpleNamespace(cases=[SimpleNamespace(case_id='case')])
    metadata = {'datasetDigest':'dataset', 'sourceRevision':'revision',
        'mediaSha256':'media', 'sidecarSha256':'sidecar', 'codeDigests':{}}
    monkeypatch.setattr(replay_tool, 'golden', lambda *_: ({'mediaSha256':'media'}, context, dataset, annotations, {}))
    monkeypatch.setattr(replay_tool, 'digest', lambda _: 'sidecar')
    monkeypatch.setattr(replay_tool, 'load', lambda path: metadata if path.name=='run-metadata.json' else
        [{'caseId':'case','status':'PASS','ranks':[],'score':{'recall_at_k':{'5':1}}}])
    monkeypatch.setattr(replay_tool, 'score_pilot_retrieval', lambda *_: {'recall_at_k':{'5':0}})
    args = SimpleNamespace(gold_dir=tmp_path,media_dir=tmp_path,run_dir=tmp_path,output=None)
    with pytest.raises(ValueError, match='score changed'):
        replay_tool.replay_optimization(args)


def test_optimization_replay_rejects_source_revision_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(replay_tool, 'golden', lambda *_: ({'mediaSha256':'media'},
        SimpleNamespace(source_revision='new-media'), SimpleNamespace(dataset_digest='dataset'), None, {}))
    monkeypatch.setattr(replay_tool, 'load', lambda _: {'datasetDigest':'dataset',
        'sourceRevision':'old-media','mediaSha256':'media','sidecarSha256':'sidecar'})
    args = SimpleNamespace(gold_dir=tmp_path,media_dir=tmp_path,run_dir=tmp_path,output=None)
    with pytest.raises(ValueError, match='identity mismatch'):
        replay_tool.replay_optimization(args)


@pytest.mark.asyncio
async def test_observed_chat_records_execution_identity_without_credentials_or_content():
    class Http:
        async def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=200, body={
                'model':'actual-model', 'provider':'actual-provider', 'id':'generation',
                'choices':[{'message':{'content':'private-answer'}}],
                'usage':{'completion_tokens_details':{'reasoning_tokens':17}}})
    records=[]
    observed=benchmark.ObservedChatHttp(Http(),records)
    await observed.post('endpoint',headers={'Authorization':'Bearer private-key'},
        json={'model':'requested-model','messages':[{'content':'private-source'}],
              'reasoning':{'effort':'max'},'provider':{'only':['supplier/fp8']}},timeout=1)
    assert records[0]['returnedModel']=='actual-model'
    assert records[0]['returnedProvider']=='actual-provider'
    assert records[0]['reasoningTokens']==17
    serialized=json.dumps(records)
    for secret in ('private-key','private-answer','private-source','Authorization'):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_prior_agent_rows_are_reused_only_for_matching_source_identity(tmp_path,monkeypatch):
    prior=tmp_path/'prior'
    current=tmp_path/'current'
    (prior/'agent').mkdir(parents=True)
    current.mkdir()
    metadata={'datasetDigest':'dataset','sidecarSha256':'sidecar','mediaSha256':'media',
        'sourceRevision':'revision','chatConfig':{'providerOnly':['supplier/fp8']},
        'reuseInputs':{'chunks':'hash'},'codeDigests':{'src/model.py':'code'}}
    benchmark.save(prior/'run-metadata.json',metadata)
    benchmark.save(current/'run-metadata.json',metadata)
    benchmark.save(prior/'environment-sources.json',[{'fileName':'authorized.env','sha256':'config'}])
    benchmark.save(current/'environment-sources.json',[{'fileName':'authorized.env','sha256':'config'}])
    rows=[{'caseId':'case','arm':arm,'projection':'stage_projection','laneRequest':'BALANCED',
           'status':'PASS' if arm=='planner_executor' else 'FAILED'}
          for arm in ('planner_executor','with_critic')]
    benchmark.save(prior/'agent/results.json',rows)
    async def never_execute(*args,**kwargs):
        pytest.fail('matching prior results must not generate or bill another answer')
    monkeypatch.setattr(benchmark,'execute_agent',never_execute)
    args=SimpleNamespace(agent_cases='case',prior_agent_run=prior)
    dataset=SimpleNamespace(case=lambda _:SimpleNamespace(case_id='case'))
    result=await benchmark.agent_suite(args,current,None,dataset,None,None,benchmark.CampaignLimit())
    assert len(result['rows'])==2
    assert all(r['reusedResultFrom'] for r in result['rows'])
    mismatch=tmp_path/'mismatch'
    mismatch.mkdir()
    benchmark.save(mismatch/'run-metadata.json',{**metadata,'sourceRevision':'different-video'})
    with pytest.raises(ValueError,match='identity mismatch: sourceRevision'):
        await benchmark.agent_suite(args,mismatch,None,dataset,None,None,benchmark.CampaignLimit())
    benchmark.save(mismatch/'run-metadata.json',metadata)
    benchmark.save(mismatch/'environment-sources.json',[{'fileName':'authorized.env','sha256':'different-key-config'}])
    with pytest.raises(ValueError,match='environment source mismatch'):
        benchmark.reusable_agent_rows(args,mismatch)
