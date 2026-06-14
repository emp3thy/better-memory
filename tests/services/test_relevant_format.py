"""Tests for format_relevant."""
from __future__ import annotations

from better_memory.services.relevant import RelevantMemory, format_relevant


def test_empty_returns_empty_string():
    assert format_relevant([]) == ""


def test_caps_items():
    items = [RelevantMemory("semantic", str(i), f"memory {i}", None, 1) for i in range(10)]
    out = format_relevant(items, max_items=5)
    assert out.count("•") == 5


def test_includes_confidence_for_reflections():
    out = format_relevant([RelevantMemory("reflection", "r1", "do the thing", 0.9, 2)])
    assert "conf 0.90" in out


def test_semantic_has_no_confidence_tag():
    out = format_relevant([RelevantMemory("semantic", "s1", "a fact", None, 1)])
    assert "conf" not in out
