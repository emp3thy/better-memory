"""Behaviour pins for the shared exposure-ledger module.

``better_memory.services.exposure_log`` lifts three SQL sites verbatim so
both the sqlite and (future) agentcore storage backends can share one
implementation:

- ``record``: the first-source-wins INSERT..WHERE NOT EXISTS dedup guard
  from ``SessionBootstrapService.record_exposures``, extended with an
  ``exploration_ids`` param that mirrors ``reflection.py``'s retrieve-path
  ``via_exploration`` write.
- ``list_unrated``: the grouped/deduped/display-joined query from
  ``SessionBootstrapService.list_session_exposures``.
- ``stamp``: the exposure-row UPDATE from
  ``MemoryRatingService._apply_one`` (copied, not rewired).

No commits inside the module — callers own the transaction.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services import exposure_log


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_reflection(conn, rid: str, *, title: str = "t") -> None:
    conn.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES (?, ?, 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')""",
        (rid, title),
    )
    conn.commit()


def _seed_semantic(conn, sid: str, *, content: str = "fact") -> None:
    conn.execute(
        """INSERT INTO semantic_memories
           (id, content, project, scope, created_at, updated_at)
           VALUES (?, ?, 'p', 'project', '2026-01-01', '2026-01-01')""",
        (sid, content),
    )
    conn.commit()


def _rows(conn, session_id="s1"):
    return conn.execute(
        "SELECT memory_kind, memory_id, source, via_exploration "
        "FROM session_memory_exposure WHERE session_id = ? "
        "ORDER BY memory_kind, memory_id",
        (session_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


class TestRecord:
    def test_inserts_one_row_per_item(self, conn):
        exposure_log.record(
            conn,
            session_id="s1",
            items=[("reflection", "r-a"), ("semantic", "s-a")],
            source="bootstrap",
            now="2026-01-01T00:00:00Z",
        )
        conn.commit()
        rows = _rows(conn)
        assert len(rows) == 2
        assert {r["source"] for r in rows} == {"bootstrap"}

    def test_no_op_when_session_id_empty(self, conn):
        exposure_log.record(
            conn, session_id="", items=[("reflection", "r-a")],
            source="bootstrap", now="2026-01-01T00:00:00Z",
        )
        conn.commit()
        assert _rows(conn, session_id="") == []

    def test_no_op_when_items_empty(self, conn):
        exposure_log.record(
            conn, session_id="s1", items=[], source="bootstrap",
            now="2026-01-01T00:00:00Z",
        )
        conn.commit()
        assert _rows(conn) == []

    def test_first_source_wins_dedup(self, conn):
        exposure_log.record(
            conn, session_id="s1", items=[("reflection", "r-a")],
            source="bootstrap", now="2026-01-01T00:00:00Z",
        )
        exposure_log.record(
            conn, session_id="s1", items=[("reflection", "r-a")],
            source="retrieve", now="2026-01-01T00:01:00Z",
        )
        conn.commit()
        rows = _rows(conn)
        assert len(rows) == 1
        assert rows[0]["source"] == "bootstrap"

    def test_other_sessions_unaffected(self, conn):
        exposure_log.record(
            conn, session_id="s1", items=[("reflection", "r-a")],
            source="bootstrap", now="2026-01-01T00:00:00Z",
        )
        exposure_log.record(
            conn, session_id="s2", items=[("reflection", "r-a")],
            source="bootstrap", now="2026-01-01T00:00:00Z",
        )
        conn.commit()
        assert len(_rows(conn, "s1")) == 1
        assert len(_rows(conn, "s2")) == 1

    def test_exploration_ids_tag_via_exploration(self, conn):
        exposure_log.record(
            conn, session_id="s1",
            items=[("reflection", "r-untested"), ("reflection", "r-proven")],
            source="retrieve", now="2026-01-01T00:00:00Z",
            exploration_ids=frozenset({"r-untested"}),
        )
        conn.commit()
        flags = {r["memory_id"]: r["via_exploration"] for r in _rows(conn)}
        assert flags["r-untested"] == 1
        assert flags["r-proven"] == 0

    def test_default_exploration_ids_is_empty_frozenset(self, conn):
        exposure_log.record(
            conn, session_id="s1", items=[("reflection", "r-a")],
            source="bootstrap", now="2026-01-01T00:00:00Z",
        )
        conn.commit()
        assert _rows(conn)[0]["via_exploration"] == 0

    def test_dedup_wins_over_exploration_tag(self, conn):
        # Memory already exposed (normal, untagged) — a later call that
        # would have tagged it as an exploration serve must not retag it;
        # first-source-wins applies to the whole row, tag included.
        exposure_log.record(
            conn, session_id="s1", items=[("reflection", "r-x")],
            source="bootstrap", now="2026-01-01T00:00:00Z",
        )
        exposure_log.record(
            conn, session_id="s1", items=[("reflection", "r-x")],
            source="retrieve", now="2026-01-01T00:01:00Z",
            exploration_ids=frozenset({"r-x"}),
        )
        conn.commit()
        rows = _rows(conn)
        assert len(rows) == 1
        assert rows[0]["via_exploration"] == 0

    def test_no_commit_inside_record(self, conn):
        exposure_log.record(
            conn, session_id="s1", items=[("reflection", "r-a")],
            source="bootstrap", now="2026-01-01T00:00:00Z",
        )
        # The module never commits — the write started an implicit
        # transaction that only the caller's own commit() can close.
        assert conn.in_transaction is True
        conn.commit()
        assert conn.in_transaction is False


# ---------------------------------------------------------------------------
# list_unrated
# ---------------------------------------------------------------------------


class TestListUnrated:
    def test_groups_by_kind_and_id_keeping_min_exposed_at_and_source(self, conn):
        rid = "r-a"
        _seed_reflection(conn, rid, title="refl-title")
        conn.executemany(
            "INSERT INTO session_memory_exposure "
            "(session_id, memory_kind, memory_id, exposed_at, source) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("s1", "reflection", rid, "2026-01-01T12:00:00Z", "bootstrap"),
                ("s1", "reflection", rid, "2026-01-01T13:00:00Z", "retrieve"),
            ],
        )
        conn.commit()

        rows = exposure_log.list_unrated(conn, session_id="s1")

        assert len(rows) == 1
        row = rows[0]
        assert row["memory_kind"] == "reflection"
        assert row["memory_id"] == rid
        assert row["exposed_at"] == "2026-01-01T12:00:00Z"
        assert row["source"] == "bootstrap"
        assert row["display"] == "refl-title"

    def test_semantic_display_uses_content(self, conn):
        sid = "s-a"
        _seed_semantic(conn, sid, content="prefer short filenames")
        conn.execute(
            "INSERT INTO session_memory_exposure "
            "(session_id, memory_kind, memory_id, exposed_at, source) "
            "VALUES ('s1', 'semantic', ?, '2026-01-01T12:00:00Z', 'retrieve')",
            (sid,),
        )
        conn.commit()

        rows = exposure_log.list_unrated(conn, session_id="s1")

        assert len(rows) == 1
        assert rows[0]["display"] == "prefer short filenames"

    def test_excludes_rated_rows(self, conn):
        rid = "r-rated"
        _seed_reflection(conn, rid)
        conn.execute(
            "INSERT INTO session_memory_exposure "
            "(session_id, memory_kind, memory_id, exposed_at, source, "
            " rated_at, classification) VALUES "
            "('s1', 'reflection', ?, '2026-01-01T12:00:00Z', 'bootstrap', "
            " '2026-01-01T13:00:00Z', 'cited')",
            (rid,),
        )
        conn.commit()

        rows = exposure_log.list_unrated(conn, session_id="s1")
        assert rows == []

    def test_orders_by_exposed_at_ascending(self, conn):
        _seed_reflection(conn, "r-later")
        _seed_reflection(conn, "r-earlier")
        conn.executemany(
            "INSERT INTO session_memory_exposure "
            "(session_id, memory_kind, memory_id, exposed_at, source) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("s1", "reflection", "r-later", "2026-01-01T14:00:00Z", "bootstrap"),
                ("s1", "reflection", "r-earlier", "2026-01-01T12:00:00Z", "bootstrap"),
            ],
        )
        conn.commit()

        rows = exposure_log.list_unrated(conn, session_id="s1")
        assert [r["memory_id"] for r in rows] == ["r-earlier", "r-later"]

    def test_empty_session_has_no_rows(self, conn):
        assert exposure_log.list_unrated(conn, session_id="no-such-session") == []


# ---------------------------------------------------------------------------
# stamp
# ---------------------------------------------------------------------------


class TestStamp:
    def test_stamps_evidence_classification_and_rated_at(self, conn):
        rid = "r-a"
        _seed_reflection(conn, rid)
        conn.execute(
            "INSERT INTO session_memory_exposure "
            "(session_id, memory_kind, memory_id, exposed_at, source) "
            "VALUES ('s1', 'reflection', ?, '2026-01-01T12:00:00Z', 'bootstrap')",
            (rid,),
        )
        conn.commit()

        rowcount = exposure_log.stamp(
            conn, session_id="s1", kind="reflection", memory_id=rid,
            classification="cited", evidence="fixed the bug",
            now="2026-01-01T13:00:00Z",
        )
        conn.commit()

        assert rowcount == 1
        row = conn.execute(
            "SELECT rated_at, classification, evidence "
            "FROM session_memory_exposure WHERE memory_id = ?",
            (rid,),
        ).fetchone()
        assert row["rated_at"] == "2026-01-01T13:00:00Z"
        assert row["classification"] == "cited"
        assert row["evidence"] == "fixed the bug"

    def test_only_updates_unrated_rows(self, conn):
        # Two exposure rows for the same (session, kind, id) — bootstrap +
        # retrieve. Both must be stamped by one call (no LIMIT on the UPDATE).
        rid = "r-a"
        _seed_reflection(conn, rid)
        conn.executemany(
            "INSERT INTO session_memory_exposure "
            "(session_id, memory_kind, memory_id, exposed_at, source) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("s1", "reflection", rid, "2026-01-01T12:00:00Z", "bootstrap"),
                ("s1", "reflection", rid, "2026-01-01T13:00:00Z", "retrieve"),
            ],
        )
        conn.commit()

        rowcount = exposure_log.stamp(
            conn, session_id="s1", kind="reflection", memory_id=rid,
            classification="shaped", evidence="used it",
            now="2026-01-01T14:00:00Z",
        )
        conn.commit()

        assert rowcount == 2

    def test_returns_zero_when_already_rated(self, conn):
        rid = "r-a"
        _seed_reflection(conn, rid)
        conn.execute(
            "INSERT INTO session_memory_exposure "
            "(session_id, memory_kind, memory_id, exposed_at, source, "
            " rated_at, classification) VALUES "
            "('s1', 'reflection', ?, '2026-01-01T12:00:00Z', 'bootstrap', "
            " '2026-01-01T13:00:00Z', 'cited')",
            (rid,),
        )
        conn.commit()

        rowcount = exposure_log.stamp(
            conn, session_id="s1", kind="reflection", memory_id=rid,
            classification="shaped", evidence="second pass",
            now="2026-01-01T15:00:00Z",
        )
        conn.commit()

        assert rowcount == 0

    def test_no_commit_inside_stamp(self, conn):
        rid = "r-a"
        _seed_reflection(conn, rid)
        conn.execute(
            "INSERT INTO session_memory_exposure "
            "(session_id, memory_kind, memory_id, exposed_at, source) "
            "VALUES ('s1', 'reflection', ?, '2026-01-01T12:00:00Z', 'bootstrap')",
            (rid,),
        )
        conn.commit()

        exposure_log.stamp(
            conn, session_id="s1", kind="reflection", memory_id=rid,
            classification="cited", evidence="fixed the bug",
            now="2026-01-01T13:00:00Z",
        )
        assert conn.in_transaction is True
        conn.commit()


# ---------------------------------------------------------------------------
# display column
# ---------------------------------------------------------------------------


class TestDisplayColumn:
    def test_session_memory_exposure_has_display_column(self, conn):
        cols = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(session_memory_exposure)"
            )
        }
        assert "display" in cols
