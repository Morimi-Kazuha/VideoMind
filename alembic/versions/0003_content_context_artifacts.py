"""Durable content-derived artifacts independent of source media lifetime."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = "0003_content_context_artifacts"
down_revision = "0002_checkpoint_longtext"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "content_context_artifacts",
        sa.Column("cache_key", sa.String(64), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("pipeline_contract", sa.String(1024), nullable=False),
        sa.Column("payload", sa.Text().with_variant(LONGTEXT(), "mysql"), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("content_context_artifacts")
