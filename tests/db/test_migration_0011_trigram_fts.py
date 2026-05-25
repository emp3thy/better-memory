"""Migration 0011 — trigram FTS5 table for the sqlite embeddings backend."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


def _insert_obs(conn: sqlite3.Connection, obs_id: str, content: str) -> None:
    conn.execute(
        "INSERT INTO episodes (id, project, started_at, outcome) "
        "VALUES (?, 'p', '2026-01-01T00:00:00+00:00', NULL)",
        (f"ep-{obs_id}",),
    )
    conn.execute(
        "INSERT INTO observations (id, content, project, episode_id, "
        "status, outcome, created_at, status_changed_at) "
        "VALUES (?, ?, 'p', ?, 'active', 'neutral', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (obs_id, content, f"ep-{obs_id}"),
    )
    conn.commit()


class TestMigration0011:
    def test_trigram_table_created(self, tmp_memory_db: Path) -> None:
        c = connect(tmp_memory_db)
        try:
            apply_migrations(c)
            row = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='observation_trigram_fts'"
            ).fetchone()
            assert row is not None
        finally:
            c.close()

    def test_backfill_indexes_existing_observations(self, tmp_memory_db: Path) -> None:
        c = connect(tmp_memory_db)
        try:
            apply_migrations(c)
            _insert_obs(c, "o1", "first observation content")
            _insert_obs(c, "o2", "second observation content")
            count = c.execute(
                "SELECT COUNT(*) FROM observation_trigram_fts"
            ).fetchone()[0]
            assert count == 2
        finally:
            c.close()

    def test_insert_trigger_indexes_new_row(self, tmp_memory_db: Path) -> None:
        c = connect(tmp_memory_db)
        try:
            apply_migrations(c)
            _insert_obs(c, "o1", "hello world")
            rows = c.execute(
                "SELECT rowid FROM observation_trigram_fts WHERE "
                "observation_trigram_fts MATCH 'ello'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            c.close()

    def test_delete_trigger_removes_row(self, tmp_memory_db: Path) -> None:
        c = connect(tmp_memory_db)
        try:
            apply_migrations(c)
            _insert_obs(c, "o1", "unique content xyz")
            c.execute("DELETE FROM observations WHERE id='o1'")
            c.commit()
            count = c.execute(
                "SELECT COUNT(*) FROM observation_trigram_fts WHERE "
                "observation_trigram_fts MATCH 'xyz'"
            ).fetchone()[0]
            assert count == 0
        finally:
            c.close()

    def test_update_trigger_reindexes_row(self, tmp_memory_db: Path) -> None:
        c = connect(tmp_memory_db)
        try:
            apply_migrations(c)
            _insert_obs(c, "o1", "old content alpha")
            c.execute(
                "UPDATE observations SET content='new content bravo' WHERE id='o1'"
            )
            c.commit()
            alpha_rows = c.execute(
                "SELECT rowid FROM observation_trigram_fts WHERE "
                "observation_trigram_fts MATCH 'alpha'"
            ).fetchall()
            assert alpha_rows == []
            bravo_rows = c.execute(
                "SELECT rowid FROM observation_trigram_fts WHERE "
                "observation_trigram_fts MATCH 'bravo'"
            ).fetchall()
            assert len(bravo_rows) == 1
        finally:
            c.close()
