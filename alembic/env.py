"""One deployment-owned migration path; application startup only checks head."""
import os

from alembic import context
from sqlalchemy import text

from dovideo.infrastructure.persistence.sqlalchemy import Base, create_sqlalchemy_engine

config = context.config


def run(connection):
    mysql = connection.dialect.name == "mysql"
    if mysql:
        # Include explicitly supplied connections: CLI and test/script helpers
        # share the same bounded deployment coordination.
        connection.execute(text("SET SESSION lock_wait_timeout = 30"))
        if connection.execute(text("SELECT GET_LOCK('videomind_schema_migration', 30)")).scalar() != 1:
            raise RuntimeError("Migration lock acquisition timed out")
        connection.commit()
    try:
        context.configure(connection=connection, target_metadata=Base.metadata,
                          compare_type=True, render_as_batch=connection.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if mysql:
            # MySQL DDL is not transactional. Preserve committed DDL, but do
            # not accidentally commit a failed revision's version-table write.
            connection.rollback()
            connection.execute(text("SELECT RELEASE_LOCK('videomind_schema_migration')"))
            connection.commit()


if context.is_offline_mode():
    raise RuntimeError("Legacy adoption requires online schema inspection; use upgrade against a database")
elif config.attributes.get("connection") is not None:
    run(config.attributes["connection"])
else:
    url = os.environ.get("DOVIDEO_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("DOVIDEO_DATABASE_URL is required for migrations")
    engine = create_sqlalchemy_engine(url, pool_size=1, max_overflow=0)
    try:
        with engine.connect() as connection:
            run(connection)
    finally:
        engine.dispose()
