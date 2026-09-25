from __future__ import annotations

from sqlalchemy.dialects.mysql import dialect as mysql_dialect
from sqlalchemy.schema import CreateTable

from dovideo.infrastructure.persistence.sqlalchemy import CheckpointRow


def test_mysql_checkpoint_payload_supports_source_rich_chunks() -> None:
    ddl = str(CreateTable(CheckpointRow.__table__).compile(dialect=mysql_dialect()))
    assert "payload LONGTEXT" in ddl
