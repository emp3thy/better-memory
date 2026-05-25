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


def _seed_reflection_hints(
    conn, *, project: str, polarity: str, hints: list[str], scope: str = "project"
) -> str:
    rid = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, tech, phase, polarity, use_cases, hints, "
        " confidence, status, evidence_count, created_at, updated_at, scope) "
        "VALUES (?, 't', ?, NULL, 'implementation', ?, 'uc', ?, 0.9, "
        " 'confirmed', 1, ?, ?, ?)",
        (rid, project, polarity, json.dumps(hints), now, now, scope),
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


def test_general_project_excludes_project_scope_general_rows(
    conn, tmp_path: Path
) -> None:
    """Spec §6 corner case: when project resolves to 'general', the default
    union (project = ? OR scope = 'general') would also pull rows tagged
    project='general', scope='project'. scope_filter='general' excludes them.
    """
    nongit = tmp_path / "loose"
    nongit.mkdir()
    # Corner case row: project='general', scope='project' — MUST be excluded.
    _seed_semantic(conn, content="gen-proj", project="general", scope="project")
    # Cross-project general row — MUST be included.
    _seed_semantic(conn, content="other-gen", project="unrelated", scope="general")
    svc = SessionBootstrapService(conn)

    result = svc.bootstrap(source="startup", session_id="sess-g-r", cwd=nongit)

    assert result.project == "general"
    assert result.semantic_count == 1


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


def test_render_includes_header_with_project_source_episode(conn, git_repo: Path) -> None:
    svc = SessionBootstrapService(conn)
    result = svc.bootstrap(source="startup", session_id="sess-h", cwd=git_repo)

    text = result.additional_context
    assert "## better-memory: session bootstrap" in text
    assert "Project: demo-repo" in text
    assert "Source: startup" in text
    assert "Episode: opened" in text


def test_render_includes_semantic_and_reflections_sections(conn, git_repo: Path) -> None:
    proj = git_repo.name
    _seed_semantic(conn, content="my-fact", project=proj, scope="project")
    _seed_reflection(conn, project=proj, polarity="do", scope="project")
    svc = SessionBootstrapService(conn)

    text = svc.bootstrap(source="startup", session_id="sess-r2", cwd=git_repo).additional_context

    assert "Semantic memories (1 entries)" in text
    assert "my-fact" in text
    assert "Reflections — do (prior wins)" in text


def test_render_omits_empty_sections(conn, git_repo: Path) -> None:
    svc = SessionBootstrapService(conn)
    text = svc.bootstrap(source="startup", session_id="sess-empty", cwd=git_repo).additional_context

    assert "Semantic memories" not in text
    assert "Reflections" not in text
    # but the header and footer should still render
    assert "## better-memory: session bootstrap" in text
    assert "memory_record_use" in text  # footer


def test_render_truncates_long_hints(conn, git_repo: Path) -> None:
    proj = git_repo.name
    long_hint = "x" * 700
    _seed_reflection_hints(conn, project=proj, polarity="do", hints=[long_hint])
    svc = SessionBootstrapService(conn)
    text = svc.bootstrap(source="startup", session_id="t-trunc", cwd=git_repo).additional_context
    assert "x" * 700 not in text
    assert "…" in text
    # The truncated form must be exactly 600 chars (599 x + 1 ellipsis).
    # Confirm by checking that 599 x's followed by … is in the output.
    assert ("x" * 599 + "…") in text


def test_render_multi_item_bucket(conn, git_repo: Path) -> None:
    proj = git_repo.name
    rid1 = _seed_reflection(conn, project=proj, polarity="do", scope="project")
    rid2 = _seed_reflection(conn, project=proj, polarity="do", scope="project")
    svc = SessionBootstrapService(conn)
    text = svc.bootstrap(source="startup", session_id="t-multi", cwd=git_repo).additional_context

    # Both ids appear.
    assert f"_id: {rid1}_" in text
    assert f"_id: {rid2}_" in text
    # Header appears exactly once.
    assert text.count("### Reflections — do (prior wins)") == 1


def test_render_bucket_isolation(conn, git_repo: Path) -> None:
    proj = git_repo.name
    _seed_reflection(conn, project=proj, polarity="do", scope="project")
    svc = SessionBootstrapService(conn)
    text = svc.bootstrap(source="startup", session_id="t-iso", cwd=git_repo).additional_context

    # Only the do bucket header appears; dont and neutral are empty so absent.
    assert "Reflections — do (prior wins)" in text
    assert "Reflections — dont (approaches to avoid)" not in text
    assert "Reflections — neutral (context)" not in text


# ---------------------------------------------------------------------------
# list_session_exposures (Task 5a) — extracted from inline MCP handler.
# ---------------------------------------------------------------------------


def test_list_session_exposures_returns_envelope(conn) -> None:
    svc = SessionBootstrapService(conn)
    result = svc.list_session_exposures(session_id="test-session-no-data")

    assert isinstance(result, dict)
    assert result.get("session_id") == "test-session-no-data"
    assert "exposures" in result
    assert result["exposures"] == []


def test_list_session_exposures_returns_seeded_reflection_row(conn) -> None:
    # Seed a reflection that the exposure row references, so the LEFT JOIN
    # populates the display column (title) the handler returns.
    rid = _seed_reflection(conn, project="proj-a", polarity="do", scope="project")
    conn.execute(
        "INSERT INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source) VALUES "
        "('s1', 'reflection', ?, '2026-05-25T12:00:00Z', 'bootstrap')",
        (rid,),
    )
    conn.commit()

    svc = SessionBootstrapService(conn)
    result = svc.list_session_exposures(session_id="s1")

    assert result["session_id"] == "s1"
    assert len(result["exposures"]) == 1
    row = result["exposures"][0]
    assert row["id"] == rid
    assert row["kind"] == "reflection"
    assert row["exposed_at"] == "2026-05-25T12:00:00Z"
    assert row["source"] == "bootstrap"
    # Reflection rows expose 'title' (not 'content').
    assert "title" in row
    assert "content" not in row


def test_list_session_exposures_semantic_uses_content_key(conn) -> None:
    sid = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO semantic_memories "
        "(id, content, project, scope, created_at, updated_at) "
        "VALUES (?, 'prefer short filenames', 'proj-b', 'project', ?, ?)",
        (sid, now, now),
    )
    conn.execute(
        "INSERT INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source) VALUES "
        "('s-sem', 'semantic', ?, '2026-05-25T12:00:00Z', 'retrieve')",
        (sid,),
    )
    conn.commit()

    svc = SessionBootstrapService(conn)
    result = svc.list_session_exposures(session_id="s-sem")

    assert len(result["exposures"]) == 1
    row = result["exposures"][0]
    assert row["kind"] == "semantic"
    assert row["id"] == sid
    assert row["content"] == "prefer short filenames"
    assert "title" not in row


def test_list_session_exposures_dedupes_by_kind_id(conn) -> None:
    """A memory can have two exposure rows (bootstrap + retrieve) in one
    session. The handler dedupes by (memory_kind, memory_id); the apply path
    stamps ALL unrated rows per (kind, id), so the LLM must see one entry
    per unique memory or apply_session_ratings rejects the batch.
    """
    rid = _seed_reflection(conn, project="proj-d", polarity="do", scope="project")
    conn.executemany(
        "INSERT INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source) VALUES "
        "(?, ?, ?, ?, ?)",
        [
            ("s-dup", "reflection", rid, "2026-05-25T12:00:00Z", "bootstrap"),
            ("s-dup", "reflection", rid, "2026-05-25T13:00:00Z", "retrieve"),
        ],
    )
    conn.commit()

    svc = SessionBootstrapService(conn)
    result = svc.list_session_exposures(session_id="s-dup")
    assert len(result["exposures"]) == 1
    # MIN(exposed_at) wins — the earlier timestamp.
    assert result["exposures"][0]["exposed_at"] == "2026-05-25T12:00:00Z"


def test_list_session_exposures_excludes_rated_rows(conn) -> None:
    rid = _seed_reflection(conn, project="proj-e", polarity="do", scope="project")
    conn.execute(
        "INSERT INTO session_memory_exposure "
        "(session_id, memory_kind, memory_id, exposed_at, source, "
        " rated_at, classification) VALUES "
        "('s-rated', 'reflection', ?, '2026-05-25T12:00:00Z', 'bootstrap', "
        " '2026-05-25T13:00:00Z', 'cited')",
        (rid,),
    )
    conn.commit()

    svc = SessionBootstrapService(conn)
    result = svc.list_session_exposures(session_id="s-rated")
    assert result["exposures"] == []


def test_list_session_exposures_empty_session_id_returns_none_envelope(conn) -> None:
    """Preserves inline handler's `if not sid` short-circuit:
    `{"session_id": None, "exposures": []}` when session_id is empty.
    """
    svc = SessionBootstrapService(conn)
    result = svc.list_session_exposures(session_id="")
    assert result == {"session_id": None, "exposures": []}
