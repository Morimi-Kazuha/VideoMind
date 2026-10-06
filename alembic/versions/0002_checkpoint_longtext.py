"""Widen historical MySQL checkpoint payloads without truncating data."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = "0002_checkpoint_longtext"
down_revision = "0001_schema_baseline"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name != "mysql":
        return
    column = next(value for value in sa.inspect(connection).get_columns("agent_checkpoints") if value["name"] == "payload")
    kind = type(column["type"]).__name__.upper()
    if kind in {"TEXT", "MEDIUMTEXT"}:
        op.alter_column("agent_checkpoints", "payload", existing_type=column["type"],
                        type_=LONGTEXT(), existing_nullable=column["nullable"])
    elif kind != "LONGTEXT":
        raise RuntimeError("Unsupported checkpoint payload type; migration stopped")


def downgrade():
    raise RuntimeError("LONGTEXT rollback could truncate data; restore a verified backup")
