"""Tests for :func:`better_memory.search.query.sanitize_fts5_query`."""

from __future__ import annotations

from better_memory.search.query import sanitize_fts5_query


def test_plain_query_is_unchanged() -> None:
    assert sanitize_fts5_query("python bug report") == "python bug report"


def test_hyphenated_terms_are_split_into_separate_tokens() -> None:
    # FTS5 would parse ``better-memory`` as ``better NOT memory`` (with
    # ``memory`` looking like a column filter), raising ``no such column``.
    # We normalise to implicit-AND over two bare tokens.
    assert sanitize_fts5_query("better-memory project") == "better memory project"


def test_colon_is_stripped_to_prevent_column_filter_parse() -> None:
    assert sanitize_fts5_query("foo:bar") == "foo bar"


def test_quotes_and_parentheses_are_stripped() -> None:
    assert sanitize_fts5_query('"quoted" (group)') == "quoted group"


def test_fts5_reserved_keywords_are_dropped() -> None:
    # AND / OR / NOT / NEAR would otherwise behave as operators.
    assert sanitize_fts5_query("alpha AND beta OR gamma NOT delta NEAR epsilon") == (
        "alpha beta gamma delta epsilon"
    )


def test_lowercase_and_or_not_are_preserved_as_tokens() -> None:
    # The reserved-keyword check is case-sensitive to match FTS5's own rule
    # (only uppercase AND/OR/NOT/NEAR are operators).
    assert sanitize_fts5_query("and or not near") == "and or not near"


def test_empty_input_returns_empty_string() -> None:
    assert sanitize_fts5_query("") == ""


def test_whitespace_only_input_returns_empty_string() -> None:
    assert sanitize_fts5_query("   \n\t ") == ""


def test_unicode_word_characters_are_preserved() -> None:
    assert sanitize_fts5_query("café naïve") == "café naïve"
