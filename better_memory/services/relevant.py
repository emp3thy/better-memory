"""Relevance filter over the curated memory set (semantic + reflections).

Evidence-gated scorer, replacing the old pure-keyword hits-x-activation
model: a memory injects only when it has positive relevance EVIDENCE --

- BM25 match against ``reflection_fts`` (title / use_cases / hints), or
- (only when that leg is structurally unavailable -- no sqlite ``conn``
  for reflections; always, for semantics, which have no FTS substrate at
  all) a keyword-hit floor as a degraded fallback.

The Wilson lower-bound prior (see ``services.scoring``) never qualifies a
memory by itself -- it only RANKS among qualifiers via reciprocal rank
fusion (RRF), alongside the BM25 rank. Popularity forcing irrelevant
injections was the old failure mode (13% useful as bootstrap); the gate
exists specifically to close it.

Fetches the small, already-ranked sets through the StorageBackend
abstraction (works on sqlite AND agentcore); the BM25 leg additionally
requires a raw sqlite ``conn``. Agentcore (``conn=None`` AND
``supports_synthesis=False``) replaces the BM25 leg wholesale with
``backend.relevance_ranks`` -- a server-side semantic-search rank map --
and falls back to the keyword-hit floor ONLY when that lookup itself
FAILS (``relevance_ranks`` returns ``None`` -- an AWS error). A
successful lookup that genuinely finds nothing (``{}``) does NOT trigger
the fallback -- a legitimate negative result from the server-side gate
must not be overridden by keyword overlap. A sqlite backend called with
``conn=None`` keeps the pre-existing keyword-fallback behavior
unchanged, regardless of whether it also implements ``relevance_ranks``
(it does, for protocol completeness only).
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

#: Keyword-hit floor used only when the FTS leg is structurally
#: unavailable (no sqlite conn, for reflections; always, for semantics,
#: which have no FTS substrate at all).
_FALLBACK_MIN_HITS = 2

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


def _wilson_for(useful: int, overlooked: int, ignored: int) -> float:
    positive = useful + overlooked
    n = useful + overlooked + ignored
    return wilson_lower_bound(positive, n)


def _rrf_score(candidates: list[dict]) -> list[tuple[float, dict]]:
    """RRF-fuse the Wilson prior with the BM25 rank already stashed
    on each candidate dict (``bm_rank``, ``None`` if absent).

    The prior rank is computed fresh here (desc by Wilson score) rather
    than carried in, since it only makes sense relative to the other
    qualifiers in this same candidate set.
    """
    order_by_wilson = sorted(range(len(candidates)), key=lambda i: -candidates[i]["wilson"])
    prior_rank = {candidates[i]["id"]: rank for rank, i in enumerate(order_by_wilson)}

    scored: list[tuple[float, dict]] = []
    for c in candidates:
        present_ranks = [prior_rank[c["id"]]]
        if c["bm_rank"] is not None:
            present_ranks.append(c["bm_rank"])
        score = sum(1.0 / (_RRF_K + rank) for rank in present_ranks)
        scored.append((score, c))
    return scored


def retrieve_relevant(
    backend: Any,
    *,
    query: str,
    project: str,
    conn: sqlite3.Connection | None = None,
    max_items: int = 3,
    include_neutral: bool = False,
    now: Callable[[], datetime] | None = None,
) -> list[RelevantMemory]:
    """Gate + rank curated memories (semantic + reflections) for ``query``.

    A memory is returned only if it clears the evidence gate: a BM25 match
    (reflections), or (only when that leg is structurally unavailable --
    no sqlite ``conn`` for reflections; always, for semantics, which have
    no FTS leg at all) a keyword-hit fallback. Among qualifiers, ranking
    is RRF over the Wilson prior plus the BM25 rank when present.

    Agentcore backends (``conn=None`` AND ``supports_synthesis=False``)
    replace the BM25 leg with ``backend.relevance_ranks`` -- a
    server-side semantic-search rank map fused into the same RRF -- and
    fall back to the keyword-hit floor only when that lookup FAILS
    (returns ``None`` -- an AWS error), never merely because it found
    nothing (``{}`` is a legitimate negative result the gate must
    respect). Sqlite backends are unaffected by this branch even when
    called with ``conn=None``.

    Never raises -- any backend/leg failure degrades that leg to "absent"
    rather than propagating.
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

    bm = _bm25_qualifiers(conn, query)
    refl_bucket_order = ["do", "dont"] + (["neutral"] if include_neutral else [])
    keywords = extract_keywords(query)     # fallback evidence only

    fts_unavailable = conn is None

    # Agentcore evidence gate (design spec 2026-07-24-agentcore-parity-
    # design.md §3): when the caller has no sqlite FTS substrate
    # (conn=None) AND the backend is agentcore-flavored --
    # supports_synthesis=False, the existing Protocol capability flag that
    # already distinguishes AgentCoreBackend from SqliteBackend everywhere
    # else in this codebase -- the evidence gate becomes membership in a
    # backend-computed relevance rank map (server-side RetrieveMemoryRecords
    # semantic search) instead of the keyword-hit fallback. A conn=None
    # SqliteBackend caller (supports_synthesis=True) is explicitly excluded
    # here, so sqlite's own fallback semantics stay byte-for-byte unchanged
    # regardless of whether SqliteBackend also implements relevance_ranks
    # (it does, for protocol completeness -- see storage/sqlite.py -- but
    # this function never calls it in that case).
    agentcore_mode = (
        conn is None
        and hasattr(backend, "relevance_ranks")
        and getattr(backend, "supports_synthesis", True) is False
    )
    # None vs {} from relevance_ranks is load-bearing (see
    # StorageBackend.relevance_ranks's docstring): None means the
    # backend-side lookup itself failed (AWS error on every namespace) --
    # THAT is the keyword-fallback trigger. {} means the lookup ran fine
    # and genuinely found nothing, which must NOT re-qualify memories via
    # keyword overlap -- the server-side gate's negative result stands.
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

    refl_candidates: list[dict] = []
    for bucket in refl_bucket_order:
        for r in buckets.get(bucket, []) or []:
            r_id = str(r.get("id"))
            title = str(r.get("title") or "")
            body = " ".join(
                [str(r.get("use_cases") or "")]
                + [str(h) for h in (r.get("hints") or [])]
            )
            text = f"{title} {body}"
            kw_hits = count_keyword_hits(text, keywords)

            in_bm = r_id in bm
            in_backend_rank = agentcore_mode and ("reflection", r_id) in rank_map
            fallback_ok = (
                (agentcore_kw_fallback if agentcore_mode else fts_unavailable)
                and kw_hits >= _FALLBACK_MIN_HITS
            )
            if not (in_bm or in_backend_rank or fallback_ok):
                continue

            refl_candidates.append({
                "id": r_id, "kind": "reflection",
                "polarity": bucket if bucket in ("do", "dont") else None,
                "text": f"{title}: {body}".strip(": "),
                "confidence": r.get("confidence"),
                "useful_count": int(r.get("useful_count") or 0),
                "age_days": _age_days(r.get("updated_at"), _now),
                "hits": kw_hits if (in_bm or in_backend_rank or fallback_ok) else 0,
                "bm_rank": (
                    rank_map.get(("reflection", r_id))
                    if agentcore_mode else bm.get(r_id)
                ),
                # storage.protocol.retrieve guarantees times_overlooked/
                # times_ignored on both backends (sqlite columns; agentcore
                # copies its internal overlooked counter and hardcodes
                # ignored=0 — see storage/agentcore.py::_parse_reflection_record).
                # The .get(...) defaults below are defensive, not load-bearing.
                "wilson": _wilson_for(
                    int(r.get("useful_count") or 0),
                    int(r.get("times_overlooked") or 0),
                    int(r.get("times_ignored") or 0),
                ),
            })

    sem_candidates: list[dict] = []
    for s in semantic or []:
        s_id = str(getattr(s, "id", ""))
        content = getattr(s, "content", "") or ""
        kw_hits = count_keyword_hits(content, keywords)

        in_backend_rank = agentcore_mode and ("semantic", s_id) in rank_map
        # Semantics have no FTS/BM25 leg and no vec leg either, so the
        # keyword-hit floor is their only evidence leg outside agentcore
        # mode -- it is always "on" here (there is no other leg to gate
        # it behind). In agentcore mode the backend rank map replaces it,
        # so the fallback there fires only per the same
        # agentcore_kw_fallback signal as reflections (relevance_ranks
        # returned None == AWS error; a genuinely empty {} does NOT
        # trigger it).
        fallback_ok = (
            agentcore_kw_fallback if agentcore_mode else True
        ) and kw_hits >= _FALLBACK_MIN_HITS
        if not (in_backend_rank or fallback_ok):
            continue

        sem_candidates.append({
            "id": s_id, "kind": "semantic", "polarity": None,
            "text": content,
            "confidence": None,
            "useful_count": int(getattr(s, "useful_count", 0) or 0),
            "age_days": _age_days(getattr(s, "updated_at", None), _now),
            "hits": kw_hits if (in_backend_rank or fallback_ok) else 0,
            "bm_rank": (
                rank_map.get(("semantic", s_id))
                if agentcore_mode else None
            ),
            "wilson": _wilson_for(
                int(getattr(s, "useful_count", 0) or 0),
                int(getattr(s, "times_overlooked", 0) or 0),
                int(getattr(s, "times_ignored", 0) or 0),
            ),
        })

    all_scored = _rrf_score(refl_candidates) + _rrf_score(sem_candidates)
    all_scored.sort(key=lambda t: (-t[0], t[1]["id"]))

    out = [
        RelevantMemory(
            kind=c["kind"], id=c["id"], text=c["text"], polarity=c["polarity"],
            confidence=c["confidence"], useful_count=c["useful_count"],
            age_days=c["age_days"], hits=c["hits"], score=score,
        )
        for score, c in all_scored[:max_items]
    ]
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
    lines.append(_BLOCK_FOOTER)
    return "\n".join(lines)
