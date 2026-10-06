"""Read-only startup guard and explicit Alembic test/deployment helpers."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory


def migration_config():
    root = Path(__file__).resolve().parents[4]
    return Config(str(root / "alembic.ini"))


def upgrade_schema(engine):
    config = migration_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()


def require_schema_head(engine):
    heads = set(ScriptDirectory.from_config(migration_config()).get_heads())
    with engine.connect() as connection:
        current = set(MigrationContext.configure(connection).get_current_heads())
    if current != heads:
        raise RuntimeError("Database schema is not at Alembic head; run alembic upgrade head before startup")
