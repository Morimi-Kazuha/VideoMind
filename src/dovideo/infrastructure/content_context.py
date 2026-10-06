"""Versioned R4 pipeline identity and independent Redis build coordination."""
import hashlib
import os

from dovideo.application.content_context import ContentContextKey
from dovideo.domain.provenance import (
    EXTRACTION_CONTRACT_VERSION, NORMALIZATION_VERSION, PROVENANCE_VERSION, sha256_canonical,
)
from .redis import RedisTaskLock
from .media.audio import AUDIO_SEGMENT_SECONDS
from .media.keyframes import DEFAULT_SCENE_THRESHOLD, KEYFRAME_FALLBACK_INTERVAL_SECONDS
from .media.hashing import DEFAULT_HAMMING_THRESHOLD


class RedisContentBuildLock(RedisTaskLock):
    """Reuse SET NX PX/token-checked Lua, with a separate content namespace."""
    def __init__(self, client, *, ttl_ms=120_000):
        super().__init__(client, ttl_ms=ttl_ms, prefix="lock:content-context")

    def redis_key(self, key: ContentContextKey):
        return f"{self.prefix}:{key.digest}"


def pipeline_contract(settings, environ=None):
    values = os.environ if environ is None else environ
    version = values.get("DOVIDEO_CONTEXT_PIPELINE_VERSION", "r4-preprocessing-v1").strip()
    if not version or len(version) > 128:
        raise ValueError("Context pipeline version must be nonblank and bounded")
    # Bump the operator version when tool binaries, weights, extraction,
    # normalization or window policies change. Runtime locations are private
    # configuration and do not enter this provider-neutral identity.
    profile = {
        "pipeline": version, "extraction": EXTRACTION_CONTRACT_VERSION,
        "normalization": NORMALIZATION_VERSION, "provenance": PROVENANCE_VERSION,
        "whisperModel": settings.whisper_model, "whisperDevice": settings.whisper_device,
        "whisperLanguage": settings.whisper_language,
        "asrProfile": "segmented-60s-v3", "audioSegmentSeconds": AUDIO_SEGMENT_SECONDS,
        "ocrProfile": "tesseract-dhash-v1", "ocrHammingThreshold": DEFAULT_HAMMING_THRESHOLD,
        "sceneThreshold": DEFAULT_SCENE_THRESHOLD, "frameFallbackSeconds": KEYFRAME_FALLBACK_INTERVAL_SECONDS,
        "contextProfile": "temporal-60000ms-v2",
    }
    return f"{version}:{sha256_canonical(profile)}"


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for piece in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(piece)
    return digest.hexdigest()
