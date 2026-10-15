import pytest
from dovideo.infrastructure.providers import ProviderConfig, ModelRequestSettings, OpenAICompatibleChatClient
from dovideo.infrastructure.r4_runtime import _query_rewrite_request_settings_from_environment


def config(**changes):
    return ProviderConfig(base_url='https://openrouter.ai/api/v1',model=changes.get('model','deepseek/deepseek-v4.1-flash'),api_key='fixture',transport='openrouter',provider_only=changes.get('pin',('siliconflow/fp8',)),provider_data_collection='deny',provider_zdr=True)


def test_query_rewrite_override_is_opt_in_and_rejects_unvalidated_values():
    assert _query_rewrite_request_settings_from_environment({}) is None
    assert _query_rewrite_request_settings_from_environment({'DOVIDEO_QUERY_REWRITE_REASONING_EFFORT':'none'}).reasoning_effort=='none'
    with pytest.raises(ValueError): _query_rewrite_request_settings_from_environment({'DOVIDEO_QUERY_REWRITE_REASONING_EFFORT':'high'})


def test_query_rewrite_setting_cannot_change_other_roles_or_output_caps():
    client=OpenAICompatibleChatClient(config(),request_settings=ModelRequestSettings(reasoning_effort='high',max_tokens=4096),query_rewrite_request_settings=ModelRequestSettings(reasoning_effort='none'))
    assert client._request_settings_for_stage('QUERY_REWRITE').request_fields()=={'reasoning_effort':'none','max_tokens':4096}
    for stage in ['FOLLOW_UP','ROLLING_SUMMARY','PLANNER','EXECUTOR','CRITIC','EVIDENCE_REFLECTION']:
        assert client._request_settings_for_stage(stage).request_fields()==client.request_settings.request_fields()


@pytest.mark.parametrize('changes',[{'model':'another/model'},{'pin':('another/provider',)}])
def test_unvalidated_provider_cannot_enable_query_rewrite_override(changes):
    with pytest.raises(ValueError): OpenAICompatibleChatClient(config(**changes),query_rewrite_request_settings=ModelRequestSettings(reasoning_effort='none'))


@pytest.mark.asyncio
async def test_real_query_rewrite_payload_retains_512_cap_and_provider_restrictions():
    from dovideo.infrastructure.providers.http import ProviderHttpResponse
    class Http:
        async def post(self,url,*,headers,json,timeout):
            self.payload=json
            return ProviderHttpResponse(200,{'choices':[{'message':{'content':'{}'},'finish_reason':'stop'}]})
    http=Http()
    client=OpenAICompatibleChatClient(config(),client=http,query_rewrite_request_settings=ModelRequestSettings(reasoning_effort='none'))
    await client.complete(({'role':'user','content':'untrusted fixture'},),stage='QUERY_REWRITE')
    payload=http.payload
    assert payload['max_tokens']==512
    assert payload['reasoning_effort']=='none'
    assert payload['provider']['only']==['siliconflow/fp8']
    assert payload['provider']['allow_fallbacks'] is False
    assert payload['provider']['data_collection']=='deny' and payload['provider']['zdr'] is True
