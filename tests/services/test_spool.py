"""Tests for :class:`better_memory.services.spool.SpoolService`.

The spool service consumes hook-event JSON files from a spool directory,
inserts corresponding rows into the ``hook_events`` table, and either deletes
the source files on success or moves malformed files to a ``.quarantine/``
subdirectory.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.spool import DrainReport, SpoolService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_memory_db: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_memory_db)
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


@pytest.fixture
def tmp_spool(tmp_path: Path) -> Path:
    spool = tmp_path / "spool"
    spool.mkdir()
    return spool


def _write_event(
    spool_dir: Path,
    *,
    name: str,
    event_type: str = "tool_use",
    tool: str | None = "Edit",
    file: str | None = "foo.py",
    content_snippet: str | None = "hello world",
    cwd: str | None = "/some/where",
    session_id: str | None = "sess-1",
    timestamp: str = "2026-04-18T12:00:00Z",
    skip_fields: tuple[str, ...] = (),
) -> Path:
    """Write a spool event JSON file. Any field in ``skip_fields`` is omitted."""
    payload: dict[str, object] = {
        "event_type": event_type,
        "tool": tool,
        "file": file,
        "content_snippet": content_snippet,
        "cwd": cwd,
        "session_id": session_id,
        "timestamp": timestamp,
    }
    for key in skip_fields:
        payload.pop(key, None)
    path = spool_dir / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Empty / idempotency cases
# ---------------------------------------------------------------------------


def test_drain_empty_dir_returns_zero_report(
    conn: sqlite3.Connection, tmp_spool: Path
) -> None:
    service = SpoolService(conn, spool_dir=tmp_spool)
    report = service.drain()
    assert report == DrainReport(drained=0, quarantined=0)


def test_drain_creates_spool_dir_if_missing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    missing = tmp_path / "does-not-exist-yet"
    service = SpoolService(conn, spool_dir=missing)
    report = service.drain()
    assert missing.exists()
    assert (missing / ".quarantine").exists()
    assert report == DrainReport(drained=0, quarantined=0)


def test_drain_twice_is_idempotent(
    conn: sqlite3.Connection, tmp_spool: Path
) -> None:
    service = SpoolService(conn, spool_dir=tmp_spool)
    _write_event(tmp_spool, name="2026-04-18T12-00-00Z_Edit_aaa.json")
    first = service.drain()
    second = service.drain()
    assert first == DrainReport(drained=1, quarantined=0)
    assert second == DrainReport(drained=0, quarantined=0)
    rows = conn.execute("SELECT COUNT(*) AS c FROM hook_events").fetchone()
    assert rows["c"] == 1


# ---------------------------------------------------------------------------
# Happy path: valid files
# ---------------------------------------------------------------------------


def test_drain_three_valid_files_inserts_rows_and_deletes_files(
    conn: sqlite3.Connection, tmp_spool: Path
) -> None:
    _write_event(
        tmp_spool, name="2026-04-18T12-00-00Z_Edit_a.json", timestamp="2026-04-18T12:00:00Z"
    )
    _write_event(
        tmp_spool, name="2026-04-18T12-00-01Z_Bash_b.json", timestamp="2026-04-18T12:00:01Z",
        tool="Bash",
    )
    _write_event(
        tmp_spool, name="2026-04-18T12-00-02Z_Write_c.json", timestamp="2026-04-18T12:00:02Z",
        tool="Write",
    )

    service = SpoolService(conn, spool_dir=tmp_spool)
    report = service.drain()

    assert report == DrainReport(drained=3, quarantined=0)
    # All original JSON files are gone from the top level.
    remaining = list(tmp_spool.glob("*.json"))
    assert remaining == []

    rows = conn.execute(
        "SELECT event_type, tool, file, content_snippet, cwd, session_id, event_timestamp "
        "FROM hook_events ORDER BY event_timestamp"
    ).fetchall()
    assert len(rows) == 3
    assert [r["tool"] for r in rows] == ["Edit", "Bash", "Write"]
    assert rows[0]["event_type"] == "tool_use"
    assert rows[0]["file"] == "foo.py"
    assert rows[0]["content_snippet"] == "hello world"
    assert rows[0]["cwd"] == "/some/where"
    assert rows[0]["session_id"] == "sess-1"
    assert rows[0]["event_timestamp"] == "2026-04-18T12:00:00Z"


def test_drain_processes_files_in_filename_order(
    conn: sqlite3.Connection, tmp_spool: Path
) -> None:
    # Names chosen so lexicographic sort is deterministic and meaningful.
    _write_event(
        tmp_spool, name="2026-04-18T12-00-02Z_Edit_c.json",
        timestamp="c-ts", tool="C",
    )
    _write_event(
        tmp_spool, name="2026-04-18T12-00-00Z_Edit_a.json",
        timestamp="a-ts", tool="A",
    )
    _write_event(
        tmp_spool, name="2026-04-18T12-00-01Z_Edit_b.json",
        timestamp="b-ts", tool="B",
    )

    service = SpoolService(conn, spool_dir=tmp_spool)
    report = service.drain()

    assert report == DrainReport(drained=3, quarantined=0)

    # drained_at defaults to CURRENT_TIMESTAMP; insert order is what we assert
    # via rowid ordering (which is monotonic for INSERTs in a single connection).
    rows = conn.execute(
        "SELECT tool FROM hook_events ORDER BY rowid"
    ).fetchall()
    assert [r["tool"] for r in rows] == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# Error paths: quarantine
# ---------------------------------------------------------------------------


def test_drain_malformed_json_is_quarantined(
    conn: sqlite3.Connection, tmp_spool: Path
) -> None:
    bad = tmp_spool / "2026-04-18T12-00-00Z_Edit_bad.json"
    bad.write_text("{not valid json", encoding="utf-8")

    service = SpoolService(conn, spool_dir=tmp_spool)
    report = service.drain()

    assert report == DrainReport(drained=0, quarantined=1)
    assert not bad.exists()
    quarantined = tmp_spool / ".quarantine" / bad.name
    assert quarantined.exists()

    count = conn.execute("SELECT COUNT(*) AS c FROM hook_events").fetchone()["c"]
    assert count == 0


def test_drain_missing_event_type_is_quarantined(
    conn: sqlite3.Connection, tmp_spool: Path
) -> None:
    _write_event(
        tmp_spool,
        name="2026-04-18T12-00-00Z_Edit_missing.json",
        skip_fields=("event_type",),
    )
    service = SpoolService(conn, spool_dir=tmp_spool)
    report = service.drain()

    assert report == DrainReport(drained=0, quarantined=1)
    remaining = list(tmp_spool.glob("*.json"))
    assert remaining == []
    quarantined = list((tmp_spool / ".quarantine").glob("*.json"))
    assert len(quarantined) == 1


def test_drain_missing_timestamp_is_quarantined(
    conn: sqlite3.Connection, tmp_spool: Path
) -> None:
    _write_event(
        tmp_spool,
        name="2026-04-18T12-00-00Z_Edit_no-ts.json",
        skip_fields=("timestamp",),
    )
    service = SpoolService(conn, spool_dir=tmp_spool)
    report = service.drain()
    assert report == DrainReport(drained=0, quarantined=1)


def test_drain_mixed_valid_and_malformed(
    conn: sqlite3.Connection, tmp_spool: Path
) -> None:
    _write_event(
        tmp_spool, name="2026-04-18T12-00-00Z_Edit_ok1.json",
        timestamp="2026-04-18T12:00:00Z"
    )
    _write_event(
        tmp_spool, name="2026-04-18T12-00-01Z_Edit_ok2.json",
        timestamp="2026-04-18T12:00:01Z"
    )
    bad = tmp_spool / "2026-04-18T12-00-02Z_Edit_bad.json"
    bad.write_text("not-json-at-all", encoding="utf-8")

    service = SpoolService(conn, spool_dir=tmp_spool)
    report = service.drain()

    assert report == DrainReport(drained=2, quarantined=1)
    assert list(tmp_spool.glob("*.json")) == []
    quarantined = list((tmp_spool / ".quarantine").glob("*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].name == bad.name

    count = conn.execute("SELECT COUNT(*) AS c FROM hook_events").fetchone()["c"]
    assert count == 2


def test_drain_ignores_subdirectories(
    conn: sqlite3.Connection, tmp_spool: Path
) -> None:
    # Pre-seed a quarantine file that must NOT be re-processed.
    quarantine = tmp_spool / ".quarantine"
    quarantine.mkdir()
    preexisting = quarantine / "2026-04-18T12-00-00Z_Edit_old.json"
    preexisting.write_text("garbage", encoding="utf-8")

    _write_event(
        tmp_spool, name="2026-04-18T12-00-01Z_Edit_ok.json",
        timestamp="2026-04-18T12:00:01Z"
    )

    service = SpoolService(conn, spool_dir=tmp_spool)
    report = service.drain()

    assert report == DrainReport(drained=1, quarantined=0)
    # Pre-existing quarantine file is still in place, untouched.
    assert preexisting.exists()
    assert preexisting.read_text(encoding="utf-8") == "garbage"


def test_drain_commit_failure_leaves_files_in_spool_and_no_rows(
    conn: sqlite3.Connection, tmp_spool: Path
) -> None:
    # Simulate a commit failure (e.g. disk full, lock held). The invariant:
    # if commit() fails, spool files MUST remain on disk so a subsequent
    # drain can retry, and the hook_events table MUST have 0 rows because
    # the transaction was rolled back / never applied.
    _write_event(
        tmp_spool,
        name="2026-04-18T12-00-00Z_Edit_a.json",
        timestamp="2026-04-18T12:00:00Z",
    )
    _write_event(
        tmp_spool,
        name="2026-04-18T12-00-01Z_Edit_b.json",
        timestamp="2026-04-18T12:00:01Z",
    )

    class _CommitBoom(sqlite3.OperationalError):
        pass

    class _ExplodingCommitConn:
        """Delegates everything to the real connection but raises on commit.

        ``sqlite3.Connection`` disallows attribute assignment, so we can't
        monkeypatch ``conn.commit`` directly. A thin proxy lets us swap in a
        failing commit while leaving ``execute`` and friends untouched.
        """

        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner

        def commit(self) -> None:
            raise _CommitBoom("simulated commit failure")

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    proxy = _ExplodingCommitConn(conn)
    service = SpoolService(proxy, spool_dir=tmp_spool)  # type: ignore[arg-type]
    with pytest.raises(_CommitBoom):
        service.drain()

    # Both source files must still be present at the top level (not unlinked,
    # not quarantined) so the next drain can retry them.
    remaining = sorted(p.name for p in tmp_spool.glob("*.json"))
    assert remaining == [
        "2026-04-18T12-00-00Z_Edit_a.json",
        "2026-04-18T12-00-01Z_Edit_b.json",
    ]
    quarantined = list((tmp_spool / ".quarantine").glob("*.json"))
    assert quarantined == []

    # Restore a working commit so we can read the table state. INSERTs issued
    # before the failed commit are part of the open transaction on THIS
    # connection, so issue a rollback to ensure visibility matches a
    # post-failure state from a fresh connection's perspective.
    conn.rollback()
    count = conn.execute("SELECT COUNT(*) AS c FROM hook_events").fetchone()["c"]
    assert count == 0


def test_drain_defaults_spool_dir_from_config(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BETTER_MEMORY_HOME", str(tmp_path / "bm"))
    service = SpoolService(conn)
    # No files yet; drain should create the derived spool dir and return a
    # zero report.
    report = service.drain()
    assert report == DrainReport(drained=0, quarantined=0)
    assert (tmp_path / "bm" / "spool").exists()


class TestDrainSessionStartEvents:
    """Phase 3: drain lazy-opens a background episode for session_start markers."""

    def test_drain_opens_background_episode_for_session_start(
        self, tmp_memory_db: Path, tmp_path: Path
    ) -> None:
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        from better_memory.services.episode import EpisodeService
        from better_memory.services.spool import SpoolService

        spool = tmp_path / "spool"
        spool.mkdir()
        # Write a session_start marker to the spool.
        marker = {
            "event_type": "session_start",
            "timestamp": "2026-04-21T10:00:00+00:00",
            "session_id": "sess-new",
            "cwd": "/some/project-xyz",
            "project": "project-xyz",
        }
        (spool / "2026-04-21_session_start_abc.json").write_text(
            __import__("json").dumps(marker), encoding="utf-8"
        )

        conn = connect(tmp_memory_db)
        apply_migrations(conn)
        try:
            episodes = EpisodeService(conn)
            svc = SpoolService(conn, spool_dir=spool, episodes=episodes)
            report = svc.drain()
            assert report.drained == 1
            assert report.quarantined == 0

            # Episode should now exist bound to the session.
            active = episodes.active_episode("sess-new")
            assert active is not None
            assert active.project == "project-xyz"
            assert active.goal is None  # background
        finally:
            conn.close()

    def test_drain_skips_session_start_when_episode_already_active(
        self, tmp_memory_db: Path, tmp_path: Path
    ) -> None:
        """Idempotent: drain doesn't create a second episode for the same session."""
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        from better_memory.services.episode import EpisodeService
        from better_memory.services.spool import SpoolService

        spool = tmp_path / "spool"
        spool.mkdir()
        marker = {
            "event_type": "session_start",
            "timestamp": "2026-04-21T10:00:00+00:00",
            "session_id": "sess-existing",
            "cwd": "/some/p",
            "project": "p",
        }
        (spool / "m.json").write_text(
            __import__("json").dumps(marker), encoding="utf-8"
        )

        conn = connect(tmp_memory_db)
        apply_migrations(conn)
        try:
            episodes = EpisodeService(conn)
            # Pre-open an episode for this session (simulates lazy-open
            # having already happened via ObservationService.create).
            pre_existing = episodes.open_background(
                session_id="sess-existing", project="p"
            )

            svc = SpoolService(conn, spool_dir=spool, episodes=episodes)
            report = svc.drain()
            assert report.drained == 1

            # Only one episode total.
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM episodes"
            ).fetchone()["c"]
            assert count == 1
            # And it's still the pre-existing one.
            active = episodes.active_episode("sess-existing")
            assert active is not None
            assert active.id == pre_existing
        finally:
            conn.close()

    def test_drain_without_episodes_dependency_still_works(
        self, tmp_memory_db: Path, tmp_path: Path
    ) -> None:
        """Back-compat: SpoolService without episodes kwarg drains as before."""
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        from better_memory.services.spool import SpoolService

        spool = tmp_path / "spool"
        spool.mkdir()
        marker = {
            "event_type": "session_start",
            "timestamp": "2026-04-21T10:00:00+00:00",
            "session_id": "sess-x",
            "cwd": "/p",
            "project": "p",
        }
        (spool / "m.json").write_text(
            __import__("json").dumps(marker), encoding="utf-8"
        )

        conn = connect(tmp_memory_db)
        apply_migrations(conn)
        try:
            svc = SpoolService(conn, spool_dir=spool)  # no episodes kwarg
            report = svc.drain()
            assert report.drained == 1
            # hook_events row exists as usual.
            rows = conn.execute(
                "SELECT event_type FROM hook_events"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["event_type"] == "session_start"
            # No episodes created (no episodes service to call).
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM episodes"
            ).fetchone()["c"]
            assert count == 0
        finally:
            conn.close()

    def test_drain_session_close_does_not_touch_episodes(
        self, tmp_memory_db: Path, tmp_path: Path
    ) -> None:
        """session_close still drains as before, does not side-effect on episodes."""
        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        from better_memory.services.episode import EpisodeService
        from better_memory.services.spool import SpoolService

        spool = tmp_path / "spool"
        spool.mkdir()
        marker = {
            "event_type": "session_end",
            "timestamp": "2026-04-21T10:00:00+00:00",
            "session_id": "sess-y",
            "cwd": "/p",
        }
        (spool / "m.json").write_text(
            __import__("json").dumps(marker), encoding="utf-8"
        )

        conn = connect(tmp_memory_db)
        apply_migrations(conn)
        try:
            episodes = EpisodeService(conn)
            svc = SpoolService(conn, spool_dir=spool, episodes=episodes)
            report = svc.drain()
            assert report.drained == 1
            # No episodes created or modified.
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM episodes"
            ).fetchone()["c"]
            assert count == 0
        finally:
            conn.close()


class TestDrainCommitCloseEvents:
    """Phase 4: drain closes the active episode for commit_close markers."""

    def test_drain_closes_active_episode_for_commit_close(
        self, tmp_memory_db: Path, tmp_path: Path
    ) -> None:
        import json as _json

        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        from better_memory.services.episode import EpisodeService
        from better_memory.services.spool import SpoolService

        spool = tmp_path / "spool"
        spool.mkdir()

        conn = connect(tmp_memory_db)
        apply_migrations(conn)
        try:
            episodes = EpisodeService(conn)
            # Pre-open a foreground episode for the session.
            ep_id = episodes.start_foreground(
                session_id="sess-close", project="p", goal="ship"
            )

            marker = {
                "event_type": "commit_close",
                "timestamp": "2026-04-22T10:00:00+00:00",
                "session_id": "sess-close",
                "cwd": "/p",
                "project": "p",
                "commit_sha": "deadbeef",
            }
            (spool / "m.json").write_text(
                _json.dumps(marker), encoding="utf-8"
            )

            svc = SpoolService(conn, spool_dir=spool, episodes=episodes)
            report = svc.drain()
            assert report.drained == 1

            row = conn.execute(
                "SELECT ended_at, close_reason, outcome "
                "FROM episodes WHERE id = ?",
                (ep_id,),
            ).fetchone()
            assert row["ended_at"] is not None
            assert row["close_reason"] == "goal_complete"
            assert row["outcome"] == "success"
        finally:
            conn.close()

    def test_drain_commit_close_without_active_episode_is_noop(
        self, tmp_memory_db: Path, tmp_path: Path
    ) -> None:
        """No active episode → close_active raises ValueError → drain swallows it."""
        import json as _json

        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        from better_memory.services.episode import EpisodeService
        from better_memory.services.spool import SpoolService

        spool = tmp_path / "spool"
        spool.mkdir()

        conn = connect(tmp_memory_db)
        apply_migrations(conn)
        try:
            episodes = EpisodeService(conn)
            # No episode open for sess-orphan.

            marker = {
                "event_type": "commit_close",
                "timestamp": "2026-04-22T10:00:00+00:00",
                "session_id": "sess-orphan",
                "cwd": "/p",
                "project": "p",
                "commit_sha": "deadbeef",
            }
            (spool / "m.json").write_text(
                _json.dumps(marker), encoding="utf-8"
            )

            svc = SpoolService(conn, spool_dir=spool, episodes=episodes)
            report = svc.drain()
            # Drain still succeeds — the hook_events row was inserted,
            # the side-effect was a no-op.
            assert report.drained == 1

            # No episodes table rows.
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM episodes"
            ).fetchone()["c"]
            assert count == 0
        finally:
            conn.close()

    def test_drain_commit_close_closes_background_episode_too(
        self, tmp_memory_db: Path, tmp_path: Path
    ) -> None:
        """Closing a background (unhardened) episode is valid — matches close_active semantics."""
        import json as _json

        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        from better_memory.services.episode import EpisodeService
        from better_memory.services.spool import SpoolService

        spool = tmp_path / "spool"
        spool.mkdir()

        conn = connect(tmp_memory_db)
        apply_migrations(conn)
        try:
            episodes = EpisodeService(conn)
            bg_id = episodes.open_background(
                session_id="sess-bg", project="p"
            )

            marker = {
                "event_type": "commit_close",
                "timestamp": "2026-04-22T10:00:00+00:00",
                "session_id": "sess-bg",
                "cwd": "/p",
                "project": "p",
                "commit_sha": "deadbeef",
            }
            (spool / "m.json").write_text(
                _json.dumps(marker), encoding="utf-8"
            )

            svc = SpoolService(conn, spool_dir=spool, episodes=episodes)
            svc.drain()

            row = conn.execute(
                "SELECT ended_at, outcome, close_reason "
                "FROM episodes WHERE id = ?",
                (bg_id,),
            ).fetchone()
            assert row["ended_at"] is not None
            assert row["outcome"] == "success"
            assert row["close_reason"] == "goal_complete"
        finally:
            conn.close()

    def test_drain_commit_close_without_episodes_dependency_is_noop(
        self, tmp_memory_db: Path, tmp_path: Path
    ) -> None:
        """Back-compat: SpoolService without episodes kwarg drains hook_events and does not attempt close."""
        import json as _json

        from better_memory.db.connection import connect
        from better_memory.db.schema import apply_migrations
        from better_memory.services.spool import SpoolService

        spool = tmp_path / "spool"
        spool.mkdir()

        conn = connect(tmp_memory_db)
        apply_migrations(conn)
        try:
            marker = {
                "event_type": "commit_close",
                "timestamp": "2026-04-22T10:00:00+00:00",
                "session_id": "sess-nc",
                "cwd": "/p",
                "project": "p",
                "commit_sha": "deadbeef",
            }
            (spool / "m.json").write_text(
                _json.dumps(marker), encoding="utf-8"
            )

            svc = SpoolService(conn, spool_dir=spool)  # no episodes
            report = svc.drain()
            assert report.drained == 1

            rows = conn.execute(
                "SELECT event_type FROM hook_events"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["event_type"] == "commit_close"
        finally:
            conn.close()
