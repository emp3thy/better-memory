-- Migration 0007: cross-project (general) scope for reflections + observations.
-- Spec: docs/superpowers/specs/2026-05-03-episodic-synthesis-design.md (Commit 5)

ALTER TABLE observations
    ADD COLUMN scope TEXT NOT NULL DEFAULT 'project'
        CHECK(scope IN ('project','general'));

ALTER TABLE reflections
    ADD COLUMN scope TEXT NOT NULL DEFAULT 'project'
        CHECK(scope IN ('project','general'));

CREATE INDEX idx_reflections_scope_general
    ON reflections(updated_at DESC)
    WHERE scope = 'general';

-- Backfill: the workflow-rule observation recorded on 2026-05-04
-- ("always assign per-step confidence to a superpowers plan") was
-- created project-scoped to better-memory before this migration; flip
-- it to general so it surfaces in every project's memory_retrieve.
-- Idempotent: UPDATE on a non-existent id is a no-op.
UPDATE observations
   SET scope = 'general'
 WHERE id = '413d47550efd4adfa2c238d6ce5099f9';
