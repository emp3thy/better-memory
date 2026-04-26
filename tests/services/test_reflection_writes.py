"""Tests for ReflectionService (UI write actions: confirm / retire / update_text)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.reflection import ReflectionService


@pytest.fixture
def conn(tmp_memory_db: Path):
    c = connect(tmp_memory_db)
    apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def fixed_clock():
    fixed = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


def _seed_reflection(conn, reflection_id: str, status: str = "pending_review") -> None:
    conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, phase, polarity, use_cases, hints, "
        "confidence, status, created_at, updated_at) "
        "VALUES (?, ?, 'proj-a', 'general', 'do', 'old uc', 'old h', "
        "0.7, ?, '2026-04-25T00:00:00+00:00', '2026-04-25T00:00:00+00:00')",
        (reflection_id, f"title-{reflection_id}", status),
    )
    conn.commit()


class TestConfirm:
    def test_confirms_pending_review(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="pending_review")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.confirm(reflection_id="r1")

        row = conn.execute(
            "SELECT status, updated_at FROM reflections WHERE id = ?",
            ("r1",),
        ).fetchone()
        assert row["status"] == "confirmed"
        assert row["updated_at"] == "2026-04-26T12:00:00+00:00"

    def test_confirm_is_idempotent_on_already_confirmed(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="confirmed")
        svc = ReflectionService(conn, clock=fixed_clock)

        # No exception; status stays confirmed; updated_at NOT bumped (no-op).
        svc.confirm(reflection_id="r1")

        row = conn.execute(
            "SELECT status, updated_at FROM reflections WHERE id = ?",
            ("r1",),
        ).fetchone()
        assert row["status"] == "confirmed"
        assert row["updated_at"] == "2026-04-25T00:00:00+00:00"

    def test_raises_when_reflection_does_not_exist(self, conn, fixed_clock):
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Reflection not found"):
            svc.confirm(reflection_id="nope")

    def test_raises_when_retired(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="retired")
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Cannot confirm reflection in status 'retired'"):
            svc.confirm(reflection_id="r1")

    def test_raises_when_superseded(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="superseded")
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Cannot confirm reflection in status 'superseded'"):
            svc.confirm(reflection_id="r1")


class TestRetire:
    def test_retires_pending_review(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="pending_review")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.retire(reflection_id="r1")

        row = conn.execute(
            "SELECT status, updated_at FROM reflections WHERE id = ?",
            ("r1",),
        ).fetchone()
        assert row["status"] == "retired"
        assert row["updated_at"] == "2026-04-26T12:00:00+00:00"

    def test_retires_confirmed(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="confirmed")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.retire(reflection_id="r1")

        row = conn.execute(
            "SELECT status FROM reflections WHERE id = ?", ("r1",)
        ).fetchone()
        assert row["status"] == "retired"

    def test_retire_is_idempotent_on_already_retired(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="retired")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.retire(reflection_id="r1")  # no-op, no exception

        row = conn.execute(
            "SELECT status, updated_at FROM reflections WHERE id = ?",
            ("r1",),
        ).fetchone()
        assert row["status"] == "retired"
        assert row["updated_at"] == "2026-04-25T00:00:00+00:00"

    def test_raises_when_reflection_does_not_exist(self, conn, fixed_clock):
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Reflection not found"):
            svc.retire(reflection_id="nope")

    def test_raises_when_superseded(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="superseded")
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Cannot retire reflection in status 'superseded'"):
            svc.retire(reflection_id="r1")


class TestUpdateText:
    def test_updates_use_cases_and_hints(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="pending_review")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.update_text(
            reflection_id="r1", use_cases="new uc", hints="new h"
        )

        row = conn.execute(
            "SELECT use_cases, hints, updated_at FROM reflections WHERE id = ?",
            ("r1",),
        ).fetchone()
        assert row["use_cases"] == "new uc"
        # Hints stored as JSON-encoded list[str] to match synthesis contract.
        assert row["hints"] == '["new h"]'
        assert row["updated_at"] == "2026-04-26T12:00:00+00:00"

    def test_works_on_confirmed(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="confirmed")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.update_text(reflection_id="r1", use_cases="new uc", hints="new h")

        row = conn.execute(
            "SELECT use_cases, hints FROM reflections WHERE id = ?", ("r1",)
        ).fetchone()
        assert row["use_cases"] == "new uc"
        assert row["hints"] == '["new h"]'

    def test_raises_when_reflection_does_not_exist(self, conn, fixed_clock):
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Reflection not found"):
            svc.update_text(reflection_id="nope", use_cases="x", hints="y")

    def test_raises_when_retired(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="retired")
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="Cannot edit reflection in status 'retired'"):
            svc.update_text(reflection_id="r1", use_cases="x", hints="y")

    def test_raises_when_use_cases_empty(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="pending_review")
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="use_cases must not be empty"):
            svc.update_text(reflection_id="r1", use_cases="   ", hints="y")

    def test_raises_when_hints_empty(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="pending_review")
        svc = ReflectionService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="hints must not be empty"):
            svc.update_text(reflection_id="r1", use_cases="x", hints="")

    def test_splits_hints_on_newlines_and_drops_empties(self, conn, fixed_clock):
        _seed_reflection(conn, "r1", status="pending_review")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.update_text(
            reflection_id="r1",
            use_cases="uc",
            hints="first hint\n\nsecond hint\n   \nthird hint",
        )

        row = conn.execute(
            "SELECT hints FROM reflections WHERE id = ?", ("r1",)
        ).fetchone()
        # Empty / whitespace-only lines are dropped; non-empty lines are stripped.
        assert row["hints"] == '["first hint", "second hint", "third hint"]'

    def test_hints_round_trip_through_synthesis_read_path(
        self, conn, fixed_clock
    ):
        """Regression: UI edit must NOT corrupt the JSON contract used by
        ReflectionSynthesisService.retrieve_reflections / _apply_augment."""
        import json

        _seed_reflection(conn, "r1", status="pending_review")
        svc = ReflectionService(conn, clock=fixed_clock)

        svc.update_text(
            reflection_id="r1",
            use_cases="uc",
            hints="hint a\nhint b",
        )

        row = conn.execute(
            "SELECT hints FROM reflections WHERE id = ?", ("r1",)
        ).fetchone()
        # Must be valid JSON list[str] — synthesis service round-trips
        # this through json.loads at retrieve_reflections / _apply_augment.
        decoded = json.loads(row["hints"])
        assert decoded == ["hint a", "hint b"]
