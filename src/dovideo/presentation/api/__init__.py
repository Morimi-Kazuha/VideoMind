"""FastAPI presentation boundary for the R1 product-parity slice."""

from .app import create_app
from .runtime import LocalR1Services, R1ServiceError

__all__ = ["LocalR1Services", "R1ServiceError", "create_app"]
