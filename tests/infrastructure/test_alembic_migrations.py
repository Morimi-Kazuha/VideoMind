from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session
import pytest

from dovideo.infrastructure.persistence.migrations import require_schema_head, upgrade_schema
from dovideo.infrastructure.persistence.sqlalchemy import (
    Base, CheckpointRow, MediaRow, UserRow, SqlAlchemyCheckpointStore, create_sqlalchemy_engine,
)


@pytest.fixture
def engine(tmp_path):
    value = create_sqlalchemy_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    yield value
    value.dispose()


def assert_no_drift(engine):
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata) == []


def test_empty_upgrade_repeat_and_model_crud(engine):
    with pytest.raises(RuntimeError, match="upgrade head"):
        require_schema_head(engine)
    upgrade_schema(engine)
    upgrade_schema(engine)
    require_schema_head(engine)
    assert_no_drift(engine)
    with Session(engine) as session, session.begin():
        session.add(UserRow(id=1, username="one", password="hash", nickname="One"))
        session.add(MediaRow(id=10, user_id=1, filename="a.mp4", status="UPLOADED", file_path="minio://media/a"))
    store = SqlAlchemyCheckpointStore(engine)
    store.upsert(10, "media:context", "VIDEO_CONTEXT", '{"ok":true}')
    assert store.read(10, "media:context").payload == '{"ok":true}'
    with Session(engine) as session, session.begin():
        row = session.get(MediaRow, 10)
        row.filename = "renamed.mp4"
    with Session(engine) as session, session.begin():
        assert session.get(MediaRow, 10).filename == "renamed.mp4"
        session.delete(session.get(MediaRow, 10))


def test_legacy_missing_tables_columns_indexes_preserves_data(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id BIGINT NOT NULL PRIMARY KEY, username VARCHAR(32) NOT NULL, password VARCHAR(255) NOT NULL, nickname VARCHAR(50) NOT NULL)"))
        connection.execute(text("INSERT INTO users VALUES (1, 'one', 'hash', 'One')"))
        connection.execute(text("CREATE TABLE media_files (id BIGINT NOT NULL PRIMARY KEY, user_id BIGINT NOT NULL, filename VARCHAR(255) NOT NULL, status VARCHAR(32) NOT NULL, file_path VARCHAR(1024) NOT NULL, content_hash VARCHAR(64), upload_time DATETIME NOT NULL)"))
        connection.execute(text("INSERT INTO media_files VALUES (10, 1, 'a.mp4', 'UPLOADED', 'minio://media/a', 'md5', CURRENT_TIMESTAMP)"))
        connection.execute(text("CREATE TABLE agent_checkpoints (media_id BIGINT NOT NULL, checkpoint_key VARCHAR(160) NOT NULL, payload TEXT, PRIMARY KEY(media_id, checkpoint_key))"))
        connection.execute(text("INSERT INTO agent_checkpoints VALUES (10, 'media:context', :payload)"), {"payload": '{"legacy":true}'})
    upgrade_schema(engine)
    upgrade_schema(engine)
    assert_no_drift(engine)
    with Session(engine) as session:
        assert session.get(UserRow, 1).role == "USER"
        assert session.get(MediaRow, 10).file_path == "minio://media/a"
        assert session.get(MediaRow, 10).content_type is None
        assert session.get(CheckpointRow, (10, "media:context")).payload == '{"legacy":true}'
        assert session.get(CheckpointRow, (10, "media:context")).stage == ""
    assert "agent_execution_records" in inspect(engine).get_table_names()
    assert "failed_analysis_task_replays" in inspect(engine).get_table_names()


def test_unversioned_current_schema_adoption(engine):
    # Reproduce installations created by the pre-Alembic production bootstrap.
    for table in Base.metadata.sorted_tables:
        if table.name != "content_context_artifacts":
            table.create(engine)
    upgrade_schema(engine)
    assert_no_drift(engine)


def test_unsafe_legacy_identity_is_not_fabricated(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id BIGINT PRIMARY KEY, password VARCHAR(255) NOT NULL, nickname VARCHAR(50) NOT NULL)"))
        connection.execute(text("INSERT INTO users VALUES (1, 'hash', 'One')"))
    with pytest.raises(RuntimeError, match="missing required users.username"):
        upgrade_schema(engine)


def test_head_check_never_performs_ddl(engine):
    upgrade_schema(engine)
    before = inspect(engine).get_table_names()
    require_schema_head(engine)
    assert inspect(engine).get_table_names() == before
