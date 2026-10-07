"""Request-local Okapi BM25 for one video's bounded chunk corpus."""

from collections import Counter
from collections.abc import Sequence
import math

from .rank_fusion import RankedCandidate
from .retrieval_documents import tokenize

BM25_K1 = 1.2
BM25_B = 0.75


class BM25Retriever:
    def rank(self, query: str, documents: Sequence[str], *, limit: int) -> tuple[RankedCandidate, ...]:
        if not query.strip() or not documents or limit <= 0:
            return ()
        terms = tuple(dict.fromkeys(tokenize(query)))
        frequencies = [Counter(tokenize(doc)) for doc in documents]
        lengths = [sum(tf.values()) for tf in frequencies]
        average = sum(lengths) / len(lengths)
        if not terms or not average:
            return ()
        document_frequency = Counter(term for tf in frequencies for term in tf)
        result = []
        for index, (tf, length) in enumerate(zip(frequencies, lengths, strict=True)):
            score = 0.0
            normalization = BM25_K1 * (1 - BM25_B + BM25_B * length / average)
            for term in terms:
                frequency = tf[term]
                if frequency:
                    df = document_frequency[term]
                    idf = math.log(1 + (len(documents) - df + 0.5) / (df + 0.5))
                    score += idf * frequency * (BM25_K1 + 1) / (frequency + normalization)
            if score > 0:
                result.append(RankedCandidate(index, score))
        return tuple(sorted(result, key=lambda item: (-item.score, item.index))[:limit])
