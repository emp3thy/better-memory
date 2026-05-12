-- Migration 0009: memory rating.
--
-- Closed-loop self-rating of reflections and semantic memories.
-- See docs/superpowers/specs/2026-05-10-memory-rating-design.md.
--
-- session_memory_exposure: one row per (session, kind, memory_id, exposed_at).
-- Composite PK includes exposed_at so the same memory exposed at bootstrap
-- AND again mid-session lands as two rows (different timestamps, both kept
-- for audit). rated_at IS NULL gates re-rating.

CREATE TABLE session_memory_exposure (
    session_id     TEXT NOT NULL,
    memory_kind    TEXT NOT NULL CHECK(memory_kind IN ('reflection', 'semantic')),
    memory_id      TEXT NOT NULL,
    exposed_at     TEXT NOT NULL,
    source         TEXT NOT NULL CHECK(source IN ('bootstrap', 'retrieve')),
    rated_at       TEXT,
    classification TEXT CHECK(classification IN
                     ('cited', 'shaped', 'ignored', 'misled')),
    PRIMARY KEY (session_id, memory_kind, memory_id, exposed_at)
);

CREATE INDEX idx_sme_session_unrated
    ON session_memory_exposure(session_id) WHERE rated_at IS NULL;
CREATE INDEX idx_sme_memory
    ON session_memory_exposure(memory_kind, memory_id);

-- Rating counters on the memory rows themselves. useful_count is bumped
-- on 'cited' / 'shaped' classifications; times_misled is bumped on 'misled'.
-- 'ignored' is a no-op on the memory row (only stamps the exposure).

ALTER TABLE reflections ADD COLUMN useful_count   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reflections ADD COLUMN last_useful_at TEXT;
ALTER TABLE reflections ADD COLUMN times_misled   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reflections ADD COLUMN last_misled_at TEXT;

ALTER TABLE semantic_memories ADD COLUMN useful_count   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE semantic_memories ADD COLUMN last_useful_at TEXT;
ALTER TABLE semantic_memories ADD COLUMN times_misled   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE semantic_memories ADD COLUMN last_misled_at TEXT;

-- Diagnostics counters surfaced on the /diagnostics page. Currently one
-- counter: session_id_missing (bumped from any service exposure-write
-- path when CLAUDE_SESSION_ID is unset).

CREATE TABLE rating_diagnostics (
    metric     TEXT PRIMARY KEY,
    value      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);

INSERT INTO rating_diagnostics (metric, value) VALUES ('session_id_missing', 0);
