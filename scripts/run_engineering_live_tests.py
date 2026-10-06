"""Isolated engineering proof over existing infrastructure, without provider calls."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--include-upload", action="store_true", help="Also run existing upload regressions in an isolated migrated database")
    args = parser.parse_args()
    from validate_deployment import read_env
    from sqlalchemy.engine import make_url
    from dovideo.infrastructure.persistence.sqlalchemy import create_sqlalchemy_engine

    values = read_env(args.env_file)
    allowed = {name: value for name, value in values.items() if name.startswith("DOVIDEO_") and any(
        part in name for part in ("DATABASE", "REDIS", "MINIO", "QDRANT", "PROFILE")
    )}
    os.environ.update(allowed)
    os.environ["DOVIDEO_PROFILE"] = "production"
    password = values.get("MYSQL_ROOT_PASSWORD")
    if not password:
        raise RuntimeError("A local MySQL administrator credential is required for generated test databases")
    admin_url = make_url(values["DOVIDEO_DATABASE_URL"]).set(username="root", password=password, database="mysql")
    os.environ["DOVIDEO_ENGINEERING_ADMIN_URL"] = admin_url.render_as_string(hide_password=False)
    os.environ["DOVIDEO_ENGINEERING_LIVE"] = "1"
    os.environ["DOVIDEO_TASK_LEASE_LIVE"] = "1"
    # Verify admin connectivity before pytest; never echo credential-bearing URLs.
    engine = create_sqlalchemy_engine(admin_url.render_as_string(hide_password=False))
    try:
        with engine.connect() as connection:
            pass
    finally:
        engine.dispose()
    import pytest
    from sqlalchemy import text
    from uuid import uuid4
    from dovideo.infrastructure.persistence.migrations import upgrade_schema
    targets = [str(ROOT / "tests/infrastructure/test_engineering_live.py"),
               str(ROOT / "tests/infrastructure/test_task_lease_redis_live.py")]
    database = None
    admin = create_sqlalchemy_engine(admin_url.render_as_string(hide_password=False))
    try:
        if args.include_upload:
            database = "videomind_engineering_" + uuid4().hex[:16]
            assert database.startswith("videomind_engineering_") and len(database) == 38
            with admin.begin() as connection:
                connection.execute(text(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
            test_url = admin_url.set(database=database).render_as_string(hide_password=False)
            test_engine = create_sqlalchemy_engine(test_url)
            try:
                upgrade_schema(test_engine)
                # Avoid ID-based Redis key collisions with any existing local
                # application users/media while keeping the DB disposable.
                start_id = 1_000_000_000 + int(uuid4().hex[:7], 16)
                with test_engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE users AUTO_INCREMENT = {start_id}"))
                    connection.execute(text(f"ALTER TABLE media_files AUTO_INCREMENT = {start_id}"))
            finally:
                test_engine.dispose()
            os.environ["DOVIDEO_DATABASE_URL"] = test_url
            os.environ["DOVIDEO_UPLOAD_LIVE"] = "1"
            targets.append(str(ROOT / "tests/infrastructure/test_upload_redis_minio_live.py"))
        return pytest.main([*targets, "-q", "--tb=short"])
    finally:
        if database is not None:
            with admin.begin() as connection:
                connection.execute(text(f"DROP DATABASE `{database}`"))
        admin.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
