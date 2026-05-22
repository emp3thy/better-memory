"""Unit tests for :mod:`better_memory.embeddings.tfidf`."""

from __future__ import annotations

import math

import pytest

from better_memory.embeddings.tfidf import tokenize


class TestTokenize:
    def test_lowercases_and_splits_on_non_alnum(self) -> None:
        result = tokenize("Hello, World! 123")
        words = [t for t in result if not t.startswith("#")]
        assert words == ["hello", "world", "123"]

    def test_keeps_snake_case_whole(self) -> None:
        words = [t for t in tokenize("session_bootstrap") if not t.startswith("#")]
        assert words == ["session_bootstrap"]

    def test_drops_tokens_shorter_than_two_chars(self) -> None:
        words = [t for t in tokenize("a bb ccc") if not t.startswith("#")]
        assert "a" not in words
        assert "bb" in words
        assert "ccc" in words

    def test_emits_char_4grams_prefixed_with_hash(self) -> None:
        result = tokenize("abcde")
        ngrams = [t for t in result if t.startswith("#")]
        assert "#abcd" in ngrams
        assert "#bcde" in ngrams

    def test_empty_string_returns_empty(self) -> None:
        assert tokenize("") == []

    def test_short_text_yields_no_ngrams(self) -> None:
        result = tokenize("ab")
        ngrams = [t for t in result if t.startswith("#")]
        assert ngrams == []
