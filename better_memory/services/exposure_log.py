"""Shared exposure-ledger SQL: one implementation for every storage backend.

Three primitives, largely lifted from their original call sites (this module is
pinned by ``tests/services/test_exposure_log.py`` + the four other suites
listed in its docstring). Extended with display-column snapshot support:
``record`` writes a ``display`` column (title/content captured at exposure time,
truncated to 120 chars); ``list_unrated`` COALESCEs the snapshot over joined
title/content. These extensions mean the module is no longer byte-identical to
the original inline SQL:

- ``record``: the first-source-wins ``INSERT..WHERE NOT EXISTS`` dedup guard
  originally inline in ``SessionBootstrapService.record_exposures``
  (``session_bootstrap.py``), extended with an ``exploration_ids`` param that
  mirrors the ``via_exploration`` write in
  ``ReflectionSynthesisService.retrieve_reflections`` (``reflection.py``).
  Passing no ``exploration_ids`` (the default, an empty frozenset) reproduces
  the original ``record_exposures`` behaviour exactly. Extended with ``display``
  column to snapshot title/content at exposure time, truncated to 120 chars —
  needed because agentcore memory ids do not exist in local content tables.
- ``list_unrated``: the grouped/deduped/display-joined query originally
  inline in ``SessionBootstrapService.list_session_exposures``, extended to
  COALESCE the snapshot ``display`` column over the joined title/content
  for display values.
- ``stamp``: the exposure-row ``UPDATE`` originally inline in
  ``MemoryRatingService._apply_one``. Copied, not rewired — the sqlite
  rating service keeps its own inline copy; this one is for the agentcore
  backend (and any future caller) to share.

Connection ownership: NONE of these functions call ``conn.commit()``. Every
existing call site already commits after invoking the original inline SQL,
so callers must continue to do so here.
"""

from __future__ import annotations

import sqlite3

_DISPLAY_TRUNC = 120


def record(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    items: list[tuple[str, str, str | None]],
    source: str,
    now: str,
    exploration_ids: frozenset[str] = frozenset(),
) -> None:
    """Write one ``session_memory_exposure`` row per (kind, id, display) item.

    At most one row per (session, kind, id), regardless of how many times
    the memory is re-served within the session — first source (and first
    ``via_exploration`` value) wins; a later call for an already-exposed
    (session, kind, id) is a no-op, even if it would have tagged the row as
    an exploration serve.

    Display is a snapshot of the memory's title/content captured at exposure
    time (None when the caller has none), truncated to 120 chars — needed
    because agentcore memory ids do not exist in local content tables.

    Best-effort: no-op when ``session_id`` or ``items`` is empty. Does not
    commit — the caller owns the transaction.
    """
    if not session_id or not items:
        return
    conn.executemany(
        "INSERT INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source, "
        " via_exploration, display) "
        "SELECT ?, ?, ?, ?, ?, ?, ? "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM session_memory_exposure "
        "  WHERE session_id = ? AND memory_kind = ? AND memory_id = ?)",
        [
            (
                session_id,
                kind,
                mid,
                now,
                source,
                1 if mid in exploration_ids else 0,
                (display[:_DISPLAY_TRUNC] if display else None),
                session_id,
                kind,
                mid,
            )
            for kind, mid, display in items
        ],
    )


def list_unrated(conn: sqlite3.Connection, *, session_id: str) -> list[sqlite3.Row]:
    """Return unrated exposure rows for ``session_id``, grouped by memory.

    Every current writer enforces at most one exposure row per (session,
    kind, id) via a NOT-EXISTS guard, so a memory having more than one row
    here can only be a legacy duplicate predating that guard. The GROUP BY
    handles those legacy duplicates; the rating apply path stamps ALL
    unrated rows per (kind, id) in one UPDATE, so callers need one entry per
    unique memory rather than one per raw row. Rows are grouped by
    (memory_kind, memory_id) with ``MIN(exposed_at)`` / ``MIN(source)`` for
    deterministic first-exposure values, joined against
    ``reflections.title`` / ``semantic_memories.content`` for display,
    ordered by first exposure ascending, and rated rows
    (``rated_at IS NOT NULL``) excluded.

    Returns rows with columns: ``memory_kind``, ``memory_id``, ``exposed_at``,
    ``source``, ``display``.
    """
    return conn.execute(
        """
        SELECT e.memory_kind, e.memory_id,
               MIN(e.exposed_at) AS exposed_at,
               MIN(e.source) AS source,
               COALESCE(MAX(e.display), r.title, s.content) AS display
          FROM session_memory_exposure e
          LEFT JOIN reflections        r ON e.memory_kind='reflection'
                                        AND e.memory_id = r.id
          LEFT JOIN semantic_memories  s ON e.memory_kind='semantic'
                                        AND e.memory_id = s.id
         WHERE e.session_id = ? AND e.rated_at IS NULL
         GROUP BY e.memory_kind, e.memory_id
         ORDER BY exposed_at ASC
        """,
        (session_id,),
    ).fetchall()


def stamp(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    kind: str,
    memory_id: str,
    classification: str,
    evidence: str | None,
    now: str,
) -> int:
    """Stamp every unrated exposure row for (session_id, kind, memory_id).

    Mirrors ``MemoryRatingService._apply_one``'s exposure UPDATE exactly
    (there is no row LIMIT — a memory with multiple unrated exposure rows in
    one session gets all of them stamped by a single call). Returns the
    UPDATE's row count; the caller decides skip semantics (e.g. treating 0
    as "already rated" or "not exposed").
    """
    cur = conn.execute(
        "UPDATE session_memory_exposure "
        "SET rated_at = ?, classification = ?, evidence = ? "
        "WHERE session_id = ? AND memory_kind = ? AND memory_id = ?"
        "  AND rated_at IS NULL",
        (now, classification, evidence, session_id, kind, memory_id),
    )
    return cur.rowcount
