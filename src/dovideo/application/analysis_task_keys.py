"""Java-compatible analysis identity and Redis key helpers.

The helpers are pure and deliberately do not know about a database or Redis.
They are the shared key policy used by the Phase 8B checkpoint service and
future dispatch/status adapters.
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum

from dovideo.domain import AnalysisMode


_MD5_PATTERN = re.compile(r"^[a-fA-F0-9]{32}$")


def _mode_name(mode: AnalysisMode | str | None) -> str:
    if mode is None:
        return AnalysisMode.GENERAL.name
    if isinstance(mode, AnalysisMode):
        return mode.name
    # TaskKey already normalizes modes, but key helpers are also used directly
    # by migration callers.  Unknown/null-like text follows Java's nullable
    # GENERAL compatibility path.
    return AnalysisMode.from_nullable(str(mode)).name


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def goal_digest(goal: str, mode: AnalysisMode | str | None = None) -> str:
    """Return the exact Java UTF-8 SHA-256 goal digest.

    GENERAL and ``None`` intentionally use the historical digest of the
    trimmed goal.  Other modes include ``mode.name + U+241F`` before the
    trimmed goal to isolate same-text tasks without changing old GENERAL
    keys.
    """

    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("analysis goal is required")
    trimmed = goal.strip()
    name = _mode_name(mode)
    if name == AnalysisMode.GENERAL.name:
        return _sha256(trimmed)
    return _sha256(f"{name}\u241f{trimmed}")


def normalize_content_hash(media_id: int, content_hash: str | None) -> str:
    """Keep a valid MD5 lower-cased, otherwise use Java's media fallback."""

    if isinstance(content_hash, str) and _MD5_PATTERN.fullmatch(content_hash):
        return content_hash.lower()
    return f"media-{media_id}"


def active(content_scope: str, goal_digest_value: str) -> str:
    return f"analysis:active:{content_scope}:{goal_digest_value}"


def lock(content_scope: str, goal_digest_value: str) -> str:
    return f"lock:analysis:{content_scope}:{goal_digest_value}"


def completed(content_scope: str, goal_digest_value: str) -> str:
    return f"analysis:completed:{content_scope}:{goal_digest_value}"


def attempts(content_scope: str, goal_digest_value: str) -> str:
    return f"analysis:attempts:{content_scope}:{goal_digest_value}"


def context_owner(content_scope: str) -> str:
    return f"analysis:context-owner:{content_scope}"


def context_lock(content_scope: str) -> str:
    return f"lock:analysis-context:{content_scope}"


class AnalysisTaskKeys:
    """Java-style static facade over the snake_case helper functions."""

    goal_digest = staticmethod(goal_digest)
    normalize_content_hash = staticmethod(normalize_content_hash)
    active = staticmethod(active)
    lock = staticmethod(lock)
    completed = staticmethod(completed)
    attempts = staticmethod(attempts)
    context_owner = staticmethod(context_owner)
    context_lock = staticmethod(context_lock)

    goalDigest = staticmethod(goal_digest)
    normalizeContentHash = staticmethod(normalize_content_hash)
    contextOwner = staticmethod(context_owner)
    contextLock = staticmethod(context_lock)


# Direct aliases are useful to adapters ported from the Java utility.
goalDigest = goal_digest
normalizeContentHash = normalize_content_hash
contextOwner = context_owner
contextLock = context_lock


__all__ = [
    "AnalysisTaskKeys",
    "active",
    "attempts",
    "completed",
    "context_lock",
    "context_owner",
    "contextLock",
    "contextOwner",
    "goal_digest",
    "goalDigest",
    "lock",
    "normalize_content_hash",
    "normalizeContentHash",
]
