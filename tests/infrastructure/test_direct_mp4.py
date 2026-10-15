import asyncio
from pathlib import Path
import aiohttp
import pytest
from dovideo.infrastructure.media.url import DirectMp4Downloader, _PinnedResolver
from dovideo.infrastructure.media.workspace import MediaWorkspace
from dovideo.infrastructure.media.ingest import MediaIngestService
from dovideo.infrastructure.media.memory import InMemoryMediaRecordStore, InMemoryObjectStorage
from dovideo.application.errors import UrlValidationError, UrlDownloadFailure

MP4 = b'\x00\x00\x00\x20ftypisom' + b'\x00' * 40
class DNS:
    def __init__(self, addresses=('8.8.8.8',)):
        self.addresses, self.calls = addresses, []
    async def resolve(self, host):
        self.calls.append(host)
        return self.addresses
class Reply:
    def __init__(self, body=MP4, status=200, headers=None, length=None, failure=None):
        self.body, self.status, self.failure = body, status, failure
        self.headers = headers or {'Content-Type': 'video/mp4'}
        self.content_length = len(body) if length is None else length
        self.content = self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def iter_chunked(self, size):
        yield self.body[:16]
        if self.failure: raise self.failure
        yield self.body[16:]

def transport(monkeypatch, replies):
    calls = []
    class Session:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False
            assert kwargs['auto_decompress'] is False
            assert isinstance(kwargs['cookie_jar'], aiohttp.DummyCookieJar)
            self.connector = kwargs['connector']
        async def __aenter__(self): return self
        async def __aexit__(self, *args): await self.connector.close()
        def get(self, url, **kwargs):
            assert kwargs['allow_redirects'] is False
            calls.append(url)
            return replies.pop(0)
    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    return calls

@pytest.mark.asyncio
async def test_stream_to_existing_media_storage_and_cleanup(monkeypatch, tmp_path):
    transport(monkeypatch, [Reply()])
    records, storage = InMemoryMediaRecordStore(), InMemoryObjectStorage()
    ingest = MediaIngestService(storage, records, DirectMp4Downloader(DNS()), workspace_parent=tmp_path)
    record = await ingest.ingest_url('https://public.example/clip.mp4', 7)
    assert record.user_id == 7 and record.media_id == 1
    assert list(storage.objects.values()) == [MP4]
    assert await records.get(1) == record
    assert list(tmp_path.iterdir()) == []

@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['file:///tmp/a.mp4','http://public.example/a.mp4','https://public.example/watch','https://u:p@public.example/a.mp4','https://public.example:8443/a.mp4','https://public.example/a.mp4#x'])
async def test_invalid_url_never_connects(tmp_path, url):
    dns=DNS()
    async with MediaWorkspace(parent=tmp_path) as workspace:
        with pytest.raises(UrlValidationError): await DirectMp4Downloader(dns).download(url,workspace)
    assert dns.calls == [] and list(tmp_path.iterdir()) == []

@pytest.mark.asyncio
@pytest.mark.parametrize('ip', ['127.0.0.1','10.0.0.1','169.254.169.254','100.100.100.200','192.0.0.1','::1','::ffff:127.0.0.1','2002:7f00:1::','fe80::1','64:ff9b::a00:1','168.63.129.16'])
async def test_private_and_metadata_blocked(tmp_path, ip):
    async with MediaWorkspace(parent=tmp_path) as workspace:
        with pytest.raises(UrlValidationError):
            await DirectMp4Downloader(DNS((ip,))).download('https://public.example/a.mp4', workspace)

@pytest.mark.asyncio
async def test_pinning_does_not_resolve_again():
    resolver=_PinnedResolver('public.example', ('8.8.8.8',))
    assert (await resolver.resolve('public.example'))[0]['host'] == '8.8.8.8'
    with pytest.raises(UrlValidationError): await resolver.resolve('other.example')

@pytest.mark.asyncio
@pytest.mark.parametrize('reply,error', [
    (Reply(status=404),UrlDownloadFailure),
    (Reply(headers={'Content-Type':'text/html'}),UrlValidationError),
    (Reply(body=b'<html>not an mp4</html>'),UrlValidationError),
    (Reply(length=1000),UrlDownloadFailure),
    (Reply(failure=aiohttp.ClientPayloadError()),UrlDownloadFailure),
    (Reply(failure=TimeoutError()),UrlDownloadFailure),
    (Reply(failure=asyncio.CancelledError()),asyncio.CancelledError),
])
async def test_failures_cleanup(monkeypatch, tmp_path, reply, error):
    transport(monkeypatch,[reply])
    async with MediaWorkspace(parent=tmp_path) as workspace:
        with pytest.raises(error): await DirectMp4Downloader(DNS()).download('https://public.example/a.mp4', workspace)
        assert list((workspace.path/'url').iterdir()) == []
    assert list(tmp_path.iterdir()) == []

@pytest.mark.asyncio
@pytest.mark.parametrize('known', [True,False])
async def test_size_limit(monkeypatch,tmp_path,known):
    reply=Reply()
    if not known: reply.content_length=None
    transport(monkeypatch,[reply])
    async with MediaWorkspace(parent=tmp_path) as workspace:
        with pytest.raises(UrlValidationError): await DirectMp4Downloader(DNS(),max_bytes=20).download('https://public.example/a.mp4',workspace)
        assert list((workspace.path/'url').iterdir()) == []

@pytest.mark.asyncio
async def test_redirect_revalidates_dns_and_blocks_rebinding(monkeypatch,tmp_path):
    calls=transport(monkeypatch,[Reply(status=302,headers={'Location':'https://public.example/b.mp4'})])
    class Rebinding:
        count=0
        async def resolve(self,host):
            self.count+=1
            return ('8.8.8.8',) if self.count==1 else ('127.0.0.1',)
    async with MediaWorkspace(parent=tmp_path) as workspace:
        with pytest.raises(UrlValidationError): await DirectMp4Downloader(Rebinding()).download('https://public.example/a.mp4',workspace)
    assert len(calls)==1

@pytest.mark.asyncio
@pytest.mark.parametrize('location', ['https://127.0.0.1/a.mp4','http://public.example/a.mp4','file:///a.mp4','https://u:p@public.example/a.mp4'])
async def test_redirect_policy(monkeypatch,tmp_path,location):
    calls=transport(monkeypatch,[Reply(status=302,headers={'Location':location})])
    dns=DNS(('127.0.0.1',)) if '127.' in location else DNS()
    if '127.' in location:
        class HostDNS:
            async def resolve(self,host): return ('127.0.0.1',) if host=='127.0.0.1' else ('8.8.8.8',)
        dns=HostDNS()
    async with MediaWorkspace(parent=tmp_path) as workspace:
        with pytest.raises(UrlValidationError): await DirectMp4Downloader(dns).download('https://public.example/a.mp4',workspace)
    assert len(calls)==1

@pytest.mark.asyncio
async def test_dns_timeout_and_mixed_addresses(tmp_path):
    class Slow:
        async def resolve(self,host): await asyncio.sleep(1)
    async with MediaWorkspace(parent=tmp_path) as workspace:
        with pytest.raises(UrlDownloadFailure): await DirectMp4Downloader(Slow(),timeout=.01).download('https://public.example/a.mp4',workspace)
        with pytest.raises(UrlValidationError): await DirectMp4Downloader(DNS(('8.8.8.8','10.0.0.1'))).download('https://public.example/a.mp4',workspace)


@pytest.mark.asyncio
async def test_public_redirect_succeeds_without_forwarding_credentials(monkeypatch,tmp_path):
    calls=transport(monkeypatch,[Reply(status=302,headers={'Location':'https://cdn.example/final.mp4','Set-Cookie':'secret=x'}),Reply()])
    dns=DNS()
    async with MediaWorkspace(parent=tmp_path) as workspace:
        result=await DirectMp4Downloader(dns).download('https://public.example/a.mp4',workspace)
        assert result.path.read_bytes()==MP4
    assert len(calls)==2 and dns.calls==['public.example','cdn.example']


@pytest.mark.asyncio
async def test_redirect_loop_is_bounded(monkeypatch,tmp_path):
    calls=transport(monkeypatch,[Reply(status=302,headers={'Location':'/a.mp4'}) for _ in range(4)])
    async with MediaWorkspace(parent=tmp_path) as workspace:
        with pytest.raises(UrlDownloadFailure): await DirectMp4Downloader(DNS()).download('https://public.example/a.mp4',workspace)
    assert len(calls)==4


@pytest.mark.asyncio
async def test_record_failure_rolls_back_object_and_workspace(monkeypatch,tmp_path):
    transport(monkeypatch,[Reply()])
    records,storage=InMemoryMediaRecordStore(),InMemoryObjectStorage()
    records.save_error=RuntimeError('database unavailable')
    ingest=MediaIngestService(storage,records,DirectMp4Downloader(DNS()),workspace_parent=tmp_path)
    with pytest.raises(RuntimeError): await ingest.ingest_url('https://public.example/a.mp4',7)
    assert storage.objects=={} and len(storage.delete_calls)==1
    assert list(tmp_path.iterdir())==[]
