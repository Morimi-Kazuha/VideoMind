"""Shared Pydantic configuration and normalization helpers for the domain layer.

The Java DTOs in the reference service are records.  Domain objects therefore
use frozen Pydantic models and tuples for collections: a parsed payload cannot
be changed through either the model or the list that was supplied by a caller.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Base model shared by all Phase 1 wire/domain objects.

    ``populate_by_name`` accepts Python snake_case names as well as the stable
    Java/Jackson camelCase aliases.  ``serialize_by_alias`` makes the default
    ``model_dump``/``model_dump_json`` representation suitable for existing
    Java consumers; callers can still request canonical Python names with
    ``by_alias=False``.
    """

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        validate_default=True,
    )


T = TypeVar("T")


def tuple_or_empty(value: Iterable[T] | None) -> tuple[T, ...]:
    """Convert an optional collection to an immutable tuple.

    Pydantic performs item validation after a ``mode='before'`` validator.  A
    small helper keeps the Java ``null -> List.of()`` defaults explicit and
    also guarantees that caller-owned lists are not retained.
    """

    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray, dict)):
        raise ValueError("expected a collection")
    return tuple(value)


def value_or_empty(value: Any) -> Any:
    """Return the Java-compatible empty value for nullable text fields.

    This helper is intentionally tiny; text fields that need trimming perform
    that operation in their own validators so fields such as ``source`` and
    ``VideoEvidenceHit.snippet`` retain the Java record's exact semantics.
    """

    return "" if value is None else value


def reject_alias_conflicts(
    data: Any,
    *groups: tuple[str, ...],
) -> dict[str, Any] | Any:
    """Reject contradictory spellings before Pydantic alias precedence runs.

    Pydantic intentionally gives an alias precedence when both an alias and a
    field name are supplied.  That is useful for normal deserialization but
    dangerous for migration payloads: ``user_goal='a', userGoal='b'`` would
    silently become ``'b'``.  Phase 1 treats all values in one spelling group
    as the same logical field and rejects a conflict.  Equal values (including
    equal list/tuple content) remain valid.  ``None`` is a value for this
    comparison, so ``None`` plus a non-None spelling is also rejected.
    """

    if not isinstance(data, dict):
        return data
    normalized = dict(data)
    for group in groups:
        present = [name for name in group if name in normalized]
        if len(present) < 2:
            continue
        first_name = present[0]
        first_value = normalized[first_name]
        if any(
            not _alias_values_equal(first_value, normalized[name])
            for name in present[1:]
        ):
            joined = "/".join(group)
            raise ValueError(f"conflicting values for aliased fields: {joined}")
    return normalized


def _alias_values_equal(left: Any, right: Any) -> bool:
    """Compare JSON-like alias values without treating list/tuple as a conflict."""

    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _alias_values_equal(item_left, item_right)
            for item_left, item_right in zip(left, right)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _alias_values_equal(left[key], right[key]) for key in left
        )
    return left == right


def normalize_nullable_aliases(
    data: Any,
    defaults: dict[str, Any],
    *groups: tuple[str, ...],
) -> dict[str, Any] | Any:
    """Check alias groups, then normalize explicit ``None`` independently.

    Defaults are written only to keys that the caller actually supplied.  This
    avoids manufacturing an alias key that could win over a valid snake_case
    value during Pydantic's alias resolution.
    """

    normalized = reject_alias_conflicts(data, *groups)
    if not isinstance(normalized, dict):
        return normalized
    normalized = dict(normalized)
    for key, default in defaults.items():
        if key in normalized and normalized[key] is None:
            normalized[key] = default
    return normalized
