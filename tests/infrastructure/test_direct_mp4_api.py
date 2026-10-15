from fastapi.testclient import TestClient
from dovideo.presentation.api import create_app
from test_direct_mp4 import transport, Reply, DNS, MP4
from dovideo.infrastructure.media.url import DirectMp4Downloader


def test_import_auth_ownership_playback_and_existing_analysis(monkeypatch,tmp_path):
    transport(monkeypatch,[Reply()])
    monkeypatch.setattr('dovideo.infrastructure.media.url.SystemDnsResolver', DNS)
    with TestClient(create_app(work_dir=tmp_path)) as client:
        def login(name):
            assert client.post('/user/register',json={'username':name,'password':'password123','nickname':name}).status_code==200
            token=client.post('/user/login',json={'username':name,'password':'password123'}).json()['data']['token']
            return {'Authorization':'Bearer '+token}
        a,b=login('url_owner'),login('url_other')
        endpoint='/media/upload-url'
        params={'url':'https://public.example/a.mp4'}
        assert client.post(endpoint,params=params).status_code==401
        imported=client.post(endpoint,params=params,headers=a)
        assert imported.status_code==200,imported.text
        media_id=imported.json()['data']['id']
        assert client.app.state.services.media.path_for(media_id).read_bytes()==MP4
        assert len(client.get('/media/list',headers=a).json()['data'])==1
        assert client.get('/media/list',headers=b).json()['data']==[]
        assert client.get('/media/playback',params={'id':media_id},headers=a).status_code==200
        assert client.get('/media/playback',params={'id':media_id},headers=b).status_code==403
        assert client.post('/analysis/ai',params={'id':media_id},headers=b).status_code==403
        accepted=client.post('/analysis/ai',params={'id':media_id},headers=a)
        assert accepted.status_code in (200,202),accepted.text



def test_import_storage_failure_has_honest_chinese_error(tmp_path):
    from dovideo.application.errors import MediaStorageFailure
    with TestClient(create_app(work_dir=tmp_path)) as client:
        client.post('/user/register',json={'username':'storage_error','password':'password123','nickname':'test'})
        token=client.post('/user/login',json={'username':'storage_error','password':'password123'}).json()['data']['token']
        async def failed(*args): raise MediaStorageFailure('private storage details')
        client.app.state.services.ingest_url=failed
        response=client.post('/media/upload-url',params={'url':'https://public.example/a.mp4'},headers={'Authorization':'Bearer '+token})
        assert response.status_code==503
        assert response.json()['message']=='视频保存失败，请稍后重试'
        assert 'private' not in response.text and '保留' not in response.text
