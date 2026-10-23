"""Durable proof linking a trusted media digest to a MinIO object version.

This binding is not derived from ETags or client metadata. Callers may create
it only after downloading a non-null, pinned version and verifying its real
SHA-256 against the media record. A lookup does not authorize access by itself;
the worker must recheck ownership and the current object's version separately.
"""
from __future__ import annotations

import asyncio
import json
import re

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .persistence.sqlalchemy import MediaRow

_SHA256 = re.compile(r"[0-9a-f]{64}")
_BINDING_FIELDS = frozenset({"schema", "source", "sha256", "versionId"})


def _valid_version_id(value: object) -> bool:
    # MinIO uses the literal "null" for unversioned objects and suspended
    # versioning. Those bytes can change without a new immutable version ID.
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and value.casefold() != "null"
        and len(value) <= 1024
    )


class MediaIdentityStore:
    """Optional optimization proof stored with, and guarded by, its media row."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def load(self, media_id: int, source: str, sha256: str) -> str | None:
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            return None
        return await asyncio.to_thread(self._load, media_id, source, sha256)

    def _load(self, media_id: int, source: str, sha256: str) -> str | None:
        with Session(self.engine) as session:
            payload = session.scalar(select(MediaRow.content_identity).where(
                MediaRow.id == media_id,
                MediaRow.file_path == source,
                MediaRow.content_hash == sha256,
            ))
        if not isinstance(payload, str) or len(payload) > 8192:
            return None
        try:
            binding = json.loads(payload)
        except (TypeError, ValueError):
            return None
        if not isinstance(binding, dict) or binding.keys() != _BINDING_FIELDS:
            return None
        if type(binding["schema"]) is not int or binding["schema"] != 1:
            return None
        if binding["source"] != source or binding["sha256"] != sha256:
            return None
        version_id = binding["versionId"]
        return version_id if _valid_version_id(version_id) else None

    async def bind(
        self, media_id: int, source: str, sha256: str, version_id: str,
    ) -> None:
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise ValueError("Media version binding requires a SHA-256 fingerprint")
        if not isinstance(source, str) or not source or len(source) > 1024:
            raise ValueError("Media version binding requires a valid media source")
        if not _valid_version_id(version_id):
            raise ValueError("Media version binding requires an immutable version ID")
        payload = json.dumps({
            "schema": 1, "source": source, "sha256": sha256, "versionId": version_id,
        }, sort_keys=True, separators=(",", ":"))
        await asyncio.to_thread(self._bind, media_id, source, sha256, payload)

    def _bind(self, media_id: int, source: str, sha256: str, payload: str) -> None:
        with Session(self.engine) as session, session.begin():
            # An ingest/update racing the verified download cannot attach the
            # old version's proof to a changed source or content fingerprint.
            session.execute(update(MediaRow).where(
                MediaRow.id == media_id,
                MediaRow.file_path == source,
                MediaRow.content_hash == sha256,
            ).values(content_identity=payload))
