"""Helpers for turning user text into safe SQLite FTS5 MATCH queries.

FTS5's MATCH grammar reserves ``-``, ``:``, ``"``, ``^``, ``+``, ``*``,
parentheses, and the uppercase keywords ``AND`` / ``OR`` / ``NOT`` / ``NEAR``
as operators. Raw user text that happens to contain any of these raises
``sqlite3.OperationalError`` (``no such column: <token>``) from deep inside
SQLite — e.g. ``better-memory`` is parsed as ``better`` followed by a
``-memory`` column-exclusion and blows up because ``memory`` is not a
column in the FTS table.

:func:`sanitize_fts5_query` normalises user text to alphanumeric tokens
joined by spaces, which FTS5 treats as implicit AND across bare terms —
the behaviour users actually want from a natural-language query.
"""

from __future__ import annotations

import re

_RESERVED_FTS5_KEYWORDS = frozenset({"AND", "OR", "NOT", "NEAR"})

# ``\w`` under Python's default ``re`` flags is Unicode-aware and matches
# letters (incl. accented), digits, and underscore. Everything else becomes
# a token boundary.
_TOKEN_RE = re.compile(r"\w+")


def sanitize_fts5_query(text: str) -> str:
    """Return ``text`` stripped of FTS5 operator syntax.

    Splits on any non-word character, drops the reserved uppercase
    keywords, and rejoins with spaces.
    """
    tokens = [
        token for token in _TOKEN_RE.findall(text)
        if token not in _RESERVED_FTS5_KEYWORDS
    ]
    return " ".join(tokens)
