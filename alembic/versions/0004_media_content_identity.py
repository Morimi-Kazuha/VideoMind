"""Bind verified media SHA-256 values to immutable object versions."""
from alembic import op
import sqlalchemy as sa

revision = "0004_media_content_identity"
down_revision = "0003_content_context_artifacts"
branch_labels = None
depends_on = None


def upgrade():
    # No backfill: a version binding requires hashing the actual video bytes.
    existing = next((column for column in sa.inspect(op.get_bind()).get_columns("media_files")
                     if column["name"] == "content_identity"), None)
    if existing is not None:
        # Support explicit adoption of an unversioned current-schema install.
        if not isinstance(existing["type"], sa.Text) or not existing["nullable"]:
            raise RuntimeError("Unsupported media content identity column; migration stopped")
        return
    op.add_column("media_files", sa.Column("content_identity", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("media_files", "content_identity")
