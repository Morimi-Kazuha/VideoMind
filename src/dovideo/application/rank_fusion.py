"""Reciprocal rank fusion, independent of dense/sparse score scales."""

from dataclasses import dataclass
from collections.abc import Sequence

RRF_CONSTANT = 60


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    index: int
    score: float


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[RankedCandidate]], *, limit: int, k: int = RRF_CONSTANT,
) -> tuple[RankedCandidate, ...]:
    if k <= 0 or limit < 0:
        raise ValueError("fusion bounds must be positive k and nonnegative limit")
    scores: dict[int, float] = {}
    for ranking in rankings:
        seen = set()
        for candidate in ranking:
            if candidate.index in seen:
                continue
            seen.add(candidate.index)
            scores[candidate.index] = scores.get(candidate.index, 0.0) + 1 / (k + len(seen))
    return tuple(sorted((RankedCandidate(i, score) for i, score in scores.items()),
                        key=lambda item: (-item.score, item.index))[:limit])
