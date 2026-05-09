"""Unit tests for SessionBootstrapService."""
from __future__ import annotations

import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.session_bootstrap import SessionBootstrapService

_MIGRATIONS = Path(__file__).resolve().parents[2] / "better_memory" / "db" / "migrations"


def _seed_semantic(conn, *, content: str, project: str, scope: str) -> str:
    mid = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO semantic_memories "
        "(id, content, project, scope, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mid, content, project, scope, now, now),
    )
    conn.commit()
    return mid


def _seed_reflection(conn, *, project: str, polarity: str, scope: str = "project") -> str:
    rid = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, tech, phase, polarity, use_cases, hints, "
        " confidence, status, evidence_count, created_at, updated_at, scope) "
        "VALUES (?, 't', ?, NULL, 'implementation', ?, 'uc', ?, 0.9, "
        " 'confirmed', 1, ?, ?, ?)",
        (rid, project, polarity, json.dumps(["h"]), now, now, scope),
    )
    conn.commit()
    return rid


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    apply_migrations(c, migrations_dir=_MIGRATIONS)
    yield c
    c.close()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=str(repo), check=True)
    return repo


def test_startup_source_opens_new_episode(conn, git_repo: Path) -> None:
    svc = SessionBootstrapService(conn)

    result = svc.bootstrap(source="startup", session_id="sess-1", cwd=git_repo)

    assert result.project == "demo-repo"
    assert result.source == "startup"
    assert result.episode_action == "opened"
    assert result.episode_id  # non-empty


def test_compact_with_existing_episode_reuses(conn, git_repo: Path) -> None:
    svc = SessionBootstrapService(conn)
    first = svc.bootstrap(source="startup", session_id="sess-2", cwd=git_repo)

    second = svc.bootstrap(source="compact", session_id="sess-2", cwd=git_repo)

    assert second.episode_action == "reused"
    assert second.episode_id == first.episode_id
    assert second.source == "compact"


def test_resume_with_no_existing_episode_opens(conn, git_repo: Path) -> None:
    svc = SessionBootstrapService(conn)

    result = svc.bootstrap(source="resume", session_id="sess-cold", cwd=git_repo)

    assert result.episode_action == "opened"
    assert result.source == "resume"


@pytest.mark.parametrize("bad_source", ["", "unknown", None, "STARTUP"])
def test_bad_source_coerces_to_startup(conn, git_repo: Path, bad_source) -> None:
    svc = SessionBootstrapService(conn)

    result = svc.bootstrap(source=bad_source, session_id="sess-x", cwd=git_repo)

    assert result.source == "startup"


def test_cwd_not_in_git_uses_general_scope(conn, tmp_path: Path) -> None:
    nongit = tmp_path / "loose"
    nongit.mkdir()
    svc = SessionBootstrapService(conn)

    result = svc.bootstrap(source="startup", session_id="sess-g", cwd=nongit)

    assert result.project == "general"


def test_bootstrap_counts_retrieved_rows(conn, git_repo: Path) -> None:
    proj = git_repo.name  # demo-repo
    _seed_semantic(conn, content="proj-a", project=proj, scope="project")
    _seed_semantic(conn, content="gen-a", project="anything", scope="general")
    _seed_reflection(conn, project=proj, polarity="do", scope="project")
    _seed_reflection(conn, project=proj, polarity="dont", scope="project")
    _seed_reflection(conn, project="anything", polarity="do", scope="general")
    svc = SessionBootstrapService(conn)

    result = svc.bootstrap(source="startup", session_id="sess-r", cwd=git_repo)

    assert result.semantic_count == 2  # 1 project + 1 general
    assert result.reflections_counts["do"] == 2     # project + general
    assert result.reflections_counts["dont"] == 1
    assert result.reflections_counts["neutral"] == 0
