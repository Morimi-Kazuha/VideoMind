"""Explicit, pickle-free JSON checkpoint serialization."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, TypeAdapter

from .errors import (
    CheckpointDeserializationError,
    CheckpointSerializationError,
    CheckpointVersionMismatchError,
)


@dataclass(frozen=True, slots=True)
class CheckpointVersion:
    """Version tuple that prevents old payloads being silently reused."""

    schema_version: int | str = 1
    prompt_version: str = "v1"
    embedding_version: str = "v1"

    def as_json(self) -> dict[str, int | str]:
        return {
            "schemaVersion": self.schema_version,
            "promptVersion": self.prompt_version,
            "embeddingVersion": self.embedding_version,
        }


def _jsonable(value: Any) -> Any:
    """Convert common domain/container values to JSON-compatible objects."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Let json.dumps report the unsupported type, but make the error from the
    # public codec consistently typed.
    return value


class JsonCheckpointCodec:
    """JSON codec with a schema/prompt/embedding version envelope.

    The envelope is intentionally direct and human-readable::

        {"schemaVersion": 1, "promptVersion": "v1",
         "embeddingVersion": "v1", "payload": {...}}

    Missing or mismatched version fields are a cache miss for the repository
    and a typed durable read failure.  No pickle or implementation-specific
    Python object representation is used.
    """

    def __init__(
        self,
        schema_version: int | str | CheckpointVersion = 1,
        prompt_version: str = "v1",
        embedding_version: str = "v1",
        *,
        version: CheckpointVersion | int | str | None = None,
        namespace_version: int | str | None = None,
    ) -> None:
        if isinstance(schema_version, CheckpointVersion):
            resolved = schema_version
        elif isinstance(version, CheckpointVersion):
            resolved = version
        else:
            resolved_schema = (
                namespace_version
                if namespace_version is not None
                else version
                if version is not None
                else schema_version
            )
            resolved = CheckpointVersion(
                schema_version=resolved_schema,
                prompt_version=prompt_version,
                embedding_version=embedding_version,
            )
        self.version = resolved

    @property
    def schema_version(self) -> int | str:
        return self.version.schema_version

    @property
    def prompt_version(self) -> str:
        return self.version.prompt_version

    @property
    def embedding_version(self) -> str:
        return self.version.embedding_version

    def encode(self, value: Any) -> str:
        document = self.version.as_json()
        document["payload"] = _jsonable(value)
        try:
            return json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except Exception as exc:  # pragma: no cover - exact json error varies
            raise CheckpointSerializationError(
                "保存 Agent Checkpoint JSON 失败"
            ) from exc

    def decode(self, payload: str | bytes | bytearray, target_type: Any = None) -> Any:
        try:
            document = json.loads(payload)
        except Exception as exc:
            raise CheckpointDeserializationError(
                "读取 Agent Checkpoint JSON 失败"
            ) from exc
        if not isinstance(document, dict):
            raise CheckpointDeserializationError(
                "Agent Checkpoint payload must be a JSON object"
            )
        expected = self.version.as_json()
        if any(document.get(key) != value for key, value in expected.items()):
            raise CheckpointVersionMismatchError(
                "Agent Checkpoint version mismatch"
            )
        if "payload" not in document:
            raise CheckpointDeserializationError(
                "Agent Checkpoint payload field is missing"
            )
        value = document["payload"]
        if target_type is None or target_type is Any or target_type is object:
            return value
        try:
            model_validate = getattr(target_type, "model_validate", None)
            if callable(model_validate):
                return model_validate(value)
            validate_python = getattr(target_type, "validate_python", None)
            if callable(validate_python):
                return validate_python(value)
            return TypeAdapter(target_type).validate_python(value)
        except Exception as exc:
            raise CheckpointDeserializationError(
                "Agent Checkpoint payload model validation failed"
            ) from exc

    # Familiar codec names make adapter substitution straightforward.
    dumps = encode
    loads = decode
    serialize = encode
    deserialize = decode
    encode_value = encode
    decode_value = decode


class VersionedJsonCheckpointCodec(JsonCheckpointCodec):
    """Descriptive alias for callers that want the version policy explicit."""


class CheckpointSerializer(JsonCheckpointCodec):
    """Compatibility alias used by persistence composition roots."""


__all__ = [
    "CheckpointSerializer",
    "CheckpointVersion",
    "JsonCheckpointCodec",
    "VersionedJsonCheckpointCodec",
]
