"""Relevance filter over the curated memory set (semantic + reflections).

Evidence-gated scorer. On the sqlite backend ONE gate applies to both
memory kinds (design spec 2026-10-05-just-in-time-serving-design.md §1):

- tokenise the prompt with ``extract_keywords``;
- drop any token that is *ubiquitous* in the candidate pool -- present in
  more than ``DF_CAP`` of the pool's memories, counted only when the pool
  has at least ``DF_MIN_POOL`` memories (a smaller pool keeps every token,
  otherwise nothing could ever qualify);
- count distinct whole-word hits per memory; a memory qualifies when
  ``hits >= min_hits`` (``cfg.context_min_hits``, default 2).

The replay that motivated this (11 rated sessions) showed the old "any one
BM25 token" qualifier serving web-research pitfalls to a repo-exploration
prompt because both mentioned "Claude Code": a single shared token is not
evidence. Two distinct non-ubiquitous tokens measured 43% useful against
19% for the old gate.

Ranking among qualifiers is reciprocal rank fusion of the Wilson prior
(``services.scoring``) with a relevance rank: distinct hits descending,
ties broken by BM25 rank over ``reflection_fts`` where that table exists.
The prior never qualifies a memory by itself.

Agentcore (``conn=None`` AND ``supports_synthesis=False``) is unchanged:
the gate is membership in ``backend.relevance_ranks`` (server-side
semantic search), falling back to a keyword-hit floor ONLY when that
lookup itself FAILS (``None`` -- an AWS error); a genuinely empty ``{}`` is
a legitimate negative.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from better_memory.search.query import sanitize_fts5_query
from better_memory.services.keywords import count_keyword_hits, extract_keywords
from better_memory.services.scoring import wilson_lower_bound
from better_memory.services.triggers import match as match_trigger

#: Keyword-hit floor for the agentcore fallback (relevance_ranks failed).
_FALLBACK_MIN_HITS = 2

#: A token present in more than this fraction of the candidate pool is
#: ubiquitous and carries no evidence. Measured band: 50% on a pool of 10
#: dropped "claude", "code", "user" and the like; see spec Assumptions A4.
DF_CAP = 0.5

#: The ubiquity filter only runs when the pool has at least this many
#: memories; below it every token in a 1- or 2-memory pool would exceed
#: DF_CAP and nothing could qualify.
DF_MIN_POOL = 4

#: Reciprocal rank fusion constant, matching search/hybrid.py and
#: ReflectionSynthesisService._fuse_by_relevance.
_RRF_K = 60


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
    reason: str | None = None   # trigger string that fired (trigger channel only)


def _age_days(iso_ts: str | None, now: datetime) -> int | None:
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0, (now - ts).days)


def _bm25_qualifiers(conn: sqlite3.Connection | None, query: str) -> dict[str, int]:
    """reflection_id -> BM25 rank (0 best) for reflections matching query."""
    sanitized = sanitize_fts5_query(query)
    tokens = [t for t in sanitized.split() if len(t) > 2]
    if not tokens or conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT r.id, bm25(reflection_fts) AS bm "
            "FROM reflection_fts JOIN reflections r ON r.rowid = reflection_fts.rowid "
            "WHERE reflection_fts MATCH ? ORDER BY bm ASC",
            (" OR ".join(tokens),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row[0]: i for i, row in enumerate(rows)}


def _ubiquitous(token_sets: list[set[str]]) -> set[str]:
    """Tokens present in more than DF_CAP of the pool (pool >= DF_MIN_POOL)."""
    n = len(token_sets)
    if n < DF_MIN_POOL:
        return set()
    counts: dict[str, int] = {}
    for toks in token_sets:
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
    return {t for t, c in counts.items() if c > DF_CAP * n}


def _wilson_for(useful: int, overlooked: int, ignored: int) -> float:
    positive = useful + overlooked
    n = useful + overlooked + ignored
    return wilson_lower_bound(positive, n)


def _rrf_score(candidates: list[dict]) -> list[tuple[float, dict]]:
    """RRF-fuse the Wilson prior with the relevance rank stashed on each
    candidate dict (``rel_rank``, ``None`` if absent).

    Both ranks are computed relative to this candidate set: the prior rank
    by Wilson score descending; the relevance rank by distinct hits
    descending, then ``bm_rank`` ascending (None last), then id.
    """
    order_by_wilson = sorted(range(len(candidates)), key=lambda i: -candidates[i]["wilson"])
    prior_rank = {candidates[i]["id"]: rank for rank, i in enumerate(order_by_wilson)}

    scored: list[tuple[float, dict]] = []
    for c in candidates:
        present_ranks = [prior_rank[c["id"]]]
        if c.get("rel_rank") is not None:
            present_ranks.append(c["rel_rank"])
        score = sum(1.0 / (_RRF_K + rank) for rank in present_ranks)
        scored.append((score, c))
    return scored


def _assign_rel_ranks(candidates: list[dict]) -> None:
    """Set ``rel_rank`` on each candidate: hits desc, bm_rank asc (None last), id."""
    def key(c: dict):
        bm = c.get("bm_rank")
        return (-c["hits"], bm if bm is not None else 10**9, c["id"])
    for rank, c in enumerate(sorted(candidates, key=key)):
        c["rel_rank"] = rank


def retrieve_relevant(
    backend: Any,
    *,
    query: str,
    project: str,
    conn: sqlite3.Connection | None = None,
    max_items: int = 3,
    include_neutral: bool = False,
    now: Callable[[], datetime] | None = None,
    min_hits: int = 2,
) -> list[RelevantMemory]:
    """Gate + rank curated memories (semantic + reflections) for ``query``.

    Sqlite: a memory is returned only when at least ``min_hits`` distinct
    non-ubiquitous query tokens appear in it (see module docstring).
    Agentcore (``conn=None`` AND ``supports_synthesis=False``): membership
    in ``backend.relevance_ranks``, with the keyword-hit floor only when
    that lookup FAILS (``None``). Ranking is RRF of the Wilson prior and
    the relevance rank. Never raises -- any backend/leg failure degrades
    that leg to "absent".
    """
    if not (query or "").strip():
        return []

    _now = (now or (lambda: datetime.now(UTC)))()

    try:
        buckets = backend.retrieve(project=project, track_exposure=False)
    except Exception:  # noqa: BLE001 - degrade to no reflections
        buckets = {}
    try:
        semantic = backend.semantic_list(project=project, track_exposure=False)
    except Exception:  # noqa: BLE001 - degrade to no semantic
        semantic = []

    refl_bucket_order = ["do", "dont"] + (["neutral"] if include_neutral else [])

    agentcore_mode = (
        conn is None
        and hasattr(backend, "relevance_ranks")
        and getattr(backend, "supports_synthesis", True) is False
    )
    # None vs {} from relevance_ranks is load-bearing: None means the lookup
    # itself failed (AWS error) -- THAT is the keyword-fallback trigger. {}
    # means it ran fine and found nothing, which must NOT re-qualify
    # memories via keyword overlap.
    raw_rank_map: dict[tuple[str, str], int] | None = None
    if agentcore_mode:
        try:
            raw_rank_map = backend.relevance_ranks(
                query=query, kinds=("reflection", "semantic"),
            )
        except Exception:  # noqa: BLE001 - best-effort; degrade to keyword fallback
            raw_rank_map = None
    agentcore_kw_fallback = agentcore_mode and raw_rank_map is None
    rank_map: dict[tuple[str, str], int] = raw_rank_map or {}

    # Flatten the pool once: (kind, id, display text, polarity, row/obj).
    pool: list[dict] = []
    for bucket in refl_bucket_order:
        for r in buckets.get(bucket, []) or []:
            title = str(r.get("title") or "")
            body = " ".join(
                [str(r.get("use_cases") or "")]
                + [str(h) for h in (r.get("hints") or [])]
            )
            pool.append({
                "kind": "reflection", "id": str(r.get("id")),
                "match_text": f"{title} {body}",
                "text": f"{title}: {body}".strip(": "),
                "polarity": bucket if bucket in ("do", "dont") else None,
                "confidence": r.get("confidence"),
                "useful_count": int(r.get("useful_count") or 0),
                "age_days": _age_days(r.get("updated_at"), _now),
                "wilson": _wilson_for(
                    int(r.get("useful_count") or 0),
                    int(r.get("times_overlooked") or 0),
                    int(r.get("times_ignored") or 0),
                ),
            })
    for s_ in semantic or []:
        content = getattr(s_, "content", "") or ""
        pool.append({
            "kind": "semantic", "id": str(getattr(s_, "id", "")),
            "match_text": content, "text": content, "polarity": None,
            "confidence": None,
            "useful_count": int(getattr(s_, "useful_count", 0) or 0),
            "age_days": _age_days(getattr(s_, "updated_at", None), _now),
            "wilson": _wilson_for(
                int(getattr(s_, "useful_count", 0) or 0),
                int(getattr(s_, "times_overlooked", 0) or 0),
                int(getattr(s_, "times_ignored", 0) or 0),
            ),
        })

    raw_keywords = extract_keywords(query)
    if agentcore_mode:
        keywords = raw_keywords                      # fallback evidence only
    else:
        ubiquitous = _ubiquitous([extract_keywords(c["match_text"]) for c in pool])
        keywords = raw_keywords - ubiquitous
    bm = _bm25_qualifiers(conn, query) if not agentcore_mode else {}

    qualifiers: list[dict] = []
    for c in pool:
        kw_hits = count_keyword_hits(c["match_text"], keywords)
        key = (c["kind"], c["id"])
        if agentcore_mode:
            in_backend_rank = key in rank_map
            fallback_ok = agentcore_kw_fallback and kw_hits >= _FALLBACK_MIN_HITS
            if not (in_backend_rank or fallback_ok):
                continue
            c["bm_rank"] = rank_map.get(key)
        else:
            if kw_hits < min_hits:
                continue
            c["bm_rank"] = bm.get(c["id"]) if c["kind"] == "reflection" else None
        c["hits"] = kw_hits
        qualifiers.append(c)

    refl_candidates = [c for c in qualifiers if c["kind"] == "reflection"]
    sem_candidates = [c for c in qualifiers if c["kind"] == "semantic"]
    if agentcore_mode:
        # Preserve the backend's own ordering as the relevance rank.
        for c in refl_candidates + sem_candidates:
            c["rel_rank"] = c["bm_rank"]
    else:
        _assign_rel_ranks(refl_candidates)
        _assign_rel_ranks(sem_candidates)

    all_scored = _rrf_score(refl_candidates) + _rrf_score(sem_candidates)
    all_scored.sort(key=lambda t: (-t[0], t[1]["id"]))

    return [
        RelevantMemory(
            kind=c["kind"], id=c["id"], text=c["text"], polarity=c["polarity"],
            confidence=c["confidence"], useful_count=c["useful_count"],
            age_days=c["age_days"], hits=c["hits"], score=score,
        )
        for score, c in all_scored[:max_items]
    ]


def triggered_memories(
    backend: Any,
    *,
    project: str,
    tool_name: str,
    tool_input: dict | None,
    large_write_chars: int,
    now: Callable[[], datetime] | None = None,
) -> list[RelevantMemory]:
    """Trigger channel (PreToolUse): every memory whose triggers fire for
    this tool call, best Wilson prior first. ``reason`` carries the trigger
    string that fired. No keyword matching happens here -- a memory without
    triggers is never tool-triggered. Never raises: a backend failure
    yields []."""
    _now = (now or (lambda: datetime.now(UTC)))()
    try:
        candidates = backend.triggered_candidates(project=project)
    except Exception:  # noqa: BLE001 - degrade to no trigger candidates
        return []
    out: list[RelevantMemory] = []
    for c in candidates or []:
        reason = match_trigger(
            list(c.get("triggers") or []), tool_name, tool_input,
            large_write_chars=large_write_chars,
        )
        if reason is None:
            continue
        wilson = _wilson_for(
            int(c.get("useful_count") or 0),
            int(c.get("times_overlooked") or 0),
            int(c.get("times_ignored") or 0),
        )
        out.append(RelevantMemory(
            kind=str(c.get("kind")), id=str(c.get("id")), text=str(c.get("text") or ""),
            polarity=c.get("polarity"), confidence=c.get("confidence"),
            useful_count=int(c.get("useful_count") or 0),
            age_days=_age_days(c.get("updated_at"), _now),
            hits=0, score=wilson, reason=reason,
        ))
    out.sort(key=lambda m: (-m.score, m.id))
    return out


_TEXT_MAX_CHARS = 400

_BLOCK_HEADER = (
    '<project-memory source="better-memory">\n'
    "Prior knowledge from past sessions in this project "
    "(factual records; verify if stale):"
)
_BLOCK_FOOTER = (
    "If any entry above materially helps or misleads this task, credit it now: "
    "memory_credit(kind, id, class, evidence) - include a one-line evidence "
    "statement.\n"
    "</project-memory>"
)


def _meta_tag(m: RelevantMemory) -> str:
    parts = [f"{m.kind} {m.id}"]
    if m.confidence is not None:
        parts.append(f"conf {m.confidence:.1f}")
    if m.useful_count:
        parts.append(f"used {m.useful_count}x")
    if m.age_days is not None:
        parts.append(f"{m.age_days}d old")
    return "[" + " | ".join(parts) + "]"


def format_relevant(items: list[RelevantMemory]) -> str:
    """Render the additionalContext block. Empty string if no items."""
    if not items:
        return ""
    lines = [_BLOCK_HEADER]
    for i, m in enumerate(items, start=1):
        text = m.text if len(m.text) <= _TEXT_MAX_CHARS else m.text[: _TEXT_MAX_CHARS - 3] + "..."
        if m.polarity == "dont":
            text = f"Known pitfall -- do this instead: {text}"
        lines.append(f"{i}. {_meta_tag(m)}")
        lines.append(f"   {text}")
        if m.reason:
            lines.append(f"   Triggered by: {m.reason}")
    lines.append(_BLOCK_FOOTER)
    return "\n".join(lines)
