"""Unit tests for :mod:`better_memory.embeddings.tfidf`."""

from __future__ import annotations

import math

import pytest

from better_memory.embeddings.tfidf import TfidfRetriever, tokenize


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


class TestTfidfRetrieverInMemory:
    def test_fit_populates_vocab_and_doc_vectors(self) -> None:
        r = TfidfRetriever(conn=None)  # type: ignore[arg-type]
        r._fit_docs({"d1": "hello world", "d2": "world peace"})
        assert "hello" in r._vocab
        assert "world" in r._vocab
        assert set(r._doc_vectors.keys()) == {"d1", "d2"}

    def test_idf_higher_for_rarer_tokens(self) -> None:
        r = TfidfRetriever(conn=None)  # type: ignore[arg-type]
        r._fit_docs({"d1": "rare", "d2": "common", "d3": "common"})
        # "rare" appears in 1/3 docs; "common" in 2/3 — rare should have higher IDF
        assert r._idf["rare"] > r._idf["common"]

    def test_vectors_are_l2_normalised(self) -> None:
        r = TfidfRetriever(conn=None)  # type: ignore[arg-type]
        r._fit_docs({"d1": "hello world", "d2": "different stuff"})
        for vec in r._doc_vectors.values():
            norm_sq = sum(v * v for v in vec.values())
            assert math.isclose(norm_sq, 1.0, abs_tol=1e-9)

    def test_score_returns_higher_for_similar_query(self) -> None:
        r = TfidfRetriever(conn=None)  # type: ignore[arg-type]
        r._fit_docs(
            {
                "match": "the quick brown fox jumps",
                "nope": "completely unrelated content here",
            }
        )
        scored = dict(r.score("quick brown fox", ["match", "nope"]))
        assert scored["match"] > scored["nope"]

    def test_score_for_oov_query_returns_zero(self) -> None:
        r = TfidfRetriever(conn=None)  # type: ignore[arg-type]
        r._fit_docs({"d1": "hello world"})
        scored = dict(r.score("xenoglossolalia", ["d1"]))
        assert scored["d1"] == 0.0

    def test_score_for_empty_corpus_returns_empty(self) -> None:
        r = TfidfRetriever(conn=None)  # type: ignore[arg-type]
        assert r.score("anything", []) == []
