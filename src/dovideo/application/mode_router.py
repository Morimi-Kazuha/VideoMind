"""Safe semantic routing from transient AUTO intent to a concrete mode."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Protocol

from dovideo.domain import AnalysisMode


MAX_MODE_ROUTING_RESPONSE_CHARS = 256
_CONCRETE_MODES = {
    AnalysisMode.GENERAL.value: AnalysisMode.GENERAL,
    AnalysisMode.LEARNING.value: AnalysisMode.LEARNING,
    AnalysisMode.REVIEW.value: AnalysisMode.REVIEW,
    AnalysisMode.CREATION.value: AnalysisMode.CREATION,
}
_logger = logging.getLogger("dovideo.mode_router")


class ModeRoutingModelPort(Protocol):
    """One model completion for classifying an unpersisted user intent."""

    async def classify(self, goal: str) -> str | Mapping[str, Any]:
        ...


class ModeRouterObserver(Protocol):
    """Sink for bounded, content-free route lifecycle events."""

    def record_mode_router_event(
        self,
        event: str,
        *,
        mode: str | None = None,
        category: str | None = None,
    ) -> None:
        ...


class ModeRoutingDecodeError(ValueError):
    """A stable, content-free mode-response validation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ModeRouter:
    """Classify a goal and safely resolve every failure to GENERAL.

    The model port is invoked once. Any retry inside that port is bounded by
    its configured transport policy; no analysis or Agent retry is started.
    """

    def __init__(
        self,
        model: ModeRoutingModelPort,
        *,
        observer: ModeRouterObserver | Any | None = None,
    ) -> None:
        if not callable(getattr(model, "classify", None)):
            raise TypeError("mode routing model must provide classify()")
        self._model = model
        self._observer = observer

    async def route(self, goal: str) -> AnalysisMode:
        """Return one concrete mode; provider or decode errors become GENERAL."""

        self._record("requested")
        self._record("provider_call_attempted")
        try:
            raw = await self._model.classify(goal)
        except Exception as error:
            category = _provider_failure_category(error)
            self._record(
                "provider_call_failed",
                category=category,
            )
            self._record("general_fallback_triggered", category=category)
            self._record("selected_concrete_mode", mode=AnalysisMode.GENERAL.value)
            return AnalysisMode.GENERAL

        self._record("provider_call_succeeded")
        try:
            mode = decode_concrete_mode(raw)
        except ModeRoutingDecodeError as error:
            return self._decode_fallback(error.code)
        except Exception:
            return self._decode_fallback("unexpected_decode_error")

        self._record("decode_succeeded")
        self._record("selected_concrete_mode", mode=mode.value)
        return mode

    def _decode_fallback(self, category: str) -> AnalysisMode:
        self._record("decode_failed", category=category)
        self._record("general_fallback_triggered", category=category)
        self._record("selected_concrete_mode", mode=AnalysisMode.GENERAL.value)
        return AnalysisMode.GENERAL

    def _record(
        self,
        event: str,
        *,
        mode: str | None = None,
        category: str | None = None,
    ) -> None:
        observer = self._observer
        try:
            if observer is None:
                _log_mode_router_event(event, mode=mode, category=category)
                return
            observer.record_mode_router_event(
                event,
                mode=mode,
                category=category,
            )
        except Exception:
            # Diagnostics cannot change a routing decision or its fallback.
            return


def decode_concrete_mode(raw: object) -> AnalysisMode:
    """Strictly accept only a JSON object with exactly one concrete mode."""

    if isinstance(raw, str):
        if not raw.strip():
            raise ModeRoutingDecodeError("empty_response")
        if len(raw) > MAX_MODE_ROUTING_RESPONSE_CHARS:
            raise ModeRoutingDecodeError("response_too_large")
        try:
            payload = json.loads(raw, object_pairs_hook=_unique_object)
        except _DuplicateJsonKey:
            raise ModeRoutingDecodeError("duplicate_field") from None
        except (json.JSONDecodeError, TypeError, ValueError):
            raise ModeRoutingDecodeError("invalid_json") from None
    elif isinstance(raw, Mapping):
        payload = raw
    else:
        raise ModeRoutingDecodeError("invalid_object")

    if not isinstance(payload, Mapping):
        raise ModeRoutingDecodeError("invalid_object")
    if len(payload) != 1 or set(payload.keys()) != {"mode"}:
        raise ModeRoutingDecodeError("invalid_field_set")
    value = payload.get("mode")
    if not isinstance(value, str):
        raise ModeRoutingDecodeError("invalid_mode")
    mode = _CONCRETE_MODES.get(value)
    if mode is None:
        raise ModeRoutingDecodeError("invalid_mode")
    return mode


class _DuplicateJsonKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _provider_failure_category(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, OSError):
        return "transport"
    error_name = type(error).__name__
    if error_name in {"ProviderTransientError", "ProviderTransportError"}:
        return "transport"
    if error_name in {
        "ProviderError",
        "ProviderAuthenticationError",
        "ProviderRequestError",
        "ProviderResponseError",
        "ModelProviderError",
        "ModelResponseError",
    }:
        return "provider_error"
    return "unexpected"


def _log_mode_router_event(
    event: str,
    *,
    mode: str | None = None,
    category: str | None = None,
) -> None:
    warning_events = {
        "provider_call_failed",
        "decode_failed",
        "general_fallback_triggered",
    }
    level = logging.WARNING if event in warning_events else logging.INFO
    _logger.log(
        level,
        "mode_router event=%s mode=%s category=%s",
        event,
        mode or "-",
        category or "-",
    )


__all__ = [
    "MAX_MODE_ROUTING_RESPONSE_CHARS",
    "ModeRouter",
    "ModeRouterObserver",
    "ModeRoutingDecodeError",
    "ModeRoutingModelPort",
    "decode_concrete_mode",
]
