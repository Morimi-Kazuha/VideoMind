"""Production configuration for the X1 restricted tool path.

The X1 policy constants are the single safety source of truth.  Production
configuration can only lower those existing bounds; it cannot widen the
application contract or turn the feature on implicitly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from dovideo.application.tool_contracts import MAX_TOOL_RESULT_PAYLOAD_BYTES
from dovideo.application.tool_policy import (
    DEFAULT_TOOL_CALLS_PER_ROUND,
    DEFAULT_TOTAL_TOOL_CALLS,
)


class X1ConfigurationError(RuntimeError):
    """Raised when the production X1 tool configuration is unsafe."""


@dataclass(frozen=True, slots=True)
class X1ToolCallingSettings:
    """Validated production rollout and bounded X1 runtime limits.

    ``DEFAULT_TOOL_CALLS_PER_ROUND`` and ``DEFAULT_TOTAL_TOOL_CALLS`` are the
    existing X1-A/B policy bounds.  Configuration is deliberately unable to
    widen them, so API and worker composition share one safety source.
    ``tool_result_limit_bytes`` is the frozen X1 64 KiB envelope ceiling and
    is not independently relaxed by production configuration.
    """

    enabled: bool = False
    tool_request_limit_per_round: int = DEFAULT_TOOL_CALLS_PER_ROUND
    tool_request_limit_total: int = DEFAULT_TOTAL_TOOL_CALLS
    tool_result_limit_bytes: int = MAX_TOOL_RESULT_PAYLOAD_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise X1ConfigurationError("X1 tool calling enabled flag must be boolean")
        _validate_bounded_int(
            self.tool_request_limit_per_round,
            "tool_request_limit_per_round",
            DEFAULT_TOOL_CALLS_PER_ROUND,
        )
        _validate_bounded_int(
            self.tool_request_limit_total,
            "tool_request_limit_total",
            DEFAULT_TOTAL_TOOL_CALLS,
        )
        _validate_bounded_int(
            self.tool_result_limit_bytes,
            "tool_result_limit_bytes",
            MAX_TOOL_RESULT_PAYLOAD_BYTES,
        )

    @property
    def per_round_limit(self) -> int:
        """Short spelling used by the AgentLoop composition."""

        return self.tool_request_limit_per_round

    @property
    def total_limit(self) -> int:
        """Short spelling used by the AgentLoop composition."""

        return self.tool_request_limit_total

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "X1ToolCallingSettings":
        values = os.environ if environ is None else environ
        return cls(
            enabled=_boolean(values, "DOVIDEO_AGENT_TOOL_CALLING_ENABLED", False),
            tool_request_limit_per_round=_bounded_environment_int(
                values,
                (
                    "DOVIDEO_AGENT_TOOL_REQUEST_LIMIT_PER_ROUND",
                    "DOVIDEO_AGENT_TOOL_CALLS_PER_ROUND",
                ),
                DEFAULT_TOOL_CALLS_PER_ROUND,
                DEFAULT_TOOL_CALLS_PER_ROUND,
            ),
            tool_request_limit_total=_bounded_environment_int(
                values,
                (
                    "DOVIDEO_AGENT_TOOL_REQUEST_LIMIT_TOTAL",
                    "DOVIDEO_AGENT_TOOL_CALLS_TOTAL",
                ),
                DEFAULT_TOTAL_TOOL_CALLS,
                DEFAULT_TOTAL_TOOL_CALLS,
            ),
            # The 64 KiB ToolResult ceiling is frozen by X1-A and is not
            # independently environment-configured.  This avoids making the
            # provider prompt and the concrete projection disagree.
            tool_result_limit_bytes=MAX_TOOL_RESULT_PAYLOAD_BYTES,
        )


def _value(values: Mapping[str, str], name: str) -> str | None:
    raw = values.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _value(values, name)
    if raw is None:
        return default
    normalized = raw.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise X1ConfigurationError("X1 tool calling enabled flag must be boolean")


def _bounded_environment_int(
    values: Mapping[str, str],
    names: tuple[str, ...],
    default: int,
    maximum: int,
) -> int:
    raw = next((value for name in names if (value := _value(values, name)) is not None), None)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise X1ConfigurationError("X1 integer setting is invalid") from error
    _validate_bounded_int(value, names[0], maximum)
    return value


def _validate_bounded_int(value: object, name: str, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise X1ConfigurationError(f"X1 {name} must be a positive integer")
    if value < 1:
        raise X1ConfigurationError(f"X1 {name} must be positive")
    if value > maximum:
        raise X1ConfigurationError(f"X1 {name} exceeds the existing safety bound")


__all__ = ["X1ConfigurationError", "X1ToolCallingSettings"]
