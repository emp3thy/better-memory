"""Relevance filter over the curated memory set (semantic + reflections).

Fetches the small, already-ranked sets through the StorageBackend abstraction
(works on sqlite AND agentcore) and scores them against a query using
hits x activation: distinct whole-word keyword hits (title hits count double)
multiplied by an activation factor built from useful_count and confidence,
halved when a memory has misled more often than it has helped. Items below
a min-hits floor are dropped; the remainder is ranked by score and capped
to max_items. Pure-Python; no embeddings, no new schema.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from better_memory.services.keywords import count_keyword_hits, extract_keywords


@dataclass
class RelevantMemory:
    kind: str                 # "reflection" | "semantic"
    id: str
    text: str                 # full display text (renderer truncates)
    polarity: str | None      # "do" | "dont" | None for semantic
    confidence: float | None
    useful_count: int
    age_days: int | None
    hits: int
    score: float


def _age_days(iso_ts: str | None, now: datetime) -> int | None:
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0, (now - ts).days)


def _activation(*, useful_count: int, times_misled: int, confidence: float | None) -> float:
    act = (1.0 + 0.2 * math.log1p(max(0, useful_count)))
    if confidence is not None:
        act *= max(0.1, float(confidence))
    if times_misled > useful_count:
        act *= 0.5
    return act


def retrieve_relevant(
    backend: Any,
    *,
    query: str,
    project: str,
    min_hits: int = 2,
    max_items: int = 3,
    include_neutral: bool = False,
    now: Callable[[], datetime] | None = None,
) -> list[RelevantMemory]:
    """Score curated memories against the query; return top max_items whose
    distinct-keyword hits >= min_hits, ordered by score desc. Never raises."""
    keywords = extract_keywords(query)
    if not keywords:
        return []
    _now = (now or (lambda: datetime.now(UTC)))()

    out: list[RelevantMemory] = []

    try:
        buckets = backend.retrieve(project=project, track_exposure=False)
    except Exception:  # noqa: BLE001 - degrade to no reflections
        buckets = {}
    order = ["do", "dont"] + (["neutral"] if include_neutral else [])
    for bucket in order:
        for r in buckets.get(bucket, []) or []:
            title = str(r.get("title") or "")
            body = " ".join(
                [str(r.get("use_cases") or "")]
                + [str(h) for h in (r.get("hints") or [])]
            )
            title_hits = count_keyword_hits(title, keywords)
            total_hits = count_keyword_hits(f"{title} {body}", keywords)
            if total_hits < min_hits:
                continue
            act = _activation(
                useful_count=int(r.get("useful_count") or 0),
                times_misled=int(r.get("times_misled") or 0),
                confidence=r.get("confidence"),
            )
            score = (total_hits + title_hits) * act  # title hits count double
            out.append(RelevantMemory(
                kind="reflection", id=str(r.get("id")),
                text=f"{title}: {body}".strip(": "),
                polarity=bucket if bucket in ("do", "dont") else None,
                confidence=r.get("confidence"),
                useful_count=int(r.get("useful_count") or 0),
                age_days=_age_days(r.get("updated_at"), _now),
                hits=total_hits, score=score,
            ))

    try:
        semantic = backend.semantic_list(project=project, track_exposure=False)
    except Exception:  # noqa: BLE001 - degrade to no semantic
        semantic = []
    for s in semantic or []:
        content = getattr(s, "content", "") or ""
        hits = count_keyword_hits(content, keywords)
        if hits < min_hits:
            continue
        act = _activation(
            useful_count=int(getattr(s, "useful_count", 0) or 0),
            times_misled=int(getattr(s, "times_misled", 0) or 0),
            confidence=None,
        )
        out.append(RelevantMemory(
            kind="semantic", id=str(getattr(s, "id", "")),
            text=content, polarity=None, confidence=None,
            useful_count=int(getattr(s, "useful_count", 0) or 0),
            age_days=_age_days(getattr(s, "updated_at", None), _now),
            hits=hits, score=hits * act,
        ))

    out.sort(key=lambda m: (-m.score, m.id))
    return out[:max_items]


def format_relevant(items: list[RelevantMemory], *, max_items: int = 5) -> str:
    """Render the additionalContext block (<= max_items). Empty if no items."""
    if not items:
        return ""
    lines = ["RELEVANT MEMORY — apply unless it conflicts with the user's request:"]
    for m in items[:max_items]:
        tag = m.kind + (f" · conf {m.confidence:.2f}" if m.confidence is not None else "")
        lines.append(f"• [{tag}] {m.text}")
    return "\n".join(lines)
