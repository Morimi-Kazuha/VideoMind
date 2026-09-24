"""Application contracts for the shared P5 AI-interaction limiter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class AiInteractionLimiterUnavailable(RuntimeError):
    """Raised when the authoritative rate-limit backend cannot be queried."""


@dataclass(frozen=True, slots=True)
class AiInteractionLimitDecision:
    """Safe result of one authenticated AI interaction admission check."""

    allowed: bool
    reason: str = "ALLOWED"
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise TypeError("rate-limit decision allowed must be boolean")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("rate-limit decision reason is required")
        if self.retry_after_seconds is not None and (
            isinstance(self.retry_after_seconds, bool)
            or not isinstance(self.retry_after_seconds, int)
            or self.retry_after_seconds <= 0
        ):
            raise ValueError("retry_after_seconds must be a positive integer or None")


class AiInteractionLimiterPort(Protocol):
    """Shared limiter boundary used by all protected API endpoints."""

    async def try_acquire(
        self,
        user_id: int,
        endpoint: str,
    ) -> AiInteractionLimitDecision:
        """Admit one interaction or return a bounded rejection decision."""


__all__ = [
    "AiInteractionLimitDecision",
    "AiInteractionLimiterPort",
    "AiInteractionLimiterUnavailable",
]
