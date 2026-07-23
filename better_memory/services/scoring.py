"""Ranking prior for reflections and semantic memories.

One function, one job: the Wilson score lower bound on the proportion of
rated exposures where the memory was positive (useful or overlooked).
Replaces the raw-count ORDER BY stack (useful_count + overlooked weight +
ignored-demotion CASE) — see the 2026-07-23 retrieval-quality spec §1.

Computed in Python, not SQL: SQLite's sqrt() requires the math extension,
the candidate sets are tiny (~150 rows), and a pure function is testable
against closed-form values.
"""

from __future__ import annotations

import math

#: 95% confidence. Pinned — changing z reorders every list; treat as part
#: of the scoring contract, not a tunable.
WILSON_Z = 1.96


def wilson_lower_bound(positive: int, n: int, z: float = WILSON_Z) -> float:
    """Lower bound of the Wilson score interval for positive/n.

    ``n == 0`` (never rated) returns 0.0 — untested memories score at the
    bottom and are surfaced by the exploration slot instead (spec §2).
    """
    if n <= 0:
        return 0.0
    p = positive / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return max(0.0, (centre - margin) / denom)
