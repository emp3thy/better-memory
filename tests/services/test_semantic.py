"""Tests for SemanticMemoryService."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


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
    fixed = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


class TestCreate:
    def test_create_with_default_scope(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="prefer terse replies", project="p1")
        assert memory_id  # non-empty id returned
        row = conn.execute(
            "SELECT content, project, scope, created_at, updated_at "
            "FROM semantic_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert row["content"] == "prefer terse replies"
        assert row["project"] == "p1"
        assert row["scope"] == "project"
        assert row["created_at"] == "2026-05-04T12:00:00+00:00"
        assert row["updated_at"] == "2026-05-04T12:00:00+00:00"

    def test_create_with_general_scope(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(
            content="always assign per-step confidence",
            project="any",
            scope="general",
        )
        row = conn.execute(
            "SELECT scope FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["scope"] == "general"

    def test_create_rejects_invalid_scope(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="scope"):
            svc.create(content="rule", project="p1", scope="invalid")

    def test_create_rejects_empty_content(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="content"):
            svc.create(content="   ", project="p1")
        with pytest.raises(ValueError, match="content"):
            svc.create(content="", project="p1")


class TestUpdateText:
    def test_update_changes_content_and_bumps_updated_at(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="old text", project="p1")

        # Advance the clock to a later instant for the update.
        from datetime import timedelta
        later = (fixed_clock() + timedelta(hours=2)).isoformat()
        svc._clock = lambda: fixed_clock() + timedelta(hours=2)

        svc.update_text(id=memory_id, content="new text")
        row = conn.execute(
            "SELECT content, created_at, updated_at "
            "FROM semantic_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert row["content"] == "new text"
        assert row["created_at"] == "2026-05-04T12:00:00+00:00"  # unchanged
        assert row["updated_at"] == later

    def test_update_raises_on_missing_id(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="not found"):
            svc.update_text(id="nope", content="x")

    def test_update_missing_id_rolls_back_implicit_transaction(
        self, conn, fixed_clock,
    ):
        """Regression for BugBot finding on PR #34: sqlite3 with default
        isolation_level opens an implicit BEGIN before the UPDATE; raising
        ValueError without rollback would strand the WAL write lock for
        any caller sharing this connection. Mirror of the
        ObservationService.set_outcome pattern.
        """
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="not found"):
            svc.update_text(id="nope", content="x")
        # After the failed call, the connection must not be in a
        # transaction — otherwise the WAL write lock is still held.
        assert conn.in_transaction is False

    def test_update_rejects_empty_content(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="original", project="p1")
        with pytest.raises(ValueError, match="content"):
            svc.update_text(id=memory_id, content="   ")
        # Original content unchanged.
        row = conn.execute(
            "SELECT content FROM semantic_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert row["content"] == "original"


class TestDelete:
    def test_delete_removes_existing_row(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="x", project="p1")
        svc.delete(id=memory_id)
        row = conn.execute(
            "SELECT 1 FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row is None

    def test_delete_is_idempotent_on_missing_id(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        # No exception when id doesn't exist.
        svc.delete(id="ghost")


class TestListForProject:
    def test_returns_empty_list_when_no_rows(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        assert svc.list_for_project(project="p1") == []

    def test_returns_only_project_rows_when_general_absent(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        # Two memories in p1, one in p2 (project-scope).
        svc.create(content="p1 rule", project="p1")
        svc.create(content="p1 other", project="p1")
        svc.create(content="p2 rule", project="p2")
        memories = svc.list_for_project(project="p1")
        assert len(memories) == 2
        assert {m.project for m in memories} == {"p1"}
        assert {m.content for m in memories} == {"p1 rule", "p1 other"}

    def test_includes_general_from_other_projects(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.create(content="p1-only rule", project="p1")
        svc.create(content="cross-project rule", project="p2", scope="general")
        memories = svc.list_for_project(project="p1")
        assert len(memories) == 2
        contents = {m.content for m in memories}
        assert "p1-only rule" in contents
        assert "cross-project rule" in contents
        # Confirm scope flag preserved on the read model.
        general_match = next(m for m in memories if m.scope == "general")
        assert general_match.project == "p2"

    def test_excludes_other_projects_project_scoped_memories(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.create(content="hidden p2 rule", project="p2")  # scope='project'
        memories = svc.list_for_project(project="p1")
        assert memories == []

    def test_orders_newest_first(self, conn, fixed_clock):
        from datetime import timedelta
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        # Three memories at t, t+1h, t+2h.
        svc._clock = lambda: fixed_clock()
        first_id = svc.create(content="oldest", project="p1")
        svc._clock = lambda: fixed_clock() + timedelta(hours=1)
        second_id = svc.create(content="middle", project="p1")
        svc._clock = lambda: fixed_clock() + timedelta(hours=2)
        third_id = svc.create(content="newest", project="p1")

        memories = svc.list_for_project(project="p1")
        assert [m.id for m in memories] == [third_id, second_id, first_id]


class TestListForProjectFilters:
    def test_scope_filter_project_only_excludes_general(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.create(content="p1 project rule", project="p1", scope="project")
        svc.create(content="p1 general rule", project="p1", scope="general")
        svc.create(content="p2 general rule", project="p2", scope="general")
        memories = svc.list_for_project(project="p1", scope_filter="project")
        assert {m.content for m in memories} == {"p1 project rule"}

    def test_scope_filter_general_only_returns_all_general_rows(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.create(content="p1 project rule", project="p1", scope="project")
        svc.create(content="p1 general rule", project="p1", scope="general")
        svc.create(content="p2 general rule", project="p2", scope="general")
        memories = svc.list_for_project(project="p1", scope_filter="general")
        assert {m.content for m in memories} == {
            "p1 general rule", "p2 general rule",
        }

    def test_scope_filter_none_matches_default_behavior(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.create(content="p1 project rule", project="p1", scope="project")
        svc.create(content="p2 general rule", project="p2", scope="general")
        svc.create(content="p2 project rule", project="p2", scope="project")
        default = svc.list_for_project(project="p1")
        explicit_none = svc.list_for_project(project="p1", scope_filter=None)
        assert {m.content for m in default} == {
            "p1 project rule", "p2 general rule",
        }
        assert {m.content for m in explicit_none} == {m.content for m in default}

    def test_search_substring_case_insensitive(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.create(content="Prefer terse replies", project="p1")
        svc.create(content="use ruff for linting", project="p1")
        svc.create(content="general fact", project="p1", scope="general")
        memories = svc.list_for_project(project="p1", search="RUFF")
        assert {m.content for m in memories} == {"use ruff for linting"}

    def test_search_escapes_like_wildcards(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.create(content="literal 50% off promo", project="p1")
        svc.create(content="no percent here", project="p1")
        # The '%' must match literally, not as a wildcard.
        memories = svc.list_for_project(project="p1", search="50%")
        assert {m.content for m in memories} == {"literal 50% off promo"}

    def test_search_combines_with_scope_filter(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.create(content="ruff project rule", project="p1", scope="project")
        svc.create(content="ruff general rule", project="p1", scope="general")
        svc.create(content="other project rule", project="p1", scope="project")
        memories = svc.list_for_project(
            project="p1", scope_filter="project", search="ruff",
        )
        assert {m.content for m in memories} == {"ruff project rule"}

    def test_empty_search_string_treated_as_no_filter(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        svc.create(content="rule one", project="p1")
        svc.create(content="rule two", project="p1")
        # Empty string should not constrain results — treated like None.
        memories = svc.list_for_project(project="p1", search="")
        assert len(memories) == 2


class TestSetScope:
    def test_set_scope_changes_value(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="rule", project="p1")
        svc.set_scope(id=memory_id, scope="general")
        row = conn.execute(
            "SELECT scope FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["scope"] == "general"

    def test_set_scope_round_trip(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="rule", project="p1", scope="general")
        svc.set_scope(id=memory_id, scope="project")
        row = conn.execute(
            "SELECT scope FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["scope"] == "project"

    def test_set_scope_bumps_updated_at(self, conn, fixed_clock):
        from datetime import timedelta
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="rule", project="p1")
        svc._clock = lambda: fixed_clock() + timedelta(hours=1)
        svc.set_scope(id=memory_id, scope="general")
        row = conn.execute(
            "SELECT created_at, updated_at FROM semantic_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert row["updated_at"] != row["created_at"]

    def test_set_scope_rejects_invalid(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create(content="rule", project="p1")
        with pytest.raises(ValueError, match="scope"):
            svc.set_scope(id=memory_id, scope="invalid")

    def test_set_scope_raises_on_missing_id(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="not found"):
            svc.set_scope(id="nope", scope="general")

    def test_set_scope_missing_id_rolls_back_implicit_transaction(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="not found"):
            svc.set_scope(id="nope", scope="general")
        assert conn.in_transaction is False


class TestCreateFromObservation:
    def _seed_active_observation(self, conn, *, obs_id="o1", project="p1",
                                 content="bug found", episode_id=None):
        if episode_id is None:
            episode_id = "ep-default"
            conn.execute(
                "INSERT OR IGNORE INTO episodes (id, project, started_at) VALUES "
                "(?, ?, '2026-04-01T00:00:00+00:00')",
                (episode_id, project),
            )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, status, "
            "outcome, created_at, status_changed_at) VALUES "
            "(?, ?, ?, ?, 'active', 'success', "
            "'2026-05-04T12:00:00+00:00','2026-05-04T12:00:00+00:00')",
            (obs_id, content, project, episode_id),
        )
        conn.commit()

    def test_create_from_observation_happy_path(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        self._seed_active_observation(conn, obs_id="o1", content="rule text")
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create_from_observation(observation_id="o1")
        mem_row = conn.execute(
            "SELECT content, project, scope FROM semantic_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert mem_row["content"] == "rule text"
        assert mem_row["project"] == "p1"
        assert mem_row["scope"] == "project"
        obs_row = conn.execute(
            "SELECT status, status_changed_at FROM observations WHERE id = 'o1'"
        ).fetchone()
        assert obs_row["status"] == "consumed_without_reflection"
        assert obs_row["status_changed_at"] == "2026-05-04T12:00:00+00:00"

    def test_create_from_observation_with_general_scope(self, conn, fixed_clock):
        from better_memory.services.semantic import SemanticMemoryService
        self._seed_active_observation(conn, obs_id="o1")
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        memory_id = svc.create_from_observation(
            observation_id="o1", scope="general",
        )
        row = conn.execute(
            "SELECT scope FROM semantic_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row["scope"] == "general"

    def test_create_from_observation_rejects_invalid_scope(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        self._seed_active_observation(conn, obs_id="o1")
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="scope"):
            svc.create_from_observation(observation_id="o1", scope="invalid")
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'o1'"
        ).fetchone()
        assert row["status"] == "active"

    def test_create_from_observation_raises_on_missing_observation(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="observation not found"):
            svc.create_from_observation(observation_id="ghost")

    def test_create_from_observation_raises_on_already_consumed(
        self, conn, fixed_clock,
    ):
        from better_memory.services.semantic import SemanticMemoryService
        conn.execute(
            "INSERT OR IGNORE INTO episodes (id, project, started_at) VALUES "
            "('ep1', 'p1', '2026-04-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, status, "
            "outcome, created_at, status_changed_at) VALUES "
            "('o1','x','p1','ep1','consumed_into_reflection','success',"
            "'2026-05-04T12:00:00+00:00','2026-05-04T12:00:00+00:00')"
        )
        conn.commit()
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="not active"):
            svc.create_from_observation(observation_id="o1")
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'o1'"
        ).fetchone()
        assert row["status"] == "consumed_into_reflection"
        count = conn.execute(
            "SELECT COUNT(*) FROM semantic_memories"
        ).fetchone()[0]
        assert count == 0

    def test_create_from_observation_rejects_empty_observation_content(
        self, conn, fixed_clock,
    ):
        """Mirrors create() and update_text() guards against empty content.
        DB schema only enforces NOT NULL; whitespace-only content would
        otherwise silently produce a useless semantic memory."""
        from better_memory.services.semantic import SemanticMemoryService
        conn.execute(
            "INSERT OR IGNORE INTO episodes (id, project, started_at) VALUES "
            "('ep1', 'p1', '2026-04-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO observations (id, content, project, episode_id, status, "
            "outcome, created_at, status_changed_at) VALUES "
            "('o1','   ','p1','ep1','active','success',"
            "'2026-05-04T12:00:00+00:00','2026-05-04T12:00:00+00:00')"
        )
        conn.commit()
        svc = SemanticMemoryService(conn, clock=fixed_clock)
        with pytest.raises(ValueError, match="empty"):
            svc.create_from_observation(observation_id="o1")
        # Source observation unchanged.
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'o1'"
        ).fetchone()
        assert row["status"] == "active"
        # No semantic memory created.
        count = conn.execute(
            "SELECT COUNT(*) FROM semantic_memories"
        ).fetchone()[0]
        assert count == 0

    def test_create_from_observation_atomic_on_failure(
        self, conn, fixed_clock,
    ):
        from unittest.mock import MagicMock
        from better_memory.services.semantic import SemanticMemoryService
        self._seed_active_observation(conn, obs_id="o1")

        # Wrap the real connection so we can intercept a specific SQL statement.
        # sqlite3.Connection is a C type — its .execute slot is not patchable
        # via monkeypatch.setattr, so we wrap at the service level instead.
        original_execute = conn.execute

        class _FailingConn:
            """Thin proxy that raises on UPDATE observations to simulate a
            mid-promote crash, so we can verify the SAVEPOINT rolls back."""

            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *args, **kwargs):
                if "UPDATE observations" in sql:
                    raise RuntimeError("simulated failure mid-promote")
                return original_execute(sql, *args, **kwargs)

        svc = SemanticMemoryService(_FailingConn(), clock=fixed_clock)  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="simulated"):
            svc.create_from_observation(observation_id="o1")

        # Verify atomicity via the real connection (SAVEPOINT rolled back).
        count = conn.execute(
            "SELECT COUNT(*) FROM semantic_memories"
        ).fetchone()[0]
        assert count == 0
        row = conn.execute(
            "SELECT status FROM observations WHERE id = 'o1'"
        ).fetchone()
        assert row["status"] == "active"


class TestUsefulMisledFields:
    """list_for_project and the drawer detail dict expose the 4 rating counters."""

    def test_list_includes_useful_count_and_times_misled(self, conn):
        """Rows returned by list_for_project carry useful_count + times_misled."""
        from better_memory.services.semantic import SemanticMemoryService
        conn.execute(
            "INSERT INTO semantic_memories "
            "(id, content, project, scope, created_at, updated_at, "
            "useful_count, last_useful_at, times_misled, last_misled_at) "
            "VALUES ('m1', 'test rule', 'p1', 'project', "
            "'2026-05-04T12:00:00+00:00', '2026-05-04T12:00:00+00:00', "
            "3, '2026-05-10T10:00:00+00:00', 1, '2026-05-09T08:00:00+00:00')",
        )
        conn.commit()
        svc = SemanticMemoryService(conn)
        rows = svc.list_for_project(project="p1", track_exposure=False)
        assert len(rows) == 1
        row = rows[0]
        assert row.useful_count == 3
        assert row.times_misled == 1

    def test_list_last_at_fields_returned(self, conn):
        """last_useful_at and last_misled_at are returned from list_for_project."""
        from better_memory.services.semantic import SemanticMemoryService
        conn.execute(
            "INSERT INTO semantic_memories "
            "(id, content, project, scope, created_at, updated_at, "
            "useful_count, last_useful_at, times_misled, last_misled_at) "
            "VALUES ('m2', 'another rule', 'p1', 'project', "
            "'2026-05-04T12:00:00+00:00', '2026-05-04T12:00:00+00:00', "
            "2, '2026-05-11T09:00:00+00:00', 0, NULL)",
        )
        conn.commit()
        svc = SemanticMemoryService(conn)
        rows = svc.list_for_project(project="p1", track_exposure=False)
        assert len(rows) == 1
        row = rows[0]
        assert row.last_useful_at == "2026-05-11T09:00:00+00:00"
        assert row.last_misled_at is None

    def test_defaults_to_zero_counts(self, conn):
        """Rows created without explicit counters default to 0 / None."""
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        svc.create(content="new rule", project="p1")
        rows = svc.list_for_project(project="p1", track_exposure=False)
        assert len(rows) == 1
        row = rows[0]
        assert row.useful_count == 0
        assert row.times_misled == 0
        assert row.last_useful_at is None
        assert row.last_misled_at is None


class TestSemanticWilsonRanking:
    def test_hit_rate_beats_raw_count(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        a = svc.create(content="workhorse", project="p")
        b = svc.create(content="newcomer", project="p")
        conn.execute(
            "UPDATE semantic_memories SET useful_count=67, times_ignored=125 WHERE id=?", (a,))
        conn.execute(
            "UPDATE semantic_memories SET useful_count=3, times_ignored=1 WHERE id=?", (b,))
        conn.commit()
        ids = [m.id for m in svc.list_for_project(project="p", track_exposure=False)]
        assert ids == [b, a]

    def test_never_rated_sorts_by_recency_at_bottom(self, conn):
        from better_memory.services.semantic import SemanticMemoryService
        svc = SemanticMemoryService(conn)
        rated = svc.create(content="rated", project="p")
        conn.execute(
            "UPDATE semantic_memories SET useful_count=1, times_ignored=1 WHERE id=?", (rated,))
        conn.commit()
        unrated = svc.create(content="unrated", project="p")
        ids = [m.id for m in svc.list_for_project(project="p", track_exposure=False)]
        assert ids == [rated, unrated]


def test_semantic_service_get_returns_model_or_none(conn):
    from better_memory.services.semantic import SemanticMemory, SemanticMemoryService
    svc = SemanticMemoryService(conn)
    mid = svc.create(content="a rule", project="p", scope="general")
    got = svc.get(id=mid)
    assert isinstance(got, SemanticMemory)
    assert got.id == mid and got.content == "a rule" and got.scope == "general"
    assert svc.get(id="missing") is None
