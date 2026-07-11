"""Tests for format_relevant."""
from __future__ import annotations

from better_memory.services.relevant import RelevantMemory, format_relevant


def test_empty_returns_empty_string():
    assert format_relevant([]) == ""


def test_caps_items():
    items = [
        RelevantMemory(kind="semantic", id=str(i), text=f"memory {i}", polarity=None,
                       confidence=None, useful_count=0, age_days=None, hits=1, score=1.0)
        for i in range(10)
    ]
    out = format_relevant(items, max_items=5)
    assert out.count("•") == 5


def test_includes_confidence_for_reflections():
    out = format_relevant([RelevantMemory(kind="reflection", id="r1", text="do the thing",
                                          polarity="do", confidence=0.9, useful_count=0,
                                          age_days=None, hits=2, score=2.0)])
    assert "conf 0.90" in out


def test_semantic_has_no_confidence_tag():
    out = format_relevant([RelevantMemory(kind="semantic", id="s1", text="a fact", polarity=None,
                                          confidence=None, useful_count=0, age_days=None,
                                          hits=1, score=1.0)])
    assert "· conf" not in out  # the confidence tag, not the word "conflicts" in the header
