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
