"""Tests for the rating directive emission in session_close.py."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations


def _run_hook(env: dict[str, str], stdin_data: str = "") -> subprocess.CompletedProcess:
    """Run session_close.py as a subprocess (mirrors how the hook is invoked)."""
    return subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.session_close"],
        input=stdin_data,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=10,
    )


def _seed_unrated_exposure(db_path: Path, sid: str = "S1", source: str = "bootstrap"):
    c = connect(db_path)
    apply_migrations(c)
    c.execute(
        """INSERT INTO reflections
           (id, title, project, phase, polarity, use_cases, hints,
            confidence, created_at, updated_at)
           VALUES ('r1', 'My Title', 'p', 'general', 'do', 'uc', '[]', 0.5,
                   '2026-01-01', '2026-01-01')"""
    )
    c.execute(
        """INSERT INTO session_memory_exposure
           (session_id, memory_kind, memory_id, exposed_at, source)
           VALUES (?, 'reflection', 'r1', '2026-05-11T11:00:00+00:00', ?)""",
        (sid, source),
    )
    c.commit()
    c.close()


def _seed_semantic_exposure(db_path: Path, sid: str = "S1", source: str = "contextual"):
    """Seed a semantic-memory unrated exposure. Assumes migrations already
    applied (call after _seed_unrated_exposure, which applies them)."""
    c = connect(db_path)
    apply_migrations(c)
    c.execute(
        """INSERT INTO semantic_memories
           (id, content, project, scope, created_at, updated_at)
           VALUES ('s1', 'My Semantic Fact', 'p', 'project',
                   '2026-01-01', '2026-01-01')"""
    )
    c.execute(
        """INSERT INTO session_memory_exposure
           (session_id, memory_kind, memory_id, exposed_at, source)
           VALUES (?, 'semantic', 's1', '2026-05-11T12:00:00+00:00', ?)""",
        (sid, source),
    )
    c.commit()
    c.close()


class TestRatingDirectiveEmission:
    def test_non_empty_unrated_emits_decision_block(
        self, tmp_path, tmp_memory_db,
    ):
        _seed_unrated_exposure(tmp_memory_db, "S1")
        home = tmp_memory_db.parent
        env = {
            "BETTER_MEMORY_HOME": str(home),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        assert result.returncode == 0
        # stdout should contain a single JSON object with the documented shape.
        payload = json.loads(result.stdout)
        assert payload["decision"] == "block"
        assert isinstance(payload.get("reason"), str) and payload["reason"]
        hso = payload["hookSpecificOutput"]
        assert hso["hookEventName"] == "Stop"
        assert "additionalContext" in hso
        assert "RATE_MEMORIES" in hso["additionalContext"]
        assert "r1" in hso["additionalContext"]
        assert "My Title" in hso["additionalContext"]
        assert len(hso["additionalContext"]) <= 10_000
        # Block emitted — spool marker MUST NOT be written on this fire.
        # Claude Code re-fires Stop after the rating turn; the marker
        # lands on the final fire so downstream synthesis runs once,
        # AFTER ratings, not twice.
        spool = home / "spool"
        if spool.exists():
            markers = list(spool.glob("*_session_end_*.json"))
            assert markers == [], f"unexpected markers on block fire: {markers}"

    def test_multi_row_exposure_dedupes_in_directive(
        self, tmp_path, tmp_memory_db,
    ):
        """A memory with TWO unrated exposure rows (bootstrap + retrieve)
        must appear ONCE in the directive — apply_session_ratings rejects
        duplicate (kind, id) pairs."""
        c = connect(tmp_memory_db)
        apply_migrations(c)
        c.execute(
            """INSERT INTO reflections
               (id, title, project, phase, polarity, use_cases, hints,
                confidence, created_at, updated_at)
               VALUES ('r-dup', 'Dup Title', 'p', 'general', 'do',
                       'uc', '[]', 0.5, '2026-01-01', '2026-01-01')"""
        )
        # Two distinct exposed_at timestamps for the same (session, kind, id).
        for ts, src in [
            ("2026-05-11T10:00:00+00:00", "bootstrap"),
            ("2026-05-11T11:00:00+00:00", "retrieve"),
        ]:
            c.execute(
                """INSERT INTO session_memory_exposure
                   (session_id, memory_kind, memory_id, exposed_at, source)
                   VALUES ('S1', 'reflection', 'r-dup', ?, ?)""",
                (ts, src),
            )
        c.commit()
        c.close()

        env = {
            "BETTER_MEMORY_HOME": str(tmp_memory_db.parent),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        directive = payload["hookSpecificOutput"]["additionalContext"]
        # Memory id should appear exactly once in the reflection bucket.
        assert directive.count("- r-dup [") == 1
        # Total rating count in the reason should be 1, not 2.
        assert "1 pending" in payload["reason"]

    def test_empty_unrated_writes_marker_no_directive(
        self, tmp_path, tmp_memory_db,
    ):
        # Migrate but no unrated rows. The DB exists; the table is empty.
        c = connect(tmp_memory_db)
        apply_migrations(c)
        c.close()
        home = tmp_memory_db.parent
        spool = home / "spool"
        spool.mkdir(exist_ok=True)
        env = {
            "BETTER_MEMORY_HOME": str(home),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        assert result.returncode == 0
        # stdout should be empty (no directive) — the table exists but has no rows.
        assert result.stdout.strip() == ""
        markers = list(spool.glob("*_session_end_*.json"))
        assert len(markers) == 1

    def test_stop_hook_active_reentry_writes_marker_and_no_directive(
        self, tmp_path, tmp_memory_db,
    ):
        """Claude Code re-fires Stop after the LLM's rating turn with
        ``stop_hook_active=True`` in the stdin payload. The continuation
        turn itself can create fresh unrated exposure rows
        (memory.retrieve inserts source='retrieve' rows; a first
        PreToolUse inserts source='contextual' rows), so a stop hook
        without a re-entry guard would find those and block again
        forever, and the session_end marker would never be spooled.
        With the guard, the re-fire skips the block and writes the
        marker even though unrated rows still exist. (Bug: #100.)"""
        _seed_unrated_exposure(tmp_memory_db, "S1")
        home = tmp_memory_db.parent
        spool = home / "spool"
        spool.mkdir(exist_ok=True)
        env = {
            "BETTER_MEMORY_HOME": str(home),
            "CLAUDE_SESSION_ID": "S1",
        }
        # Payload with stop_hook_active=True mirrors what Claude Code
        # sends on Stop re-entry after a prior block returned.
        payload = json.dumps(
            {
                "session_id": "S1",
                "stop_hook_active": True,
                "hook_event_name": "Stop",
            }
        )
        result = _run_hook(env, stdin_data=payload)
        assert result.returncode == 0
        # No block directive on re-entry — the LLM has already had its
        # rating turn.
        assert result.stdout.strip() == ""
        # Marker MUST be written on the re-entry so downstream synthesis
        # runs — even though unrated rows still exist.
        markers = list(spool.glob("*_session_end_*.json"))
        assert len(markers) == 1

    def test_db_error_falls_back_to_marker(self, tmp_path):
        """If the DB doesn't exist, the hook still exits 0 and writes a marker."""
        home = tmp_path / "nonexistent"
        env = {
            "BETTER_MEMORY_HOME": str(home),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        assert result.returncode == 0
        # Marker should still be written even though DB is absent.
        markers = list((home / "spool").glob("*_session_end_*.json"))
        assert len(markers) == 1

    def test_directive_keeps_one_rule_line(self, tmp_path, tmp_memory_db):
        _seed_unrated_exposure(tmp_memory_db, "S1")
        env = {
            "BETTER_MEMORY_HOME": str(tmp_memory_db.parent),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        directive = payload["hookSpecificOutput"]["additionalContext"]
        assert "Evidence line first; none possible = `ignored`." in directive
        assert "rate-session-memories" in directive
        # Rules that now live only in the skill must NOT be restated.
        assert "Non-ignored ratings" not in directive
        assert "overlooked" not in directive

    def test_directive_shows_source_labels_without_counts_line(
        self, tmp_path, tmp_memory_db,
    ):
        _seed_unrated_exposure(tmp_memory_db, "S1", source="bootstrap")
        _seed_semantic_exposure(tmp_memory_db, "S1", source="contextual")
        env = {
            "BETTER_MEMORY_HOME": str(tmp_memory_db.parent),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        directive = payload["hookSpecificOutput"]["additionalContext"]
        assert "[bootstrap]" in directive
        assert "[contextual]" in directive
        assert "sources:" not in directive
        assert "My Title" in directive
        assert "My Semantic Fact" in directive

    def test_display_snapshot_shown_for_foreign_id(
        self, tmp_path, tmp_memory_db,
    ):
        """An agentcore-style exposure (mem-<uuid> id, NO local content row)
        renders its snapshotted display text."""
        c = connect(tmp_memory_db)
        apply_migrations(c)
        c.execute(
            """INSERT INTO session_memory_exposure
               (session_id, memory_kind, memory_id, exposed_at, source, display)
               VALUES ('S1', 'reflection', 'mem-abc-123',
                       '2026-08-06T10:00:00+00:00', 'retrieve',
                       'Snapshotted AWS Title')"""
        )
        c.commit()
        c.close()
        env = {
            "BETTER_MEMORY_HOME": str(tmp_memory_db.parent),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        directive = payload["hookSpecificOutput"]["additionalContext"]
        assert "Snapshotted AWS Title" in directive

    def test_empty_semantic_bucket_omitted(self, tmp_path, tmp_memory_db):
        _seed_unrated_exposure(tmp_memory_db, "S1")  # reflection only
        env = {
            "BETTER_MEMORY_HOME": str(tmp_memory_db.parent),
            "CLAUDE_SESSION_ID": "S1",
        }
        result = _run_hook(env)
        payload = json.loads(result.stdout)
        directive = payload["hookSpecificOutput"]["additionalContext"]
        assert "Semantic" not in directive
        assert "(none)" not in directive
