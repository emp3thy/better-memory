"""Wilson lower bound: the ranking prior for reflections + semantic memories.

Chosen over raw useful_count because raw counts are rich-get-richer: 67
useful over 192 rated sessions (35% hit rate) permanently outranked 3/4
(75%). The lower bound rewards hit rate while discounting small samples.
"""
from __future__ import annotations

import pytest

from better_memory.services.scoring import wilson_lower_bound


class TestWilsonLowerBound:
    def test_no_data_scores_zero(self):
        assert wilson_lower_bound(0, 0) == 0.0

    def test_proven_dead_weight_scores_near_zero(self):
        assert wilson_lower_bound(0, 58) == pytest.approx(0.0, abs=1e-9)

    def test_high_hit_rate_newcomer_beats_popular_workhorse(self):
        # The design's worked example: 3/4 (75%) must outrank 67/192 (35%).
        assert wilson_lower_bound(3, 4) > wilson_lower_bound(67, 192)

    def test_worked_example_values(self):
        assert wilson_lower_bound(67, 192) == pytest.approx(0.285, abs=0.005)
        assert wilson_lower_bound(3, 4) == pytest.approx(0.301, abs=0.005)

    def test_monotonic_in_positives_at_fixed_n(self):
        scores = [wilson_lower_bound(k, 10) for k in range(11)]
        assert scores == sorted(scores)
        assert scores[0] < scores[10]

    def test_more_evidence_at_same_rate_scores_higher(self):
        assert wilson_lower_bound(30, 40) > wilson_lower_bound(3, 4)

    def test_never_negative_and_never_above_one(self):
        for positive, n in [(0, 1), (1, 1), (1, 1000), (999, 1000)]:
            assert 0.0 <= wilson_lower_bound(positive, n) <= 1.0
