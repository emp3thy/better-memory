# Semantic memories — design

**Status:** Approved 2026-05-04
**Branch target:** new feature branch off `main` after PR #25 merges (e.g. `semantic-memories`)
**Predecessor:** PR #25 (episodic synthesis + general scope) — adds `scope` ('project'|'general') to `observations` and `reflections`. This design extends the same scope model to a new top-level concept.

## Goal

Add a new top-level concept to better-memory: **semantic memories**, defined as user-stated facts and preferences. Distinct from episodic data (observations / reflections), they are written explicitly by the user and never LLM-distilled. Like reflections, they support project-scope and general-scope so cross-project workflow rules surface in every session. They are retrieved at session startup as a flat list (no buckets), alongside the existing `memory_retrieve` reflection retrieval.

## Why now

The user's auto-memory MEMORY.md system already stores facts and preferences but is per-project (under `~/.claude/projects/<proj>/memory/`) and limited to ~200 lines. With the general-scope feature just added in PR #25, better-memory now supports surfacing the same item in every project. The natural next step: a first-class structured place for user-stated current truths, complementing (not replacing) MEMORY.md. Examples that would land here:

- "I prefer terse responses" (general)
- "This codebase uses pytest, never unittest" (project)
- "Always assign per-step confidence to a superpowers plan" (general — the workflow rule recorded earlier today)

## Decisions log

| Decision | Choice | Why |
|---|---|---|
| Record shape | Free-form `content: str` + `scope: 'project'\|'general'` | Maximum flexibility, minimum ceremony. Users naturally express rules as sentences (see existing MEMORY.md content) — not key-value pairs. Categories/tags can be added inside the content text by convention if filtering becomes useful later. |
| Project-collision protection | The `(project, scope)` pair already gives this. `project='X', scope='project'` is bucketed to project X only; `scope='general'` rows surface in every project regardless of where they were created. | Same model as PR #25's reflections.scope — proven and tested. |
| Storage | New top-level table `semantic_memories(id, content, project, scope, created_at, updated_at)` | Cleaner than reusing `observations` with a `kind` discriminator. `observations` has 4-5 fields that don't apply (episode_id, outcome, status, theme, component, tech) and reusing would mean every existing query has to reason about which fields apply to which kind — exactly the conflation we just unwound by removing `synthesis_runs`. |
| Retrieval shape | New tool `memory.semantic_retrieve` returning a flat list, separate from `memory.retrieve` | Reflections are bucketed by outcome (`do`/`dont`/`neutral`); semantic memories are facts not lessons, and shoehorning them into outcome buckets is wrong. One extra startup call (a single SELECT + serialization) is trivial cost for clean separation of concerns. |
| Lifecycle | Hard delete + edit-in-place (`create`, `update_text`, `delete`) | Mirrors a notes-app mental model. Audit trail for "what did this preference USED to say" is rarely useful in practice. Reflections need pending_review/confirmed/retired because they're LLM-distilled and need user review; semantic memories are already "confirmed" by virtue of being user-asserted. |
| Tool naming | `memory.semantic_observe` / `_retrieve` / `_update` / `_delete` | Consistency with existing `memory.observe` / `memory.retrieve` toolset. Same `_observe` verb for "user records something" parity. |
| Retrieve merging | Always returns `(project = ? OR scope = 'general')`, no scope filter on retrieval | The whole value of the design is that retrieval merges both buckets. Filtering would be a one-liner SQL change later if needed; YAGNI now. |
| Relationship to MEMORY.md | Complement, not replace | MEMORY.md is markdown loaded automatically; semantic memories are structured DB rows surfaced via tool call. Different load mechanisms; users can use either or both. |

## Approach

Single branch off `main` (after PR #25 merges), three logical commits:

| # | Commit | Files | Type |
|---|---|---|---|
| 1 | `feat(db): migration 0008 — semantic_memories table` | `better_memory/db/migrations/0008_semantic_memories.sql` (new), `tests/db/test_migration_0008.py` (new), `tests/db/test_schema.py` (extend) | Feature |
| 2 | `feat(semantic): SemanticMemoryService` | `better_memory/services/semantic.py` (new), `tests/services/test_semantic.py` (new) | Feature |
| 3 | `feat(mcp): memory.semantic_observe / _retrieve / _update / _delete tools` | `better_memory/mcp/server.py`, `tests/mcp/test_semantic_tools.py` (new) | Feature |

Commits are dependency-ordered: schema → service → MCP. CI green at each commit boundary.

## Commit 1 — Migration 0008

`better_memory/db/migrations/0008_semantic_memories.sql`:

```sql
-- Migration 0008: semantic memories table.
-- See docs/superpowers/specs/2026-05-04-semantic-memories-design.md.
--
-- User-stated facts/preferences. Distinct from observations (episodic,
-- recorded as work happens, fed to synthesis) and reflections
-- (LLM-distilled lessons). Same scope model as PR #25's reflections:
-- 'project' rows live in their own project bucket; 'general' rows
-- surface in every project's retrieval.

CREATE TABLE semantic_memories (
    id            TEXT PRIMARY KEY,
    content       TEXT NOT NULL,
    project       TEXT NOT NULL,
    scope         TEXT NOT NULL DEFAULT 'project'
                  CHECK(scope IN ('project', 'general')),
    created_at    TIMESTAMP NOT NULL,
    updated_at    TIMESTAMP NOT NULL
);

CREATE INDEX idx_semantic_memories_project
    ON semantic_memories(project, created_at DESC);

CREATE INDEX idx_semantic_memories_general
    ON semantic_memories(created_at DESC)
    WHERE scope = 'general';
```

**Test seed** (`tests/db/test_migration_0008.py`):

- Schema: `id`, `content`, `project`, `scope`, `created_at`, `updated_at` columns exist.
- CHECK constraint rejects `scope='invalid'` on INSERT.
- Default scope is 'project' on insert without explicit scope.
- Both indexes (`idx_semantic_memories_project`, `idx_semantic_memories_general`) exist; the `_general` one is partial.

**Schema-test extension** (`tests/db/test_schema.py`):

- Update `test_apply_migrations_is_idempotent` to expect `["0001"…"0008"]` (consistent with how 0006 and 0007 fix-ups landed).

## Commit 2 — `SemanticMemoryService`

`better_memory/services/semantic.py`:

```python
"""User-stated facts/preferences. Free-form content; project + general scope."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4


def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SemanticMemory:
    """Read model returned by retrieve."""
    id: str
    content: str
    project: str
    scope: str        # 'project' | 'general'
    created_at: str
    updated_at: str


class SemanticMemoryService:
    """User-stated facts/preferences.

    Connection ownership: writes within own commit envelope. No SAVEPOINT
    needed — single-row mutations are atomic on their own.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock or _default_clock

    def create(self, *, content: str, project: str, scope: str = "project") -> str:
        if scope not in ("project", "general"):
            raise ValueError(
                f"scope must be 'project' or 'general', got {scope!r}"
            )
        if not content.strip():
            raise ValueError("content must not be empty")
        memory_id = uuid4().hex
        now = self._clock().isoformat()
        self._conn.execute(
            """
            INSERT INTO semantic_memories
                (id, content, project, scope, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (memory_id, content, project, scope, now, now),
        )
        self._conn.commit()
        return memory_id

    def update_text(self, *, id: str, content: str) -> None:
        if not content.strip():
            raise ValueError("content must not be empty")
        now = self._clock().isoformat()
        cur = self._conn.execute(
            "UPDATE semantic_memories SET content = ?, updated_at = ? WHERE id = ?",
            (content, now, id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"semantic memory not found: {id}")
        self._conn.commit()

    def delete(self, *, id: str) -> None:
        """Idempotent — no error if id absent."""
        self._conn.execute("DELETE FROM semantic_memories WHERE id = ?", (id,))
        self._conn.commit()

    def list_for_project(self, *, project: str) -> list[SemanticMemory]:
        """Project rows + general-scope rows from any project, newest first."""
        rows = self._conn.execute(
            """
            SELECT id, content, project, scope, created_at, updated_at
              FROM semantic_memories
             WHERE project = ? OR scope = 'general'
             ORDER BY created_at DESC
            """,
            (project,),
        ).fetchall()
        return [
            SemanticMemory(
                id=r["id"], content=r["content"], project=r["project"],
                scope=r["scope"],
                created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for r in rows
        ]
```

**Tests** (`tests/services/test_semantic.py`):

| Test | Behavior |
|---|---|
| `test_create_with_default_scope` | content + project, no scope → row stored with scope='project' |
| `test_create_with_general_scope` | scope='general' explicit → row stored, surfaces in other projects' list |
| `test_create_rejects_invalid_scope` | scope='invalid' → ValueError before DB hit |
| `test_create_rejects_empty_content` | content='' or whitespace → ValueError |
| `test_update_text_changes_content_and_bumps_updated_at` | After update, content matches, updated_at > created_at |
| `test_update_text_raises_on_missing_id` | id not in DB → ValueError |
| `test_update_text_rejects_empty_content` | empty content → ValueError, row unchanged |
| `test_delete_removes_row` | Existing id → row gone after delete |
| `test_delete_is_idempotent_on_missing_id` | Missing id → no error |
| `test_list_for_project_orders_newest_first` | 3 memories with different created_at → returned newest-first |
| `test_list_for_project_includes_general_from_other_projects` | project='p1' rule + project='p2' general rule → list_for_project('p1') returns both |
| `test_list_for_project_excludes_other_projects_project_scoped` | project='p2' scope='project' → not in p1's list |

## Commit 3 — MCP tools

`better_memory/mcp/server.py`:

Four new tool definitions, registered alongside existing memory tools:

```python
{
    "name": "memory.semantic_observe",
    "description": (
        "Record a user-stated fact or preference. "
        "Distinct from memory.observe (episodic): semantic memories are "
        "user-asserted current truths, retrieved at session startup."
    ),
    "input_schema": {
        "type": "object",
        "required": ["content"],
        "properties": {
            "content": {"type": "string"},
            "scope": {
                "type": "string",
                "enum": ["project", "general"],
                "description": (
                    "'project' (default) for project-scoped rules; "
                    "'general' for cross-project workflow rules."
                ),
            },
        },
    },
}

{
    "name": "memory.semantic_retrieve",
    "description": (
        "Return user-stated facts/preferences for the current project, "
        "merged with all general-scope semantic memories. "
        "Flat list ordered most-recent first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": "Optional project override; defaults to cwd-derived.",
            },
        },
    },
}

{
    "name": "memory.semantic_update",
    "description": "Edit a semantic memory's content in place. Bumps updated_at.",
    "input_schema": {
        "type": "object",
        "required": ["id", "content"],
        "properties": {
            "id": {"type": "string"},
            "content": {"type": "string"},
        },
    },
}

{
    "name": "memory.semantic_delete",
    "description": "Remove a semantic memory. Idempotent — no error if id absent.",
    "input_schema": {
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
}
```

Each handler:
1. Constructs `SemanticMemoryService(memory_conn)`.
2. Resolves `project` via `project_name()` if not provided (matching existing pattern).
3. Calls the service method, serializes the result.

**Tests** (`tests/mcp/test_semantic_tools.py`): one test per tool exercising the wiring. The actual storage logic is covered by `test_semantic.py`; MCP tests verify schema validation, default scope, project resolution, and serialization.

## CLAUDE.md addendum (out of repo, separate edit)

Add to the user's `~/.claude/CLAUDE.md` "Retrieve: On Startup (MANDATORY)" section:

> - `mcp__better-memory__memory_semantic_retrieve` — returns user-stated facts/preferences for the current project + all general-scope semantic memories. A flat list, ordered most-recent first.

Add a new "Record: Semantic memories" subsection nearby:

> Use `mcp__better-memory__memory_semantic_observe` when the user states a durable fact or preference (e.g. "I prefer terse responses", "this codebase uses pytest, never unittest"). Set `scope='general'` for cross-project rules; default `'project'` otherwise. These persist in the better-memory DB and surface at every session start.

The CLAUDE.md edit is OUTSIDE this repo's PR — it lives in the user's global config. Documented here so the change is tracked alongside the design.

## Out-of-scope / future work

- **Search / FTS** on semantic memories. The startup retrieval brings the full per-project + general set into context (typically dozens of rows, not hundreds). Add FTS later if rows grow into the hundreds.
- **Audit log** of edits/deletes. Hard-delete loses history, but the value of "what did this preference USED to say" is low enough that it's YAGNI.
- **Migration helper for MEMORY.md content.** Users can manually `memory_semantic_observe` each rule they want to promote; a one-shot import script is simple if needed but also YAGNI for v1.
- **Categories / tags** as a structured field. The free-form `content` plus convention (e.g. starting a memory with `[workflow]` or `[fact]`) is enough; no DB change needed unless filtering becomes a real need.
- **UI surfacing** in the better-memory web UI. The current UI ("The Archive") shows observations and reflections; semantic memories could get their own panel later. Not blocking the core feature.
- **Source-tracking field** on records (e.g. who/what created it). All v1 semantic memories are user-stated; if future LLM-promoted records get added, a `source` enum can be added then.

## Spec self-review

- **Placeholders:** none. Every commit has files, code, and SQL specified concretely. Every test has a one-line behavior description.
- **Internal consistency:** ✓ — `scope` semantics are described identically across the decisions log, schema SQL, service code, MCP tool descriptions, and CLAUDE.md addendum. The `(project = ? OR scope = 'general')` retrieval clause appears identically in `list_for_project` and the prose.
- **Scope:** ✓ — three commits, ~150 LOC service + 50 LOC migration + ~250 LOC tests + ~80 LOC MCP wiring. Tractable for one implementation cycle.
- **Ambiguity:** the only field default to confirm is `scope='project'` — same as PR #25's `observations.scope` and `reflections.scope`. Consistency wins.
