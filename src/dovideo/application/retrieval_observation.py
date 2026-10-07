"""Caller-scoped, transient observation of the first actual retrieval ranking.

No global result cache, telemetry text, public API field or gold annotation.
ContextVar isolation also keeps concurrent evaluations from sharing candidates.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable

from dovideo.domain import VideoEvidenceHit

_observer: ContextVar[Callable | None] = ContextVar("retrieval_observer", default=None)


def observe_retrieval(hit_factory: Callable[[], tuple[VideoEvidenceHit, ...]]) -> None:
    callback = _observer.get()
    if callback is not None:
        callback(hit_factory())


@contextmanager
def capture_retrieval():
    """Keep the initial user-query ranking; later Critic/tool queries are separate."""
    batches: list[tuple[VideoEvidenceHit, ...]] = []
    def record(hits):
        if not batches:
            batches.append(hits)
    token = _observer.set(record)
    try:
        yield batches
    finally:
        _observer.reset(token)
