"""R6 HTTP semantics and completion recovery through the actual API routes."""
import pytest
import asyncio
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from dovideo.application.errors import MediaPayloadTooLarge, MediaStorageFailure, MediaUnauthorized
from dovideo.presentation.api import create_app


@pytest.fixture
def api(tmp_path):
    with TestClient(create_app(work_dir=tmp_path)) as client:
        registered = client.post('/user/register', json={'username': 'r6_user', 'password': 'password123', 'nickname': 'R6'})
        assert registered.status_code == 200
        logged_in = client.post('/user/login', json={'username': 'r6_user', 'password': 'password123'})
        assert logged_in.status_code == 200
        token = logged_in.json()['data']['token']
        yield client, {'Authorization': f'Bearer {token}'}


def test_missing_file_unsupported_extension_and_auth_have_correct_http_semantics(api):
    client, headers = api
    assert client.post('/media/upload', headers=headers).status_code == 400
    assert client.post('/media/upload', files={'file': ('fake.exe', b'video', 'video/mp4')}, headers=headers).status_code == 415
    assert client.post('/media/init-upload', params={'filename': 'fake.exe', 'totalChunks': 1}, headers=headers).status_code == 415
    assert client.post('/media/upload', files={'file': ('clip.mp4', b'video', 'video/mp4')}).status_code == 401


@pytest.mark.parametrize('exception,expected', [(MediaPayloadTooLarge('bounded'), 413), (MediaUnauthorized('private owner'), 403), (MediaStorageFailure('private storage'), 503)])
def test_typed_upload_failures_preserve_http_status_without_leaking_infrastructure(api, exception, expected):
    client, headers = api
    async def failed_status(*args): raise exception
    client.app.state.services.uploads.status = failed_status
    response = client.get('/media/upload-status', params={'uploadId': 'test'}, headers=headers)
    assert response.status_code == expected
    assert 'private' not in response.text


def test_lost_complete_response_recovers_one_local_media_and_rejects_late_chunks(api):
    client, headers = api
    upload_id = client.post('/media/init-upload', params={'filename': 'clip.mp4', 'totalChunks': 1}, headers=headers).json()['data']['uploadId']
    form = {'uploadId': upload_id, 'chunkIndex': 0, 'totalChunks': 1}
    assert client.post('/media/upload-chunk', data=form, files={'file': ('part', b'video')}, headers=headers).status_code == 200
    # Intentionally discard the first completion response, then read status.
    client.post('/media/complete-upload', params={'uploadId': upload_id}, headers=headers)
    status = client.get('/media/upload-status', params={'uploadId': upload_id}, headers=headers).json()['data']
    completed_id = status['completedMediaId']
    again = client.post('/media/complete-upload', params={'uploadId': upload_id}, headers=headers)
    assert again.json()['data']['id'] == completed_id
    assert len(client.get('/media/list', headers=headers).json()['data']) == 1
    assert client.post('/media/upload-chunk', data=form, files={'file': ('part', b'late')}, headers=headers).status_code == 409


@pytest.mark.asyncio
async def test_completion_releases_chunks_and_ttl_cleanup_evicts_completed_and_abandoned_sessions(tmp_path):
    from dovideo.presentation.api.runtime import LocalR1Services, R1ServiceError
    from dovideo.application.media import UploadSession
    services = LocalR1Services(work_dir=tmp_path)
    uploads = services.uploads
    completed = await uploads.init(11, 'clip.mp4', 1)
    await uploads.put_chunk(completed, 11, 0, 1, b'video')
    _, status = await uploads.complete(completed, 11)
    assert uploads.values[completed].chunks == {}
    remaining = status.session.expires_at - datetime.now(timezone.utc)
    assert timedelta(hours=23) < remaining <= timedelta(hours=24)
    abandoned = await uploads.init(11, 'abandoned.mp4', 1)
    await uploads.put_chunk(abandoned, 11, 0, 1, b'pending')
    for upload_id in (completed, abandoned):
        session = uploads.values[upload_id].session
        uploads.values[upload_id].session = UploadSession(
            upload_id=session.upload_id, filename=session.filename, total_chunks=session.total_chunks,
            user_id=session.user_id, created_at=datetime.now(timezone.utc) - timedelta(days=2),
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1), state=session.state,
        )
    await uploads.init(11, 'fresh.mp4', 1)
    assert completed not in uploads.values and completed not in uploads.markers
    assert abandoned not in uploads.values
    with pytest.raises(R1ServiceError) as error: await uploads.status(completed, 11)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_concurrent_completion_rejects_second_merge_and_recovers_same_media(tmp_path):
    from dovideo.presentation.api.runtime import LocalR1Services, R1ServiceError
    from dataclasses import replace
    services = LocalR1Services(work_dir=tmp_path)
    uploads = services.uploads
    upload_id = await uploads.init(11, 'clip.mp4', 1)
    await uploads.put_chunk(upload_id, 11, 0, 1, b'video')
    entered, release = asyncio.Event(), asyncio.Event()
    original = services.media.ingest
    async def pending(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    services.media.ingest = pending
    first = asyncio.create_task(uploads.complete(upload_id, 11))
    await entered.wait()
    try:
        # The original session may expire during an accepted merge. Status
        # reads must not evict the in-flight reservation or its recovery data.
        session = uploads.values[upload_id].session
        uploads.values[upload_id].session = replace(
            session, created_at=datetime.now(timezone.utc) - timedelta(days=2),
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        assert (await uploads.status(upload_id, 11)).completed_media_id is None
        with pytest.raises(R1ServiceError) as error: await uploads.complete(upload_id, 11)
        assert error.value.status_code == 409
    finally:
        release.set()
    record, _ = await first
    recovered, _ = await uploads.complete(upload_id, 11)
    assert recovered.media_id == record.media_id
    assert len(services.media.values) == 1
