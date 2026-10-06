-- Migration 0020: widen session_memory_exposure.source CHECK to include
-- 'trigger' (the PreToolUse trigger channel; see
-- docs/superpowers/specs/2026-10-05-just-in-time-serving-design.md §1).
-- SQLite cannot ALTER a CHECK constraint, so the table is recreated with
-- every column added since migration 0012 (via_exploration, evidence,
-- display). No table holds a foreign key into session_memory_exposure.

CREATE TABLE session_memory_exposure_new (
    session_id      TEXT NOT NULL,
    memory_kind     TEXT NOT NULL CHECK(memory_kind IN ('reflection', 'semantic')),
    memory_id       TEXT NOT NULL,
    exposed_at      TEXT NOT NULL,
    source          TEXT NOT NULL CHECK(source IN ('bootstrap', 'retrieve', 'contextual', 'trigger')),
    rated_at        TEXT,
    classification  TEXT CHECK(classification IN
                      ('cited', 'shaped', 'ignored', 'misled', 'overlooked')),
    via_exploration INTEGER NOT NULL DEFAULT 0,
    evidence        TEXT,
    display         TEXT,
    PRIMARY KEY (session_id, memory_kind, memory_id, exposed_at)
);

INSERT INTO session_memory_exposure_new
    (session_id, memory_kind, memory_id, exposed_at, source, rated_at,
     classification, via_exploration, evidence, display)
SELECT
    session_id, memory_kind, memory_id, exposed_at, source, rated_at,
    classification, via_exploration, evidence, display
FROM session_memory_exposure;

DROP TABLE session_memory_exposure;
ALTER TABLE session_memory_exposure_new RENAME TO session_memory_exposure;

CREATE INDEX idx_sme_session_unrated
    ON session_memory_exposure(session_id) WHERE rated_at IS NULL;
CREATE INDEX idx_sme_memory
    ON session_memory_exposure(memory_kind, memory_id);
