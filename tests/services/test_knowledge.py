"""Tests for :class:`better_memory.services.knowledge.KnowledgeService`.

Exercises the knowledge-base indexer against a tmp markdown tree: the initial
walk populates ``documents`` + ``document_fts``; subsequent reindex calls
only touch rows whose on-disk ``mtime`` changed or whose file disappeared.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from better_memory.db.connection import connect
from better_memory.db.schema import apply_migrations
from better_memory.services.knowledge import (
    KnowledgeService,
    SessionLoad,
)

# Location of the knowledge-DB migrations relative to the package.
_KNOWLEDGE_MIGRATIONS = (
    Path(__file__).resolve().parents[2]
    / "better_memory"
    / "db"
    / "knowledge_migrations"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def knowledge_tree(tmp_knowledge_base: Path) -> Path:
    """Populate a fresh knowledge-base directory with a representative tree."""
    (tmp_knowledge_base / "standards").mkdir()
    (tmp_knowledge_base / "standards" / "golden.md").write_text(
        "Always test first.",
        encoding="utf-8",
    )
    (tmp_knowledge_base / "standards" / "ignore.txt").write_text(
        "not markdown", encoding="utf-8"
    )

    (tmp_knowledge_base / "languages" / "python").mkdir(parents=True)
    (tmp_knowledge_base / "languages" / "python" / "conventions.md").write_text(
        "PEP 8 applies.", encoding="utf-8"
    )

    (tmp_knowledge_base / "languages" / "csharp").mkdir(parents=True)
    (tmp_knowledge_base / "languages" / "csharp" / "conventions.md").write_text(
        "Use PascalCase.", encoding="utf-8"
    )

    (tmp_knowledge_base / "projects" / "auth").mkdir(parents=True)
    (tmp_knowledge_base / "projects" / "auth" / "architecture.md").write_text(
        "auth service uses JWT.", encoding="utf-8"
    )

    (tmp_knowledge_base / "projects" / "billing").mkdir(parents=True)
    (tmp_knowledge_base / "projects" / "billing" / "architecture.md").write_text(
        "billing uses Stripe.", encoding="utf-8"
    )

    # File outside the three recognised scopes — must be skipped.
    (tmp_knowledge_base / "other").mkdir()
    (tmp_knowledge_base / "other" / "foo.md").write_text(
        "out of scope", encoding="utf-8"
    )

    return tmp_knowledge_base


@pytest.fixture
def knowledge_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Fresh knowledge.db with knowledge-migrations applied."""
    db_path = tmp_path / "knowledge.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn, migrations_dir=_KNOWLEDGE_MIGRATIONS)
        yield conn
    finally:
        conn.close()


@pytest.fixture
def fixed_clock() -> Any:
    fixed = datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


@pytest.fixture
def service(
    knowledge_conn: sqlite3.Connection,
    knowledge_tree: Path,
    fixed_clock: Any,
) -> KnowledgeService:
    return KnowledgeService(
        knowledge_conn,
        knowledge_base=knowledge_tree,
        clock=fixed_clock,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bump_mtime(path: Path, *, delta_seconds: int = 60) -> None:
    """Move a file's mtime forward without changing its content."""
    stat = path.stat()
    new_mtime = stat.st_mtime + delta_seconds
    os.utime(path, (stat.st_atime, new_mtime))


# ---------------------------------------------------------------------------
# Reindex
# ---------------------------------------------------------------------------


def test_reindex_initial_walk(service: KnowledgeService) -> None:
    report = service.reindex()
    assert report.added == 5
    assert report.updated == 0
    assert report.unchanged == 0
    assert report.removed == 0


def test_reindex_idempotent_without_changes(service: KnowledgeService) -> None:
    service.reindex()
    report = service.reindex()
    assert report.added == 0
    assert report.updated == 0
    assert report.unchanged == 5
    assert report.removed == 0


def test_reindex_detects_mtime_change(
    service: KnowledgeService,
    knowledge_tree: Path,
) -> None:
    service.reindex()
    _bump_mtime(knowledge_tree / "projects" / "auth" / "architecture.md")
    report = service.reindex()
    assert report.added == 0
    assert report.updated == 1
    assert report.unchanged == 4
    assert report.removed == 0


def test_reindex_removes_deleted_files(
    service: KnowledgeService,
    knowledge_tree: Path,
) -> None:
    service.reindex()
    (knowledge_tree / "projects" / "billing" / "architecture.md").unlink()
    report = service.reindex()
    assert report.added == 0
    assert report.updated == 0
    assert report.unchanged == 4
    assert report.removed == 1


def test_reindex_skips_non_markdown_and_out_of_scope(
    service: KnowledgeService,
    knowledge_conn: sqlite3.Connection,
) -> None:
    service.reindex()
    paths = {
        row["path"]
        for row in knowledge_conn.execute("SELECT path FROM documents").fetchall()
    }
    assert "standards/ignore.txt" not in paths
    assert "other/foo.md" not in paths


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_search_finds_project_document(service: KnowledgeService) -> None:
    service.reindex()
    hits = service.search("JWT")
    assert len(hits) >= 1
    assert hits[0].document.path == "projects/auth/architecture.md"


def test_search_project_filter_narrows_projects(
    service: KnowledgeService,
) -> None:
    service.reindex()
    hits = service.search("Stripe", project="billing")
    assert len(hits) == 1
    assert hits[0].document.path == "projects/billing/architecture.md"


def test_search_project_filter_preserves_languages_and_standards(
    service: KnowledgeService,
) -> None:
    """Languages and standards always surface regardless of the project filter."""
    service.reindex()
    hits = service.search("PEP", project="auth")
    assert len(hits) == 1
    assert hits[0].document.scope == "language"
    assert hits[0].document.language == "python"


def test_search_with_hyphenated_query_does_not_crash(
    service: KnowledgeService,
) -> None:
    """Regression: ``better-memory`` once raised ``no such column: memory``.

    FTS5 parses ``-memory`` as a column-exclusion filter; the service must
    sanitise operator characters out of user text before calling MATCH.
    """
    service.reindex()

    # Must not raise; may return any hits (we only assert it completes).
    service.search("better-memory project commit push conventions")


def test_search_with_fts5_operator_chars_does_not_crash(
    service: KnowledgeService,
) -> None:
    """Colons, quotes, parentheses, and reserved keywords must all survive."""
    service.reindex()
    service.search('alpha:beta "gamma" (delta)')
    service.search("AND OR NOT NEAR")


def test_reindex_content_update_propagates_to_fts(
    tmp_knowledge_base: Path,
    knowledge_conn: sqlite3.Connection,
    fixed_clock: Any,
) -> None:
    """A content rewrite must remove old terms and insert new ones in FTS."""
    (tmp_knowledge_base / "projects" / "test").mkdir(parents=True)
    doc = tmp_knowledge_base / "projects" / "test" / "foo.md"
    doc.write_text("alpha marker", encoding="utf-8")

    service = KnowledgeService(
        knowledge_conn,
        knowledge_base=tmp_knowledge_base,
        clock=fixed_clock,
    )
    service.reindex()
    hits = service.search("alpha")
    assert len(hits) == 1
    assert hits[0].document.path == "projects/test/foo.md"

    # Rewrite content and bump mtime so reindex picks it up.
    doc.write_text("beta marker", encoding="utf-8")
    _bump_mtime(doc)

    report = service.reindex()
    assert report.updated == 1

    assert service.search("alpha") == []
    beta_hits = service.search("beta")
    assert len(beta_hits) == 1
    assert beta_hits[0].document.path == "projects/test/foo.md"


# ---------------------------------------------------------------------------
# list_documents
# ---------------------------------------------------------------------------


def test_list_documents_returns_all(service: KnowledgeService) -> None:
    service.reindex()
    docs = service.list_documents()
    assert len(docs) == 5
    scopes = {d.scope for d in docs}
    assert scopes == {"standard", "language", "project"}


def test_list_documents_with_project_filter(service: KnowledgeService) -> None:
    service.reindex()
    docs = service.list_documents(project="auth")
    paths = {d.path for d in docs}
    # auth-only project, plus all standards + languages
    assert paths == {
        "standards/golden.md",
        "languages/python/conventions.md",
        "languages/csharp/conventions.md",
        "projects/auth/architecture.md",
    }


# ---------------------------------------------------------------------------
# detect_languages
# ---------------------------------------------------------------------------


def test_detect_languages_python_via_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    service = KnowledgeService.__new__(KnowledgeService)  # type: ignore[call-arg]
    # We only need the method — no DB interaction for detect_languages.
    assert KnowledgeService.detect_languages(service, tmp_path) == ["python"]


def test_detect_languages_python_and_typescript(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    service = KnowledgeService.__new__(KnowledgeService)  # type: ignore[call-arg]
    assert KnowledgeService.detect_languages(service, tmp_path) == [
        "python",
        "typescript",
    ]


def test_detect_languages_csharp_via_csproj(tmp_path: Path) -> None:
    (tmp_path / "MyApp.csproj").write_text("<Project />", encoding="utf-8")
    service = KnowledgeService.__new__(KnowledgeService)  # type: ignore[call-arg]
    assert KnowledgeService.detect_languages(service, tmp_path) == ["csharp"]


def test_detect_languages_by_extension(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "app.tsx").write_text("export {};", encoding="utf-8")
    service = KnowledgeService.__new__(KnowledgeService)  # type: ignore[call-arg]
    assert KnowledgeService.detect_languages(service, tmp_path) == [
        "python",
        "typescript",
    ]


def test_detect_languages_skips_vendor_dirs(tmp_path: Path) -> None:
    """Files under `.git` / `node_modules` must not influence detection."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hook.py").write_text("")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "foo.ts").write_text("")

    service = KnowledgeService.__new__(KnowledgeService)  # type: ignore[call-arg]
    assert KnowledgeService.detect_languages(service, tmp_path) == ["python"]


# ---------------------------------------------------------------------------
# project_for
# ---------------------------------------------------------------------------


def test_project_for_defaults_to_cwd_name(tmp_path: Path) -> None:
    cwd = tmp_path / "my-service"
    cwd.mkdir()
    service = KnowledgeService.__new__(KnowledgeService)  # type: ignore[call-arg]
    assert KnowledgeService.project_for(service, cwd) == "my-service"


def test_project_for_override_via_dot_better_memory(tmp_path: Path) -> None:
    cwd = tmp_path / "renamed"
    cwd.mkdir()
    (cwd / ".better-memory").write_text("canonical-name\n", encoding="utf-8")
    service = KnowledgeService.__new__(KnowledgeService)  # type: ignore[call-arg]
    assert KnowledgeService.project_for(service, cwd) == "canonical-name"


# ---------------------------------------------------------------------------
# load_session
# ---------------------------------------------------------------------------


def test_load_session_returns_standards_languages_project(
    service: KnowledgeService,
    tmp_path: Path,
) -> None:
    service.reindex()
    cwd = tmp_path / "auth"
    cwd.mkdir()
    (cwd / "pyproject.toml").write_text("[project]\nname='auth'\n", encoding="utf-8")

    load = service.load_session(cwd)
    assert isinstance(load, SessionLoad)

    assert [d.path for d in load.standards] == ["standards/golden.md"]
    assert [d.path for d in load.languages] == [
        "languages/python/conventions.md",
    ]
    assert [d.path for d in load.project] == [
        "projects/auth/architecture.md",
    ]


def test_load_session_no_languages_when_none_detected(
    service: KnowledgeService,
    tmp_path: Path,
) -> None:
    service.reindex()
    cwd = tmp_path / "no-signals"
    cwd.mkdir()

    load = service.load_session(cwd)
    assert load.languages == []
    assert [d.path for d in load.standards] == ["standards/golden.md"]
    # 'no-signals' isn't a project in the knowledge base.
    assert load.project == []
