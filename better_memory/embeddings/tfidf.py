"""TF-IDF retriever for the no-Ollama embeddings backend.

Pure stdlib. State is in-memory only; rebuilt on MCP startup via
:meth:`TfidfRetriever.fit_from_db` and on every write via
:meth:`TfidfRetriever.add_doc`.

Tokenization combines word tokens (lowercased ASCII alphanumeric + underscore,
length >= 2) with character 4-grams. The 4-grams are prefixed with ``"#"``
to keep them in a distinct namespace from word tokens — ``"#"`` is not a
valid word-token char so collisions are impossible.

Vectors are sparse ``dict[token, float]`` with TF*IDF weights, L2-normalised
so cosine similarity reduces to dot product.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable

_WORD_RE = re.compile(r"[a-z0-9_]+")
_NGRAM_N = 4


def tokenize(text: str) -> list[str]:
    """Return word tokens plus character 4-gram tokens for ``text``.

    Word tokens are lowercased, ASCII alphanumeric + underscore, length >= 2.
    Character n-grams are 4-grams over the lowercased text (spaces and
    punctuation included), prefixed with ``"#"`` to namespace them apart
    from word tokens.
    """
    lower = text.lower()
    words = [w for w in _WORD_RE.findall(lower) if len(w) >= 2]
    ngrams = [f"#{lower[i : i + _NGRAM_N]}" for i in range(len(lower) - _NGRAM_N + 1)]
    return words + ngrams


class TfidfRetriever:
    """In-memory TF-IDF retriever.

    State is rebuilt on every ``add_doc`` / ``remove_doc`` and on
    ``fit_from_db``. At ~500 documents, fit cost is ~50 ms.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._vocab: set[str] = set()
        self._idf: dict[str, float] = {}
        self._doc_vectors: dict[str, dict[str, float]] = {}

    # ------------------------------------------------------------ public
    def vectorize(self, text: str) -> dict[str, float]:
        """Return a sparse, L2-normalised TF*IDF vector for ``text``.

        Tokens not in the fitted vocabulary are dropped silently.
        """
        tokens = tokenize(text)
        if not tokens:
            return {}
        tf = Counter(t for t in tokens if t in self._vocab)
        if not tf:
            return {}
        weighted = {t: count * self._idf[t] for t, count in tf.items()}
        return _l2_normalise(weighted)

    def score(
        self, query: str, candidate_ids: list[str]
    ) -> list[tuple[str, float]]:
        """Score ``candidate_ids`` against ``query`` by cosine similarity.

        Returns ``[(id, score), ...]`` sorted by score descending. Unknown
        ids are skipped. Empty query or empty candidates returns ``[]``.
        """
        if not candidate_ids:
            return []
        qv = self.vectorize(query)
        scored: list[tuple[str, float]] = []
        for doc_id in candidate_ids:
            dv = self._doc_vectors.get(doc_id)
            if dv is None:
                continue
            scored.append((doc_id, _cosine_normalised(qv, dv)))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored

    # ----------------------------------------------------------- internals
    def _fit_docs(self, docs: dict[str, str]) -> None:
        """Rebuild vocab, IDF, and doc_vectors from ``docs``."""
        tokenised = {doc_id: tokenize(text) for doc_id, text in docs.items()}
        n_docs = len(tokenised)
        df: Counter[str] = Counter()
        for tokens in tokenised.values():
            df.update(set(tokens))
        # Smoothed IDF: log((N + 1) / (df + 1)) + 1
        self._idf = {
            t: math.log((n_docs + 1) / (count + 1)) + 1.0
            for t, count in df.items()
        }
        self._vocab = set(self._idf.keys())
        self._doc_vectors = {}
        for doc_id, tokens in tokenised.items():
            if not tokens:
                self._doc_vectors[doc_id] = {}
                continue
            tf = Counter(tokens)
            weighted = {t: count * self._idf[t] for t, count in tf.items() if t in self._idf}
            self._doc_vectors[doc_id] = _l2_normalise(weighted)


def _l2_normalise(vec: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm == 0.0:
        return {}
    return {t: v / norm for t, v in vec.items()}


def _cosine_normalised(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine sim assuming both inputs are already L2-normalised => dot product."""
    if not a or not b:
        return 0.0
    smaller, larger = (a, b) if len(a) <= len(b) else (b, a)
    return sum(v * larger.get(t, 0.0) for t, v in smaller.items())
