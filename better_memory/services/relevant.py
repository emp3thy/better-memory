"""Relevance filter over the curated memory set (semantic + reflections).

Fetches the small, already-ranked sets through the StorageBackend abstraction
(works on sqlite AND agentcore), whole-word keyword-filters them against a query,
and returns the top matches. Pure-Python; no embeddings, no new schema.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from better_memory.services.keywords import count_keyword_hits, extract_keywords


@dataclass
class RelevantMemory:
    kind: str            # "reflection" | "semantic"
    id: str
    summary: str         # short display text
    confidence: float | None
    hits: int            # distinct keyword hits (higher = more relevant)


def _reflection_text(r: dict[str, Any]) -> str:
    parts = [str(r.get("title") or ""), str(r.get("use_cases") or "")]
    hints = r.get("hints") or []
    if isinstance(hints, list):
        parts.extend(str(h) for h in hints)
    return " ".join(parts)


def retrieve_relevant(
    backend: Any,
    *,
    query: str,
    project: str,
    limit: int = 5,
    include_neutral: bool = False,
) -> list[RelevantMemory]:
    """Return up to ``limit`` curated memories whose text whole-word-matches a
    keyword from ``query``, ordered by (# hits desc, managed rank asc).

    Never raises: any backend error yields an empty list (the hook must not break
    a turn). "Managed rank" is the order the backend already returned items in
    (confidence / useful-count): reflections flattened do -> dont -> [neutral],
    then semantic.
    """
    keywords = extract_keywords(query)
    if not keywords:
        return []

    candidates: list[tuple[int, RelevantMemory]] = []  # (managed_rank, mem)
    rank = 0

    try:
        buckets = backend.retrieve(project=project, track_exposure=False)
    except Exception:  # noqa: BLE001 — degrade to no reflections
        buckets = {}
    order = ["do", "dont"] + (["neutral"] if include_neutral else [])
    for bucket in order:
        for r in buckets.get(bucket, []) or []:
            hits = count_keyword_hits(_reflection_text(r), keywords)
            if hits:
                candidates.append((rank, RelevantMemory(
                    kind="reflection",
                    id=str(r.get("id")),
                    summary=str(r.get("title") or _reflection_text(r))[:160],
                    confidence=r.get("confidence"),
                    hits=hits,
                )))
            rank += 1

    try:
        semantic = backend.semantic_list(project=project, track_exposure=False)
    except Exception:  # noqa: BLE001 — degrade to no semantic
        semantic = []
    for s in semantic or []:
        content = getattr(s, "content", "") or ""
        hits = count_keyword_hits(content, keywords)
        if hits:
            candidates.append((rank, RelevantMemory(
                kind="semantic",
                id=str(getattr(s, "id", "")),
                summary=content[:160],
                confidence=None,
                hits=hits,
            )))
        rank += 1

    candidates.sort(key=lambda t: (-t[1].hits, t[0]))
    return [m for _, m in candidates[:limit]]


def format_relevant(items: list[RelevantMemory], *, max_items: int = 5) -> str:
    """Render the additionalContext block (<= max_items). Empty if no items."""
    if not items:
        return ""
    lines = ["RELEVANT MEMORY — apply unless it conflicts with the user's request:"]
    for m in items[:max_items]:
        tag = m.kind + (f" · conf {m.confidence:.2f}" if m.confidence is not None else "")
        lines.append(f"• [{tag}] {m.summary}")
    return "\n".join(lines)
