"""A persisted digest alone never proves the current object's identity."""
import hashlib
import json

from alembic import command
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from dovideo.infrastructure.media_identity import MediaIdentityStore
from dovideo.infrastructure.persistence.migrations import migration_config, upgrade_schema
from dovideo.infrastructure.persistence.sqlalchemy import MediaRow, create_sqlalchemy_engine

SOURCE = "minio://media/videos/owned.mp4"
DIGEST = hashlib.sha256(b"verified video").hexdigest()


@pytest.fixture
def engine(tmp_path):
    value = create_sqlalchemy_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    upgrade_schema(value)
    with Session(value) as session, session.begin():
        session.add(MediaRow(
            id=10, user_id=1, filename="video.mp4", status="COMPLETED",
            file_path=SOURCE, content_hash=DIGEST,
        ))
    yield value
    value.dispose()


@pytest.mark.asyncio
async def test_binding_is_optional_and_guarded_by_record_source_and_sha256(engine):
    store = MediaIdentityStore(engine)
    assert await store.load(10, SOURCE, DIGEST) is None
    await store.bind(10, SOURCE, DIGEST, "version-verified")
    assert await store.load(10, SOURCE, DIGEST) == "version-verified"
    assert await store.load(11, SOURCE, DIGEST) is None
    assert await store.load(10, SOURCE + "-changed", DIGEST) is None
    assert await store.load(10, SOURCE, "a" * 64) is None
    # A record edit between download and bind must not publish a stale proof.
    with Session(engine) as session, session.begin():
        session.get(MediaRow, 10).file_path = SOURCE + "-changed"
    await store.bind(10, SOURCE, DIGEST, "stale-version")
    assert await store.load(10, SOURCE + "-changed", DIGEST) is None
    with Session(engine) as session:
        assert json.loads(session.get(MediaRow, 10).content_identity)["versionId"] == "version-verified"


@pytest.mark.asyncio
@pytest.mark.parametrize("digest", [None, "", "a" * 32, "x" * 64, "A" * 64])
async def test_legacy_empty_or_invalid_digest_never_has_a_fast_path(engine, digest):
    store = MediaIdentityStore(engine)
    assert await store.load(10, SOURCE, digest) is None
    with pytest.raises(ValueError, match="SHA-256"):
        await store.bind(10, SOURCE, digest, "verified-version")


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "", "null", "NULL", " null ", "x" * 1025])
async def test_unversioned_or_malformed_version_cannot_be_bound(engine, version):
    with pytest.raises(ValueError, match="immutable version ID"):
        await MediaIdentityStore(engine).bind(10, SOURCE, DIGEST, version)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"schema": True}, {"schema": 2}, {"source": SOURCE + "-private"},
    {"sha256": "a" * 64}, {"versionId": "null"}, {"extra": "ignored?"},
])
async def test_corrupt_or_mismatched_proof_is_a_miss(engine, changes):
    binding = {"schema": 1, "source": SOURCE, "sha256": DIGEST, "versionId": "version-1"}
    binding.update(changes)
    with Session(engine) as session, session.begin():
        session.get(MediaRow, 10).content_identity = json.dumps(binding)
    assert await MediaIdentityStore(engine).load(10, SOURCE, DIGEST) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["not json", "null", "[]", "{}", "x" * 8193])
async def test_malformed_proof_is_a_miss(engine, payload):
    with Session(engine) as session, session.begin():
        session.get(MediaRow, 10).content_identity = payload
    assert await MediaIdentityStore(engine).load(10, SOURCE, DIGEST) is None


def test_migration_preserves_legacy_md5_and_does_not_fabricate_binding(tmp_path):
    value = create_sqlalchemy_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    config = migration_config()
    try:
        with value.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0003_content_context_artifacts")
            connection.execute(text(
                "INSERT INTO media_files (id,user_id,filename,status,file_path,content_hash,upload_time) "
                "VALUES (10,1,'old.mp4','COMPLETED',:source,:digest,CURRENT_TIMESTAMP)"
            ), {"source": SOURCE, "digest": "b" * 32})
            connection.commit()
        upgrade_schema(value)
        upgrade_schema(value)
        with Session(value) as session:
            row = session.get(MediaRow, 10)
            assert row.content_hash == "b" * 32
            assert row.content_identity is None
            assert row.file_path == SOURCE
        column = next(item for item in inspect(value).get_columns("media_files")
                      if item["name"] == "content_identity")
        assert column["nullable"]
    finally:
        value.dispose()
