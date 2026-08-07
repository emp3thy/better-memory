-- Migration 0017: display-text snapshot on rated exposures.
--
-- Display-text snapshot captured at exposure-record time. Nullable: rows
-- written before this migration (and callers with no display in hand) leave
-- it NULL and the read path falls back to joining reflections.title /
-- semantic_memories.content — which only resolves local-table ids, not
-- agentcore's AWS-minted mem-<uuid> ids. See
-- docs/superpowers/specs/2026-08-06-rate-directive-display-design.md.

ALTER TABLE session_memory_exposure ADD COLUMN display TEXT;
