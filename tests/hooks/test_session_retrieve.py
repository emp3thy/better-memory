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


def _read_hook_errors(home: Path) -> list[dict]:
    """Read the hook_errors table after a hook ran. Returns rows as dicts."""
    conn = connect(home / "memory.db")
    try:
        rows = conn.execute(
            "SELECT id, hook_name, exception_type, exception_message FROM hook_errors"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def test_missing_db_injects_fallback_directive(tmp_path: Path) -> None:
    """No memory.db at all (first install before MCP server has booted)."""
    home = tmp_path / "no-db-home"
    home.mkdir()
    project_dir = tmp_path / "demo"
    project_dir.mkdir()

    result = _run_hook(home, cwd=project_dir)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("better-memory: memory injection failed (")
    assert "memory_retrieve manually" in ctx
    assert "session_retrieve:" in result.stderr


def test_simulated_sql_error_injects_fallback(
    home_with_schema: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """retrieve_reflections raises → fallback path."""
    project_dir = tmp_path / "demo"
    project_dir.mkdir()

    # Use a bootstrap script to monkeypatch the service inside the subprocess.
    # subprocess.run's monkeypatch fixture only affects the parent process, so
    # we point sys.executable at a -c that imports + patches + runs main().
    bootstrap = (
        "import sqlite3, sys\n"
        "from better_memory.services import reflection as rmod\n"
        "def _boom(self, **kw):\n"
        "    raise sqlite3.OperationalError('simulated retrieve failure')\n"
        "rmod.ReflectionSynthesisService.retrieve_reflections = _boom\n"
        "from better_memory.hooks.session_retrieve import main\n"
        "main()\n"
    )
    env = {**os.environ, "BETTER_MEMORY_HOME": str(home_with_schema)}
    result = subprocess.run(
        [sys.executable, "-c", bootstrap],
        text=True, capture_output=True, env=env, timeout=30,
        cwd=str(project_dir),
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "memory injection failed (OperationalError: simulated retrieve failure" in ctx
    rows = _read_hook_errors(home_with_schema)
    assert len(rows) == 1
    assert rows[0]["hook_name"] == "session_retrieve"
    assert rows[0]["exception_type"] == "OperationalError"


def test_hint_truncation(home_with_schema: Path, tmp_path: Path) -> None:
    project_dir = tmp_path / "trunc-project"
    project_dir.mkdir()
    long_hint = "x" * 1500  # 2.5x the 600 cap
    conn = connect(home_with_schema / "memory.db")
    try:
        _seed_reflection(
            conn, title="Long-hint reflection", project="trunc-project",
            polarity="do", hints=[long_hint, "short hint"],
        )
    finally:
        conn.close()

    result = _run_hook(home_with_schema, cwd=project_dir)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    # Find the long-hint line; assert it was truncated and ends with ellipsis.
    long_lines = [ln for ln in ctx.split("\n") if ln.startswith("- xxxx")]
    assert len(long_lines) == 1
    truncated = long_lines[0]
    # "- " prefix (2) + truncated hint (599 chars + "…" = 600) = 602 chars on the line
    assert len(truncated) == 2 + 600
    assert truncated.endswith("…")
    # Short hint remained intact.
    assert "- short hint" in ctx
