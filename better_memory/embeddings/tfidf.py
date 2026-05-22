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
