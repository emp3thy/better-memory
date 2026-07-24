"""Unit tests for SessionBootstrapService."""
from __future__ import annotations

import json
import re
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.session_bootstrap import SessionBootstrapService

_MIGRATIONS = Path(__file__).resolve().parents[2] / "better_memory" / "db" / "migrations"


def _seed_semantic(
    conn, *, content: str, project: str, scope: str, updated_at: str | None = None
) -> str:
    mid = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    ts = updated_at or now
    conn.execute(
        "INSERT INTO semantic_memories "
        "(id, content, project, scope, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mid, content, project, scope, now, ts),
    )
    conn.commit()
    return mid


def _seed_reflection(
    conn,
    *,
    project: str,
    polarity: str,
    scope: str = "project",
    title: str = "t",
    updated_at: str | None = None,
) -> str:
    rid = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    ts = updated_at or now
    conn.execute(
        "INSERT INTO reflections "
        "(id, title, project, tech, phase, polarity, use_cases, hints, "
        " confidence, status, evidence_count, created_at, updated_at, scope) "
        "VALUES (?, ?, ?, NULL, 'implementation', ?, 'uc', ?, 0.9, "
        " 'confirmed', 1, ?, ?, ?)",
        (rid, title, project, polarity, json.dumps(["h"]), now, ts, scope),
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

    assert "Semantic memories (1 shown in full)" in text
    assert "my-fact" in text
    assert "Reflections (shown in full)" in text
    assert "[do]" in text


def test_render_omits_empty_sections(conn, git_repo: Path) -> None:
    svc = SessionBootstrapService(conn)
    text = svc.bootstrap(source="startup", session_id="sess-empty", cwd=git_repo).additional_context

    assert "Semantic memories" not in text
    assert "Reflections" not in text
    # but the header and footer should still render
    assert "## better-memory: session bootstrap" in text
    assert "memory_credit" in text  # footer


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
    assert text.count("### Reflections (shown in full)") == 1


def test_render_bucket_isolation(conn, git_repo: Path) -> None:
    proj = git_repo.name
    _seed_reflection(conn, project=proj, polarity="do", scope="project")
    svc = SessionBootstrapService(conn)
    text = svc.bootstrap(source="startup", session_id="t-iso", cwd=git_repo).additional_context

    # Only the do polarity label appears; dont and neutral are absent.
    assert "[do]" in text
    assert "[dont" not in text
    assert "[neutral]" not in text


# ---------------------------------------------------------------------------
# Task 9: bootstrap slimming — top-N full render + index + affordance.
# ---------------------------------------------------------------------------


def test_top_n_limits_full_renders(conn, git_repo: Path) -> None:
    proj = git_repo.name
    for i in range(8):
        _seed_semantic(conn, content=f"sem-{i}", project=proj, scope="project")

    base = datetime(2026, 1, 1, tzinfo=UTC)
    titles = []
    ids = []
    for i in range(8):
        title = f"refl-title-{i}"
        titles.append(title)
        rid = _seed_reflection(
            conn,
            project=proj,
            polarity="do",
            scope="project",
            title=title,
            # Spaced updated_at so retrieve_reflections' updated_at DESC
            # tiebreak makes ordering deterministic: highest i = newest =
            # ranked first (full render); i=0 is oldest (index).
            updated_at=(base + timedelta(minutes=i)).isoformat(),
        )
        ids.append(rid)

    svc = SessionBootstrapService(conn, top_n=2)
    text = svc.bootstrap(
        source="startup", session_id="sess-topn", cwd=git_repo
    ).additional_context

    full_sem_lines = re.findall(r"^- \[[0-9a-f]{32}\]", text, flags=re.MULTILINE)
    assert len(full_sem_lines) == 2

    assert text.count("_id: ") == 2

    assert "### Index" in text

    # Oldest reflection (index 0) is guaranteed to land in the index —
    # its title appears exactly once, and its id is never rendered.
    assert text.count(titles[0]) == 1
    assert f"_id: {ids[0]}_" not in text


def test_general_semantic_always_full(conn, git_repo: Path) -> None:
    proj = git_repo.name
    for i in range(3):
        _seed_semantic(conn, content=f"gen-{i}", project="anyproj", scope="general")
    for i in range(3):
        _seed_semantic(conn, content=f"proj-{i}", project=proj, scope="project")

    svc = SessionBootstrapService(conn, top_n=1)
    text = svc.bootstrap(
        source="startup", session_id="sess-gen", cwd=git_repo
    ).additional_context

    full_sem_lines = re.findall(r"^- \[[0-9a-f]{32}\]", text, flags=re.MULTILINE)
    # 3 general (always full) + 1 project-scope (top_n=1) = 4.
    assert len(full_sem_lines) == 4
    assert "### Index" in text


def test_full_ids_never_truncated(conn, git_repo: Path) -> None:
    proj = git_repo.name
    mid = _seed_semantic(conn, content="fact", project=proj, scope="project")

    svc = SessionBootstrapService(conn, top_n=5)
    text = svc.bootstrap(
        source="startup", session_id="sess-fullid", cwd=git_repo
    ).additional_context

    assert f"[{mid}]" in text
    assert f"[{mid[:8]}]" not in text


def test_age_stamp_present(conn, git_repo: Path) -> None:
    proj = git_repo.name
    fixed_now = datetime(2026, 7, 11, tzinfo=UTC)
    old_ts = (fixed_now - timedelta(days=10)).isoformat()
    _seed_semantic(conn, content="aged-fact", project=proj, scope="project", updated_at=old_ts)

    svc = SessionBootstrapService(conn, clock=lambda: fixed_now, top_n=5)
    text = svc.bootstrap(
        source="startup", session_id="sess-age", cwd=git_repo
    ).additional_context

    assert "(10d old)" in text


def test_affordance_footer_counts(conn, git_repo: Path) -> None:
    proj = git_repo.name
    for i in range(7):
        _seed_semantic(conn, content=f"sem-{i}", project=proj, scope="project")

    svc = SessionBootstrapService(conn, top_n=1)
    text = svc.bootstrap(
        source="startup", session_id="sess-footer", cwd=git_repo
    ).additional_context

    assert "6 more memories are indexed above" in text
    assert "memory_retrieve" in text


def test_top_n_zero_is_legacy_full_dump(conn, git_repo: Path) -> None:
    proj = git_repo.name
    for i in range(5):
        _seed_semantic(conn, content=f"sem-{i}", project=proj, scope="project")
    for i in range(5):
        _seed_reflection(conn, project=proj, polarity="do", scope="project", title=f"t-{i}")

    svc = SessionBootstrapService(conn, top_n=0)
    text = svc.bootstrap(
        source="startup", session_id="sess-legacy", cwd=git_repo
    ).additional_context

    full_sem_lines = re.findall(r"^- \[[0-9a-f]{32}\]", text, flags=re.MULTILINE)
    assert len(full_sem_lines) == 5
    assert text.count("_id: ") == 5
    assert "### Index" not in text


def test_exposures_only_for_full_renders(conn, git_repo: Path) -> None:
    proj = git_repo.name
    ids = [
        _seed_semantic(conn, content=f"sem-{i}", project=proj, scope="project")
        for i in range(3)
    ]

    svc = SessionBootstrapService(conn, top_n=1)
    result = svc.bootstrap(source="startup", session_id="sess-exp", cwd=git_repo)

    rows = conn.execute(
        "SELECT memory_id FROM session_memory_exposure WHERE session_id = ?",
        ("sess-exp",),
    ).fetchall()
    exposed_ids = {r["memory_id"] for r in rows}
    assert len(exposed_ids) == 1

    full_id = next(iter(exposed_ids))
    assert f"[{full_id}]" in result.additional_context
    for mid in ids:
        if mid != full_id:
            assert mid not in exposed_ids


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


# ---------------------------------------------------------------------------
# Task 6: deferred bootstrap mode.
# ---------------------------------------------------------------------------


class TestDeferredBootstrap:
    def test_deferred_renders_general_semantics_and_index_only(
        self, conn, git_repo: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("BETTER_MEMORY_INJECT_MODE", "deferred")
        proj = git_repo.name

        _seed_semantic(conn, content="general-fact-one", project="anyproj", scope="general")
        _seed_semantic(conn, content="general-fact-two", project="otherproj", scope="general")
        _seed_semantic(conn, content="project-fact-one", project=proj, scope="project")
        _seed_semantic(conn, content="project-fact-two", project=proj, scope="project")
        _seed_semantic(conn, content="project-fact-three", project=proj, scope="project")
        for i in range(4):
            _seed_reflection(
                conn, project=proj, polarity="do", scope="project", title=f"refl-title-{i}",
            )

        svc = SessionBootstrapService(conn)
        text = svc.bootstrap(
            source="startup", session_id="sess-deferred-1", cwd=git_repo,
        ).additional_context

        assert "general-fact-one" in text
        assert "general-fact-two" in text
        assert "project-fact-one" not in text
        assert "project-fact-two" not in text
        assert "project-fact-three" not in text
        assert "refl-title-0" not in text
        assert "knows 4 reflections + 5 semantic memories" in text

    def test_deferred_exposes_only_general_semantics(
        self, conn, git_repo: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("BETTER_MEMORY_INJECT_MODE", "deferred")
        proj = git_repo.name

        gen_ids = [
            _seed_semantic(conn, content="general-a", project="anyproj", scope="general"),
            _seed_semantic(conn, content="general-b", project="otherproj", scope="general"),
        ]
        _seed_semantic(conn, content="proj-a", project=proj, scope="project")
        _seed_semantic(conn, content="proj-b", project=proj, scope="project")
        _seed_reflection(conn, project=proj, polarity="do", scope="project")

        svc = SessionBootstrapService(conn)
        svc.bootstrap(source="startup", session_id="sess-deferred-2", cwd=git_repo)

        rows = conn.execute(
            "SELECT memory_kind, memory_id, source FROM session_memory_exposure "
            "WHERE session_id = ?",
            ("sess-deferred-2",),
        ).fetchall()
        exposed = {(r["memory_kind"], r["memory_id"]) for r in rows}
        assert exposed == {("semantic", gid) for gid in gen_ids}
        assert all(r["source"] == "bootstrap" for r in rows)

    def test_legacy_mode_byte_identical(
        self, tmp_path: Path, git_repo: Path, monkeypatch
    ) -> None:
        from uuid import UUID

        proj = git_repo.name
        fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
        fixed_uuid = UUID("a" * 32)

        monkeypatch.setattr(
            "better_memory.services.episode.uuid4", lambda: fixed_uuid,
        )

        def make_conn(name: str):
            db = tmp_path / name
            c = connect(db)
            apply_migrations(c, migrations_dir=_MIGRATIONS)
            return c

        # Explicit ids + a fixed timestamp (rather than the _seed_* helpers,
        # which mint a random uuid per call) so the two conns render
        # byte-identical text — only the inject_mode env var differs.
        fixed_ts = "2026-01-01T00:00:00+00:00"

        def seed(c) -> None:
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("1" * 32, "proj-fact", proj, "project", fixed_ts, fixed_ts),
            )
            c.execute(
                "INSERT INTO semantic_memories "
                "(id, content, project, scope, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("2" * 32, "gen-fact", "anyproj", "general", fixed_ts, fixed_ts),
            )
            c.execute(
                "INSERT INTO reflections "
                "(id, title, project, tech, phase, polarity, use_cases, hints, "
                " confidence, status, evidence_count, created_at, updated_at, scope) "
                "VALUES (?, 'refl-a', ?, NULL, 'implementation', 'do', 'uc', ?, 0.9, "
                " 'confirmed', 1, ?, ?, 'project')",
                ("3" * 32, proj, json.dumps(["h"]), fixed_ts, fixed_ts),
            )
            c.commit()

        monkeypatch.delenv("BETTER_MEMORY_INJECT_MODE", raising=False)
        conn_unset = make_conn("unset.db")
        seed(conn_unset)
        svc_unset = SessionBootstrapService(conn_unset, clock=lambda: fixed_now)
        text_unset = svc_unset.bootstrap(
            source="startup", session_id="sess-same", cwd=git_repo,
        ).additional_context

        monkeypatch.setenv("BETTER_MEMORY_INJECT_MODE", "legacy")
        conn_legacy = make_conn("legacy.db")
        seed(conn_legacy)
        svc_legacy = SessionBootstrapService(conn_legacy, clock=lambda: fixed_now)
        text_legacy = svc_legacy.bootstrap(
            source="startup", session_id="sess-same", cwd=git_repo,
        ).additional_context

        assert text_unset == text_legacy

        monkeypatch.setenv("BETTER_MEMORY_INJECT_MODE", "deferred")
        conn_deferred = make_conn("deferred.db")
        seed(conn_deferred)
        svc_deferred = SessionBootstrapService(conn_deferred, clock=lambda: fixed_now)
        text_deferred = svc_deferred.bootstrap(
            source="startup", session_id="sess-same", cwd=git_repo,
        ).additional_context

        assert text_deferred != text_unset
