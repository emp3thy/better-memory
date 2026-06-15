"""Keyword extraction + whole-word matching for contextual memory relevance.

Pure, dependency-free. Used by retrieve_relevant to filter the curated memory
set (semantic + reflections) against the current prompt / tool-input.
"""
from __future__ import annotations

import re

# Small, deliberately conservative stopword set. Tunable.
_STOPWORDS = frozenset({
    "the", "and", "for", "are", "was", "were", "you", "your", "our", "with",
    "this", "that", "from", "into", "have", "has", "had", "but", "not", "can",
    "will", "would", "should", "could", "lets", "let", "get", "got", "out",
    "use", "using", "what", "when", "how", "why", "who", "all", "any", "its",
    "his", "her", "they", "them", "then", "than", "now", "via", "per",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def extract_keywords(text: str) -> set[str]:
    """Lowercase, tokenise on non-alphanumerics, drop stopwords + <3-char tokens."""
    if not text:
        return set()
    return {
        tok
        for tok in _TOKEN_RE.findall(text.lower())
        if len(tok) >= 3 and tok not in _STOPWORDS
    }


def count_keyword_hits(text: str, keywords: set[str]) -> int:
    """Number of distinct keywords that appear as a WHOLE WORD in text."""
    if not text or not keywords:
        return 0
    lowered = text.lower()
    hits = 0
    for kw in keywords:
        if re.search(rf"\b{re.escape(kw)}\b", lowered):
            hits += 1
    return hits
