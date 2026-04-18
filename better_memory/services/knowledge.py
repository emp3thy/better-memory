"""Knowledge-base indexing and retrieval service.

The knowledge base is a tree of markdown documents organised by scope:

    knowledge-base/
        standards/<file>.md                 scope='standard'
        languages/<lang>/<file>.md          scope='language', language=<lang>
        projects/<project>/<file>.md        scope='project',  project=<project>

:class:`KnowledgeService` walks that tree, upserts rows into the
``documents`` table of ``knowledge.db``, and keeps the ``document_fts``
virtual table in sync (via the schema triggers). Reindexing is
mtime-driven: rows whose on-disk ``mtime`` has not changed are left
untouched.

The service is read-only from the user's perspective — external tooling
edits the markdown files; this class only indexes and queries them.

Connection ownership
--------------------
The service calls ``commit()`` on the provided connection. Callers must
not share a connection that already has an open transaction.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from better_memory.search.query import sanitize_fts5_query


@dataclass(frozen=True)
class KnowledgeDocument:
    """An indexed markdown document from the knowledge base."""

    id: str
    path: str  # relative to knowledge-base root, POSIX-style
    scope: str  # 'standard' | 'language' | 'project'
    project: str | None
    language: str | None
    content: str
    last_indexed: str
    file_mtime: str


@dataclass(frozen=True)
class KnowledgeSearchResult:
    """A single search hit: the document and its bm25 rank (lower = better)."""

    document: KnowledgeDocument
    rank: float


@dataclass(frozen=True)
class SessionLoad:
    """Bundle returned by :meth:`KnowledgeService.load_session`."""

    standards: list[KnowledgeDocument]
    languages: list[KnowledgeDocument]
    project: list[KnowledgeDocument]


@dataclass(frozen=True)
class ReindexReport:
    """Outcome counters from :meth:`KnowledgeService.reindex`."""

    added: int
    updated: int
    unchanged: int
    removed: int


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _doc_id(relative_path: str) -> str:
    """Stable 16-char id derived from the POSIX relative path."""
    return hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:16]


def _classify(
    relative_parts: tuple[str, ...],
) -> tuple[str, str | None, str | None] | None:
    """Return ``(scope, project, language)`` or ``None`` if the path is out of scope.

    The caller passes ``path.relative_to(knowledge_base).parts``.
    """
    if not relative_parts:
        return None
    head = relative_parts[0]
    if head == "standards":
        # standards/<anything>.md — must have at least the file name.
        if len(relative_parts) < 2:
            return None
        return ("standard", None, None)
    if head == "languages":
        # languages/<lang>/<file>.md — require the language segment + a file.
        if len(relative_parts) < 3:
            return None
        return ("language", None, relative_parts[1])
    if head == "projects":
        if len(relative_parts) < 3:
            return None
        return ("project", relative_parts[1], None)
    return None


def _row_to_document(row: sqlite3.Row) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=row["id"],
        path=row["path"],
        scope=row["scope"],
        project=row["project"],
        language=row["language"],
        content=row["content"],
        last_indexed=row["last_indexed"],
        file_mtime=row["file_mtime"],
    )


# Language detection --------------------------------------------------------

# JavaScript and TypeScript share a single knowledge bundle in this system, so
# `.js`/`.jsx` files and `package.json` all surface the `typescript` scope.
_EXT_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".cs": "csharp",
}

_MARKER_LANGUAGES: dict[str, str] = {
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "package.json": "typescript",
    "tsconfig.json": "typescript",
}

# Directories pruned from the detect_languages walk — vendor / build / VCS
# caches that would otherwise dominate the traversal and pollute results
# (e.g. vendored JS under `.venv` misclassified as "typescript").
_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".idea",
        ".vscode",
    }
)


class KnowledgeService:
    """Indexer + reader for the knowledge-base tree."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        knowledge_base: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._knowledge_base = Path(knowledge_base) if knowledge_base else None
        self._clock: Callable[[], datetime] = clock or _default_clock

    # ------------------------------------------------------------------ public

    def reindex(self) -> ReindexReport:
        """Walk ``knowledge_base`` and reconcile ``documents`` with disk.

        Rows whose file mtime matches the DB are skipped (``unchanged``).
        Missing files are removed. Returns a per-category count.
        """
        root = self._require_root()
        now_iso = self._clock().isoformat()

        # Build the on-disk picture: {rel_path -> (abs_path, mtime_iso, classification)}
        disk: dict[str, tuple[Path, str, tuple[str, str | None, str | None]]] = {}
        for file_path in root.rglob("*.md"):
            if not file_path.is_file():
                continue
            try:
                rel = file_path.relative_to(root)
            except ValueError:
                # Symlink or similar pointing outside the tree — skip silently.
                continue
            classification = _classify(rel.parts)
            if classification is None:
                continue
            rel_posix = rel.as_posix()
            stat = file_path.stat()
            mtime_iso = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
            disk[rel_posix] = (file_path, mtime_iso, classification)

        # Existing DB rows, keyed by path.
        existing: dict[str, sqlite3.Row] = {
            row["path"]: row
            for row in self._conn.execute(
                "SELECT id, path, file_mtime FROM documents"
            ).fetchall()
        }

        added = updated = unchanged = removed = 0

        # Upsert phase.
        for rel_posix, (abs_path, mtime_iso, (scope, project, language)) in disk.items():
            doc_id = _doc_id(rel_posix)
            prior = existing.get(rel_posix)
            if prior is not None and prior["file_mtime"] == mtime_iso:
                unchanged += 1
                continue

            content = abs_path.read_text(encoding="utf-8")
            self._conn.execute(
                """
                INSERT OR REPLACE INTO documents (
                    id, path, scope, project, language,
                    content, last_indexed, file_mtime
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_id,
                    rel_posix,
                    scope,
                    project,
                    language,
                    content,
                    now_iso,
                    mtime_iso,
                ),
            )
            if prior is None:
                added += 1
            else:
                updated += 1

        # Deletion phase: anything in the DB but not on disk.
        stale_paths = [p for p in existing if p not in disk]
        if stale_paths:
            placeholders = ",".join("?" for _ in stale_paths)
            self._conn.execute(
                f"DELETE FROM documents WHERE path IN ({placeholders})",
                stale_paths,
            )
            removed = len(stale_paths)

        self._conn.commit()
        return ReindexReport(
            added=added,
            updated=updated,
            unchanged=unchanged,
            removed=removed,
        )

    def detect_languages(self, cwd: Path) -> list[str]:
        """Detect languages in use under ``cwd`` via cheap marker/ext checks.

        Returns a deduplicated, alphabetically sorted list. The scan is
        pragmatic — marker files at the root plus a bounded extension walk.
        """
        cwd = Path(cwd)
        found: set[str] = set()

        # Marker files in the root directory.
        for marker, lang in _MARKER_LANGUAGES.items():
            if (cwd / marker).is_file():
                found.add(lang)

        # Any top-level *.csproj or *.sln implies C#.
        if any(cwd.glob("*.csproj")) or any(cwd.glob("*.sln")):
            found.add("csharp")

        # Extension sweep — bounded to avoid traversing huge vendor trees.
        # os.walk lets us prune _SKIP_DIRS in-place, and we bail out as soon
        # as every language has a hit.
        want = set(_EXT_LANGUAGES.values())
        for _dirpath, dirnames, filenames in os.walk(cwd):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                lang = _EXT_LANGUAGES.get(ext)
                if lang is not None:
                    found.add(lang)
                    if found >= want:
                        return sorted(found)

        return sorted(found)

    def project_for(self, cwd: Path) -> str:
        """Return the project name for ``cwd``.

        Defaults to ``cwd.name`` (the leaf directory). Overridden by the first
        non-empty line of ``<cwd>/.better-memory`` when present.
        """
        cwd = Path(cwd)
        override = cwd / ".better-memory"
        if override.is_file():
            text = override.read_text(encoding="utf-8").strip()
            if text:
                return text.splitlines()[0].strip()
        return cwd.name

    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        limit: int = 10,
    ) -> list[KnowledgeSearchResult]:
        """BM25 full-text search against ``document_fts``.

        When ``project`` is set, project-scoped rows are restricted to that
        project; standards and languages always surface.
        """
        # Sanitise before MATCH: raw user text containing FTS5 operator
        # chars (``-``, ``:``, ``"``) would otherwise crash the query
        # (e.g. ``better-memory`` → ``no such column: memory``).
        sanitized = sanitize_fts5_query(query)
        if not sanitized:
            return []

        sql = (
            "SELECT d.id AS id, d.path AS path, d.scope AS scope, "
            "       d.project AS project, d.language AS language, "
            "       d.content AS content, d.last_indexed AS last_indexed, "
            "       d.file_mtime AS file_mtime, "
            "       bm25(document_fts) AS rank "
            "FROM document_fts "
            "JOIN documents d ON d.rowid = document_fts.rowid "
            "WHERE document_fts MATCH ? "
        )
        params: list[object] = [sanitized]
        if project is not None:
            sql += (
                "AND (d.scope = 'standard' OR d.scope = 'language' "
                "     OR d.project = ?) "
            )
            params.append(project)
        sql += "ORDER BY rank LIMIT ?"
        params.append(limit)

        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Safety net for any FTS5 operator the sanitiser missed.
            return []
        return [
            KnowledgeSearchResult(document=_row_to_document(row), rank=row["rank"])
            for row in rows
        ]

    def list_documents(
        self,
        *,
        project: str | None = None,
    ) -> list[KnowledgeDocument]:
        """Return documents, optionally filtered to a project (+ all shared scopes)."""
        if project is None:
            rows = self._conn.execute(
                "SELECT * FROM documents ORDER BY path"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM documents "
                "WHERE scope IN ('standard', 'language') OR project = ? "
                "ORDER BY path",
                (project,),
            ).fetchall()
        return [_row_to_document(row) for row in rows]

    def load_session(self, cwd: Path) -> SessionLoad:
        """Build the ``{standards, languages, project}`` bundle for ``cwd``."""
        cwd = Path(cwd)
        detected = set(self.detect_languages(cwd))
        project_name = self.project_for(cwd)

        standards = [
            _row_to_document(row)
            for row in self._conn.execute(
                "SELECT * FROM documents WHERE scope = 'standard' ORDER BY path"
            ).fetchall()
        ]

        if detected:
            placeholders = ",".join("?" for _ in detected)
            language_rows = self._conn.execute(
                f"SELECT * FROM documents WHERE scope = 'language' "
                f"AND language IN ({placeholders}) ORDER BY path",
                tuple(sorted(detected)),
            ).fetchall()
        else:
            language_rows = []
        languages = [_row_to_document(row) for row in language_rows]

        project_docs = [
            _row_to_document(row)
            for row in self._conn.execute(
                "SELECT * FROM documents WHERE scope = 'project' AND project = ? "
                "ORDER BY path",
                (project_name,),
            ).fetchall()
        ]

        return SessionLoad(
            standards=standards,
            languages=languages,
            project=project_docs,
        )

    # ----------------------------------------------------------------- helpers

    def _require_root(self) -> Path:
        if self._knowledge_base is None:
            raise RuntimeError(
                "KnowledgeService has no knowledge_base configured; pass "
                "knowledge_base=... to the constructor."
            )
        return self._knowledge_base
