-- Migration 0019: tool-call triggers on memories; trigger-channel diagnostics.
--
-- ``triggers`` holds a JSON array of trigger strings (see
-- better_memory/services/triggers.py for the grammar) or NULL when a memory
-- has none. Rows without triggers are never served by the PreToolUse
-- trigger channel. See
-- docs/superpowers/specs/2026-10-05-just-in-time-serving-design.md §2.

ALTER TABLE reflections ADD COLUMN triggers TEXT;
ALTER TABLE semantic_memories ADD COLUMN triggers TEXT;

INSERT INTO rating_diagnostics (metric, value) VALUES ('contextual_suppressed_nonhuman', 0);
INSERT INTO rating_diagnostics (metric, value) VALUES ('trigger_fired', 0);
INSERT INTO rating_diagnostics (metric, value) VALUES ('trigger_injected', 0);
