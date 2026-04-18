"""Tests for the shared audit helper :mod:`better_memory.services.audit`.

Covers only the helper in isolation — service integration is exercised by
the per-service audit tests (``test_observation.py``, ``test_insight.py``)
and the end-to-end round-trip in ``test_audit_trail.py``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services import audit


@pytest.fixture
def conn(tmp_memory_db: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_memory_db)
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


def test_log_inserts_row_with_serialized_detail(conn: sqlite3.Connection) -> None:
    audit.log(
        conn,
        entity_type="test",
        entity_id="x",
        action="probe",
        detail={"k": 1},
    )
    conn.commit()

    rows = conn.execute("SELECT * FROM audit_log").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["entity_type"] == "test"
    assert row["entity_id"] == "x"
    assert row["action"] == "probe"
    # Detail is stored as JSON-encoded text.
    assert row["detail"] == '{"k": 1}'
    # Round-trip through json.loads for good measure.
    assert json.loads(row["detail"]) == {"k": 1}


def test_log_id_is_32_char_hex(conn: sqlite3.Connection) -> None:
    audit.log(conn, entity_type="t", entity_id="i", action="a")
    conn.commit()
    row = conn.execute("SELECT id FROM audit_log").fetchone()
    assert isinstance(row["id"], str)
    assert len(row["id"]) == 32
    # All characters are lowercase hex.
    int(row["id"], 16)


def test_log_with_none_detail_stores_null(conn: sqlite3.Connection) -> None:
    audit.log(conn, entity_type="t", entity_id="i", action="a", detail=None)
    conn.commit()
    row = conn.execute("SELECT detail FROM audit_log").fetchone()
    assert row["detail"] is None


def test_log_defaults_actor_to_ai(conn: sqlite3.Connection) -> None:
    audit.log(conn, entity_type="t", entity_id="i", action="a")
    conn.commit()
    row = conn.execute("SELECT actor FROM audit_log").fetchone()
    assert row["actor"] == "ai"


def test_log_honours_all_optional_fields(conn: sqlite3.Connection) -> None:
    audit.log(
        conn,
        entity_type="insight",
        entity_id="i1",
        action="status_changed",
        actor="human",
        triggered_by="user",
        from_status="pending_review",
        to_status="confirmed",
        session_id="sess-1",
        detail={"note": "promoted"},
    )
    conn.commit()
    row = conn.execute(
        "SELECT actor, triggered_by, from_status, to_status, session_id, detail "
        "FROM audit_log"
    ).fetchone()
    assert row["actor"] == "human"
    assert row["triggered_by"] == "user"
    assert row["from_status"] == "pending_review"
    assert row["to_status"] == "confirmed"
    assert row["session_id"] == "sess-1"
    assert json.loads(row["detail"]) == {"note": "promoted"}


def test_log_does_not_commit(conn: sqlite3.Connection, tmp_memory_db: Path) -> None:
    """``audit.log`` leaves the transaction open; a second connection cannot see
    the uncommitted row until the test connection commits."""
    audit.log(conn, entity_type="t", entity_id="i", action="a")
    # ``in_transaction`` is True after the INSERT — ``audit.log`` did not commit.
    assert conn.in_transaction is True

    # A separate connection to the same DB file must NOT see the uncommitted row.
    other = sqlite3.connect(str(tmp_memory_db))
    try:
        count = other.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        assert count == 0
    finally:
        other.close()

    conn.commit()
    # After commit a fresh connection sees the row.
    other2 = sqlite3.connect(str(tmp_memory_db))
    try:
        count = other2.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        assert count == 1
    finally:
        other2.close()
