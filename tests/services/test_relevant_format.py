"""Tests for format_relevant."""
from __future__ import annotations

from dataclasses import replace

from better_memory.services.relevant import RelevantMemory, format_relevant

_BASE = RelevantMemory(
    kind="reflection", id="a" * 32, text="Use junit-xml on windows",
    polarity="do", confidence=0.9, useful_count=15, age_days=34,
    hits=3, score=5.0,
)


def _mem(**kw):
    return replace(_BASE, **kw)


def test_empty_items_renders_empty():
    assert format_relevant([]) == ""


def test_block_structure_and_full_id():
    out = format_relevant([_mem()])
    assert out.startswith('<project-memory source="better-memory">')
    assert out.rstrip().endswith("</project-memory>")
    assert "a" * 32 in out                       # FULL id present
    assert "conf 0.9" in out
    assert "used 15x" in out
    assert "34d old" in out
    assert "memory_credit" in out                 # rating affordance line
    assert "'cited'|'shaped'|'misled'" in out


def test_dont_polarity_rendered_as_corrective():
    out = format_relevant([_mem(polarity="dont", text="inline INSERT SQL in tests drifts")])
    assert "Known pitfall -- do this instead:" in out


def test_semantic_item_without_confidence():
    out = format_relevant([_mem(kind="semantic", polarity=None, confidence=None,
                                useful_count=0, text="repo uses uv run pytest")])
    assert "conf" not in out.split("\n")[2]       # no conf tag on the semantic line
    assert "semantic" in out


def test_missing_age_omitted():
    out = format_relevant([_mem(age_days=None)])
    assert "d old" not in out


def test_output_is_ascii():
    out = format_relevant([_mem()])
    out.encode("ascii")  # raises if any non-ASCII slipped in
