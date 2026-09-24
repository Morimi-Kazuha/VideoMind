"""Typed failures for the Phase 8 checkpoint persistence boundary.

The repository deliberately treats the durable store and the hot cache as
different failure domains.  Durable failures are surfaced with one of the
typed errors below; cache failures are handled by ``CheckpointRepository``
and never make a committed durable write look unsuccessful.
"""

from __future__ import annotations


class CheckpointError(RuntimeError):
    """Base class for checkpoint persistence and codec failures."""


class DurableCheckpointError(CheckpointError):
    """A failure while reading or mutating the durable checkpoint store."""


class CheckpointReadError(DurableCheckpointError):
    """The durable checkpoint could not be read."""


class CheckpointWriteError(DurableCheckpointError):
    """The durable checkpoint could not be committed."""


class CheckpointDeleteError(DurableCheckpointError):
    """The durable checkpoint could not be deleted."""


class CheckpointSerializationError(CheckpointReadError):
    """A checkpoint payload is not valid JSON or cannot build its model."""


class CheckpointDeserializationError(CheckpointSerializationError):
    """A persisted payload cannot be deserialized into the requested type."""


class CheckpointVersionMismatchError(CheckpointDeserializationError):
    """A payload belongs to a different checkpoint schema/prompt version."""


class CheckpointCacheError(CheckpointError):
    """Optional marker for adapters that want to classify cache failures.

    The repository does not propagate this error: cache failures are logged
    or swallowed after durable work has completed.
    """


class MediaPersistenceError(RuntimeError):
    """Base error for durable ``media_files`` adapter failures."""


class MediaReadError(MediaPersistenceError):
    """A media record could not be read or mapped."""


class MediaWriteError(MediaPersistenceError):
    """A media record could not be committed."""


class MediaDeleteError(MediaPersistenceError):
    """A media record could not be deleted."""


# These spellings are useful to callers that use the shorter names from the
# Java adapter design or from an infrastructure error taxonomy.
CheckpointDeserializeError = CheckpointDeserializationError
CheckpointCodecError = CheckpointSerializationError
MediaRecordPersistenceError = MediaPersistenceError


__all__ = [
    "CheckpointCacheError",
    "CheckpointCodecError",
    "CheckpointDeserializeError",
    "CheckpointDeserializationError",
    "CheckpointDeleteError",
    "CheckpointError",
    "CheckpointReadError",
    "CheckpointSerializationError",
    "CheckpointVersionMismatchError",
    "CheckpointWriteError",
    "DurableCheckpointError",
    "MediaDeleteError",
    "MediaPersistenceError",
    "MediaReadError",
    "MediaRecordPersistenceError",
    "MediaWriteError",
]
