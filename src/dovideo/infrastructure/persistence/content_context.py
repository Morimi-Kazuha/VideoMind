"""Durable-first content artifacts; Redis hints never authorize a cache hit."""
import asyncio
import hashlib
import logging
import re

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

from dovideo.application import (
    AsrBranchOutcome, OcrBranchOutcome, MediaObservationBundle,
    TranscriptSpan, OcrObservation, VideoContextBuilder,
)
from dovideo.domain import VideoContext
from .sqlalchemy import ContentContextArtifactRow, _db_now

logger = logging.getLogger(__name__)


def validate_artifact(key, context):
    if context.source != key.artifact_source or context.user_goal:
        raise ValueError("Content artifact contains presentation binding")
    if not context.observations or not context.segments:
        raise ValueError("Content artifact requires original provenance observations")
    refs = [ref for segment in context.segments for ref in segment.evidence_frames]
    refs += [item.frame_ref for item in context.observations if item.frame_ref]
    if any(not re.fullmatch(r"frame_[0-9a-f]{64}", ref) for ref in refs):
        raise ValueError("Content artifact contains a private frame location")
    asr = sorted((item for item in context.observations if item.source_item.source_type == "ASR"),
                 key=lambda item: item.source_item.ordinal)
    ocr = sorted((item for item in context.observations if item.source_item.source_type == "OCR"),
                 key=lambda item: item.source_item.ordinal)
    bundle = MediaObservationBundle(
        AsrBranchOutcome(tuple(TranscriptSpan(item.source_item.timestamp_ms, item.source_item.end_ms, item.text)
                               for item in asr), attempted=len(asr)),
        OcrBranchOutcome(tuple(OcrObservation(item.source_item.timestamp_ms, item.text, item.frame_ref)
                               for item in ocr), attempted=len(ocr)),
    )
    rebuilt = VideoContextBuilder().build(key.artifact_source, "", bundle, media_content_identity=key.media_identity)
    if rebuilt != context:
        raise ValueError("Content artifact provenance or pipeline identity mismatch")


class SqlAlchemyContentArtifacts:
    def __init__(self, engine, redis_client=None):
        self.engine, self.redis_client = engine, redis_client

    @staticmethod
    def hint_key(key):
        return f"content-context:artifact:{key.digest}"

    async def _hint(self, key, payload):
        if self.redis_client is None:
            return
        try:
            if payload is None:
                await asyncio.to_thread(self.redis_client.delete, self.hint_key(key))
            else:
                await asyncio.to_thread(self.redis_client.set, self.hint_key(key), payload, ex=7 * 86400)
        except Exception:
            logger.warning("Content artifact Redis hint unavailable; durable lookup retained")

    async def load(self, key):
        cached = None
        if self.redis_client is not None:
            try:
                cached = await asyncio.to_thread(self.redis_client.get, self.hint_key(key))
                if isinstance(cached, bytes):
                    cached = cached.decode("utf-8")
            except Exception:
                logger.warning("Content cache unavailable; reading durable artifact")
        context, payload = await asyncio.to_thread(self._load, key, cached)
        # A hot payload avoids transferring LONGTEXT from MySQL only after
        # confirming the durable header/digest. Orphan cache entries are misses.
        await self._hint(key, payload)
        return context

    def _load(self, key, cached=None):
        with Session(self.engine) as session:
            row = session.get(ContentContextArtifactRow, key.digest, options=(defer(ContentContextArtifactRow.payload),))
            if row is None:
                return None, None
            try:
                if row.fingerprint != key.fingerprint or row.pipeline_contract != key.pipeline_contract:
                    raise ValueError("Artifact key mismatch")
                cached_valid = isinstance(cached, str) and hashlib.sha256(cached.encode("utf-8")).hexdigest() == row.payload_digest
                payload = cached if cached_valid else row.payload
                digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                if digest != row.payload_digest:
                    raise ValueError("Artifact payload digest mismatch")
                context = VideoContext.model_validate_json(payload)
                validate_artifact(key, context)
            except (ValueError, TypeError):
                # Conditional cleanup cannot delete a concurrently replaced row.
                session.execute(delete(ContentContextArtifactRow).where(
                    ContentContextArtifactRow.cache_key == key.digest,
                    ContentContextArtifactRow.payload_digest == row.payload_digest,
                ))
                session.commit()
                logger.warning("Invalid content artifact removed; rebuilding required")
                return None, None
            return context, payload

    async def publish(self, key, context):
        validate_artifact(key, context)
        payload = context.model_dump_json(by_alias=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        await asyncio.to_thread(self._publish, key, payload, digest)
        await self._hint(key, payload)

    def _publish(self, key, payload, digest):
        try:
            with Session(self.engine) as session, session.begin():
                session.add(ContentContextArtifactRow(
                    cache_key=key.digest, fingerprint=key.fingerprint,
                    pipeline_contract=key.pipeline_contract, payload=payload,
                    payload_digest=digest, created_at=_db_now(),
                ))
        except IntegrityError:
            # Immutable first publication; never overwrite another valid owner.
            if self._load(key)[0] is None:
                raise
