"""Bounded no-provider MinIO object-read diagnostic."""

from __future__ import annotations

import asyncio
import runpy
import sys
import traceback
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(REPO / "src"),
    str(REPO / "work" / "r1-venv" / "Lib" / "site-packages"),
]
runpy.run_path(str(REPO / "tools" / "run_r4_live.py"))["load_local_environment"]()

from dovideo.infrastructure import create_r2_infrastructure  # noqa: E402


async def main() -> None:
    infrastructure = create_r2_infrastructure()
    try:
        with infrastructure.engine.connect() as connection:
            from sqlalchemy import text

            row = connection.execute(
                text(
                    "SELECT id FROM media_files "
                    "WHERE file_path LIKE 'minio://%' "
                    "ORDER BY id DESC LIMIT 1"
                )
            ).first()
        if row is None:
            print("MINIO_DIAGNOSTIC=NO_MEDIA")
            return
        media_id = int(row[0])
        record = await infrastructure.media_repository.get(media_id)
        if record is None:
            print("MINIO_DIAGNOSTIC=MEDIA_NOT_FOUND")
            return
        source = str(record.source)
        prefix = "minio://"
        if not source.startswith(prefix):
            print("MINIO_DIAGNOSTIC=BAD_SOURCE_SCHEME")
            return
        rest = source[len(prefix) :]
        bucket, object_name = rest.split("/", 1)
        print("MINIO_DIAGNOSTIC=START")
        response = infrastructure.minio_client.get_object(bucket, object_name)
        total = 0
        try:
            while True:
                piece = response.read(1024 * 1024)
                if not piece:
                    break
                total += len(piece)
        finally:
            response.close()
            response.release_conn()
        print("MINIO_DIAGNOSTIC=PASS")
        print("MEDIA_ID=" + str(media_id))
        print("OBJECT_BYTES=" + str(total))
    except BaseException as exc:
        frames = traceback.extract_tb(exc.__traceback__)
        print("MINIO_DIAGNOSTIC=FAIL")
        print("ERROR_TYPE=" + type(exc).__name__)
        for item in frames[-6:]:
            print(
                "ERROR_FRAME="
                + Path(item.filename).name
                + ":"
                + str(item.lineno)
                + ":"
                + item.name
            )
    finally:
        infrastructure.close()


if __name__ == "__main__":
    asyncio.run(main())
