"""Tests for ``better_memory.hooks.session_retrieve``.

Mirrors the subprocess pattern used by tests/hooks/test_session_start.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations

_MIGRATIONS = Path(__file__).resolve().parents[2] / "better_memory" / "db" / "migrations"


def _run_hook(
    home_dir: Path,
    *,
    stdin: str = "",
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "BETTER_MEMORY_HOME": str(home_dir)}
    return subprocess.run(
        [sys.executable, "-m", "better_memory.hooks.session_retrieve"],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        cwd=str(cwd) if cwd is not None else None,
    )


def _seed_reflection(
    conn,
    *,
    title: str,
    project: str,
    polarity: str,
    use_cases: str = "Test scenario",
    hints: list[str] | None = None,
    confidence: float = 0.9,
    tech: str | None = None,
    phase: str = "implementation",
) -> str:
    rid = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO reflections (
            id, title, project, tech, phase, polarity, use_cases, hints,
            confidence, status, evidence_count, created_at, updated_at, scope
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', 1, ?, ?, 'project')
        """,
        (
            rid, title, project, tech, phase, polarity, use_cases,
            json.dumps(hints or ["a hint"]), confidence, now, now,
        ),
    )
    conn.commit()
    return rid


@pytest.fixture
def home_with_schema(tmp_path: Path) -> Path:
    home = tmp_path / "better-memory-home"
    home.mkdir()
    conn = connect(home / "memory.db")
    try:
        apply_migrations(conn, migrations_dir=_MIGRATIONS)
    finally:
        conn.close()
    return home


def test_populated_db_renders_buckets(home_with_schema: Path, tmp_path: Path) -> None:
    project_dir = tmp_path / "demo-project"
    project_dir.mkdir()
    conn = connect(home_with_schema / "memory.db")
    try:
        _seed_reflection(
            conn, title="Use Timespan.hour", project="demo-project",
            polarity="do", use_cases="Growatt API consumption queries",
            hints=["Switch to Timespan.hour", "12-snapshot aggregate"],
        )
        _seed_reflection(
            conn, title="Avoid Timespan.day", project="demo-project",
            polarity="dont", use_cases="Growatt API queries",
            hints=["Returns inflated values"],
        )
    finally:
        conn.close()

    result = _run_hook(home_with_schema, cwd=project_dir)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "Use Timespan.hour" in ctx
    assert "Avoid Timespan.day" in ctx
    assert "### do" in ctx
    assert "### dont" in ctx
    assert "memory_record_use" in ctx  # footer present


def test_empty_db_injects_no_memory_yet_message(
    home_with_schema: Path, tmp_path: Path
) -> None:
    project_dir = tmp_path / "fresh-project"
    project_dir.mkdir()

    result = _run_hook(home_with_schema, cwd=project_dir)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "no reflections recorded yet" in ctx
    assert "memory_observe" in ctx
    # No bucket headings, no fallback "memory injection failed" text.
    assert "### do" not in ctx
    assert "memory injection failed" not in ctx
