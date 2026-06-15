"""Tests for keyword extraction + whole-word matching."""
from __future__ import annotations

from better_memory.services.keywords import count_keyword_hits, extract_keywords


class TestExtractKeywords:
    def test_lowercases_and_splits(self):
        assert extract_keywords("Write the Plan") == {"write", "plan"}

    def test_drops_stopwords_and_short_tokens(self):
        kw = extract_keywords("we are on to the CI plan")
        assert "plan" in kw
        assert "the" not in kw and "are" not in kw
        assert "ci" not in kw  # 2-char tokens dropped (documented tunable)

    def test_dedupes(self):
        assert extract_keywords("plan plan PLAN") == {"plan"}

    def test_empty(self):
        assert extract_keywords("   ") == set()


class TestCountKeywordHits:
    def test_whole_word_only(self):
        kw = {"art", "plan"}
        # 'art' in 'start', 'plan' in 'planner' — neither is a whole word
        assert count_keyword_hits("let us start the planner", kw) == 0
        assert count_keyword_hits("the art of a plan", kw) == 2

    def test_case_insensitive_and_punctuation(self):
        assert count_keyword_hits("Finalise the Plan.", {"plan"}) == 1

    def test_distinct_terms_counted_once_each(self):
        assert count_keyword_hits("plan plan plan", {"plan"}) == 1
