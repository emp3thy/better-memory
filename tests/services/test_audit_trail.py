"""End-to-end audit-trail verification for :class:`ObservationService`.

The Phase 10 plan's key verification test: one ``create`` + one
``record_use(outcome='success')`` produces exactly two ``audit_log`` rows
with the expected ``detail`` payloads, *without* the retrieval-audit path
ever firing.

Also covers the :attr:`ObservationService.audit_log_retrieved` toggle and
its effect on ``retrieved_count`` bumps.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.episode import EpisodeService
from better_memory.services.observation import ObservationService

_VEC_DIM = 768
_VEC_FIXED = [0.01] * _VEC_DIM


class _StubEmbedder:
    """Deterministic async embedder — never touches Ollama."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return list(_VEC_FIXED)


@pytest.fixture
def conn(tmp_memory_db: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_memory_db)
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


@pytest.fixture
def fixed_clock() -> Any:
    fixed = datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


def _make_service(
    conn: sqlite3.Connection,
    fixed_clock: Any,
    *,
    audit_log_retrieved: bool | None = None,
) -> ObservationService:
    return ObservationService(
        conn,
        _StubEmbedder(),
        clock=fixed_clock,
        project_resolver=lambda: "test-project",
        scope_resolver=lambda: None,
        session_id="sess-phase10",
        audit_log_retrieved=audit_log_retrieved,
        episodes=EpisodeService(conn),
    )


# ---------------------------------------------------------------------------
# Plan's explicit verification: 1 create + 1 record_use → exactly 2 audit rows
# ---------------------------------------------------------------------------


async def test_create_plus_record_use_produces_exactly_two_audit_rows(
    conn: sqlite3.Connection, fixed_clock: Any
) -> None:
    svc = _make_service(conn, fixed_clock)
    obs_id = await svc.create("hello", component="auth", outcome="success")
    svc.record_use(obs_id, outcome="success")

    rows = conn.execute(
        "SELECT action, actor, session_id, detail FROM audit_log "
        "WHERE entity_id = ? ORDER BY rowid",
        (obs_id,),
    ).fetchall()
    assert len(rows) == 2

    created = rows[0]
    assert created["action"] == "created"
    assert created["actor"] == "ai"
    assert created["session_id"] == "sess-phase10"
    created_detail = json.loads(created["detail"])
    assert "outcome" in created_detail
    assert created_detail["outcome"] == "success"

    used = rows[1]
    assert used["action"] == "used"
    assert used["actor"] == "ai"
    assert used["session_id"] == "sess-phase10"
    used_detail = json.loads(used["detail"])
    assert used_detail == {"outcome": "success"}


# ---------------------------------------------------------------------------
# Retrieval audit: AUDIT_LOG_RETRIEVED controls both audit rows AND counters
# ---------------------------------------------------------------------------


async def _seed_three(svc: ObservationService) -> list[str]:
    """Three observations covering each outcome so every bucket has a hit."""
    ids = [
        await svc.create("marker success", outcome="success"),
        await svc.create("marker failure", outcome="failure"),
        await svc.create("marker neutral", outcome="neutral"),
    ]
    return ids


async def test_retrieve_with_audit_on_writes_retrieved_rows_and_bumps_counter(
    conn: sqlite3.Connection, fixed_clock: Any
) -> None:
    svc = _make_service(conn, fixed_clock, audit_log_retrieved=True)
    ids = await _seed_three(svc)

    result = await svc.retrieve(query="marker")
    returned_ids = {r.id for r in (*result.do, *result.dont, *result.neutral)}
    # All three seeded observations came back (one per bucket).
    assert returned_ids == set(ids)

    # One ``retrieved`` audit row per returned observation.
    retrieved_rows = conn.execute(
        "SELECT entity_id, detail FROM audit_log WHERE action = 'retrieved'"
    ).fetchall()
    assert len(retrieved_rows) == len(returned_ids)
    audited_ids = {r["entity_id"] for r in retrieved_rows}
    assert audited_ids == returned_ids

    # Each detail payload carries outcome + final_score + bucket.
    for row in retrieved_rows:
        detail = json.loads(row["detail"])
        assert "outcome" in detail
        assert "final_score" in detail
        assert detail["bucket"] in {"do", "dont", "neutral"}

    # retrieved_count bumped to 1 on every returned observation.
    for obs_id in returned_ids:
        row = conn.execute(
            "SELECT retrieved_count, last_retrieved FROM observations WHERE id = ?",
            (obs_id,),
        ).fetchone()
        assert row["retrieved_count"] == 1
        assert row["last_retrieved"] is not None


async def test_retrieve_with_audit_off_skips_rows_and_counter(
    conn: sqlite3.Connection, fixed_clock: Any
) -> None:
    # First do one retrieve WITH audit so we have a baseline retrieved_count.
    svc_on = _make_service(conn, fixed_clock, audit_log_retrieved=True)
    ids = await _seed_three(svc_on)
    await svc_on.retrieve(query="marker")

    baseline_counts = {
        obs_id: conn.execute(
            "SELECT retrieved_count FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()["retrieved_count"]
        for obs_id in ids
    }
    baseline_audit = conn.execute(
        "SELECT COUNT(*) AS c FROM audit_log WHERE action = 'retrieved'"
    ).fetchone()["c"]

    # New service instance with the flag flipped off.
    svc_off = _make_service(conn, fixed_clock, audit_log_retrieved=False)
    await svc_off.retrieve(query="marker")

    # No new ``retrieved`` audit rows.
    new_audit = conn.execute(
        "SELECT COUNT(*) AS c FROM audit_log WHERE action = 'retrieved'"
    ).fetchone()["c"]
    assert new_audit == baseline_audit

    # retrieved_count unchanged for every observation.
    for obs_id in ids:
        current = conn.execute(
            "SELECT retrieved_count FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()["retrieved_count"]
        assert current == baseline_counts[obs_id]


async def test_audit_log_retrieved_flag_defaults_to_config(
    conn: sqlite3.Connection,
    fixed_clock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the kwarg is omitted the service reads the env-driven config."""
    monkeypatch.setenv("AUDIT_LOG_RETRIEVED", "false")
    svc = ObservationService(
        conn,
        _StubEmbedder(),
        clock=fixed_clock,
        project_resolver=lambda: "test-project",
        scope_resolver=lambda: None,
        session_id="sess-phase10",
        episodes=EpisodeService(conn),
    )
    ids = await _seed_three(svc)
    await svc.retrieve(query="marker")

    # Flag is false ⇒ no retrieval audit rows or counter bumps.
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM audit_log WHERE action = 'retrieved'"
        ).fetchone()["c"]
        == 0
    )
    for obs_id in ids:
        row = conn.execute(
            "SELECT retrieved_count FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
        assert row["retrieved_count"] == 0
