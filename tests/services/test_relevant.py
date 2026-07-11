"""Tests for retrieve_relevant scoring (hits x activation, min-hits floor)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from better_memory.services.relevant import RelevantMemory, retrieve_relevant

FIXED_NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)


class _FakeBackend:
    """Minimal stand-in for a StorageBackend: only the two methods
    retrieve_relevant calls (retrieve, semantic_list) are implemented."""

    def __init__(self):
        self.reflections: dict = {"do": [], "dont": [], "neutral": []}
        self.semantic: list = []
        self.retrieve_error: Exception | None = None
        self.semantic_error: Exception | None = None

    def retrieve(self, *, project, track_exposure=False):
        if self.retrieve_error is not None:
            raise self.retrieve_error
        return self.reflections

    def semantic_list(self, *, project, track_exposure=False):
        if self.semantic_error is not None:
            raise self.semantic_error
        return self.semantic


@pytest.fixture
def fake_backend():
    return _FakeBackend()


def _reflection(id="r1", title="pytest windows redirect", hints=None,
                useful_count=0, times_misled=0, confidence=0.8,
                updated_at="2026-07-01T00:00:00+00:00", polarity="do"):
    return {
        "id": id, "title": title, "phase": "general", "use_cases": "",
        "hints": hints or [], "confidence": confidence, "tech": None,
        "evidence_count": 1, "useful_count": useful_count,
        "times_misled": times_misled, "updated_at": updated_at,
        "_polarity": polarity,  # test helper only; buckets carry polarity
    }


def test_backend_error_degrades_to_empty(fake_backend):
    fake_backend.retrieve_error = RuntimeError("boom")
    out = retrieve_relevant(fake_backend, query="alpha beta", project="p",
                            now=lambda: FIXED_NOW)
    assert out == []


def test_empty_keyword_query_returns_empty(fake_backend):
    fake_backend.reflections = {"do": [_reflection(title="alpha beta")], "dont": [], "neutral": []}
    out = retrieve_relevant(fake_backend, query="   ", project="p", now=lambda: FIXED_NOW)
    assert out == []


def test_floor_rejects_single_hit_by_default(fake_backend):
    fake_backend.reflections = {"do": [_reflection(title="pytest only-one")],
                                "dont": [], "neutral": []}
    out = retrieve_relevant(fake_backend, query="pytest something unrelated",
                            project="p", now=lambda: FIXED_NOW)
    assert out == []  # 1 distinct hit < min_hits=2


def test_floor_admits_two_hits(fake_backend):
    fake_backend.reflections = {"do": [_reflection(title="pytest windows redirect")],
                                "dont": [], "neutral": []}
    out = retrieve_relevant(fake_backend, query="run pytest on windows",
                            project="p", now=lambda: FIXED_NOW)
    assert [m.id for m in out] == ["r1"]
    assert out[0].hits == 2
    assert out[0].age_days == 10
    assert out[0].kind == "reflection"


def test_title_hits_count_double_in_score(fake_backend):
    title_match = _reflection(id="rt", title="alpha beta", updated_at="2026-07-01T00:00:00+00:00")
    hint_match = _reflection(id="rh", title="zzz yyy", hints=["alpha beta"],
                             updated_at="2026-07-01T00:00:00+00:00")
    fake_backend.reflections = {"do": [hint_match, title_match], "dont": [], "neutral": []}
    out = retrieve_relevant(fake_backend, query="alpha beta", project="p", now=lambda: FIXED_NOW)
    assert out[0].id == "rt"  # same hits, but title-weighted score wins


def test_misled_penalty_halves_score(fake_backend):
    clean = _reflection(id="rc", title="alpha beta", useful_count=0, times_misled=0)
    burned = _reflection(id="rb", title="alpha beta", useful_count=0, times_misled=3)
    fake_backend.reflections = {"do": [burned, clean], "dont": [], "neutral": []}
    out = retrieve_relevant(fake_backend, query="alpha beta", project="p", now=lambda: FIXED_NOW)
    assert out[0].id == "rc"
    assert out[1].score < out[0].score


def test_max_items_cap(fake_backend):
    fake_backend.reflections = {"do": [
        _reflection(id=f"r{i}", title="alpha beta") for i in range(6)
    ], "dont": [], "neutral": []}
    out = retrieve_relevant(fake_backend, query="alpha beta", project="p",
                            max_items=3, now=lambda: FIXED_NOW)
    assert len(out) == 3


def test_missing_metadata_is_neutral(fake_backend):
    r = _reflection(id="rm", title="alpha beta")
    del r["times_misled"]  # older backend shape
    del r["updated_at"]
    fake_backend.reflections = {"do": [r], "dont": [], "neutral": []}
    out = retrieve_relevant(fake_backend, query="alpha beta", project="p", now=lambda: FIXED_NOW)
    assert out[0].age_days is None
    assert out[0].score > 0


def test_returns_relevantmemory(fake_backend):
    fake_backend.reflections = {"do": [_reflection(title="alpha beta")], "dont": [], "neutral": []}
    out = retrieve_relevant(fake_backend, query="alpha beta", project="p", now=lambda: FIXED_NOW)
    assert out and all(isinstance(m, RelevantMemory) for m in out)
    assert all(m.kind in ("reflection", "semantic") for m in out)
