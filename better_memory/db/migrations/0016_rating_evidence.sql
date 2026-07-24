-- Migration 0016: evidence line on rated exposures.
--
-- Rating variance is the noise floor under every ranking signal: identical
-- memory sets were rated `shaped` in one session and `ignored` in another
-- (2026-07 A/B runs). Non-ignored ratings now carry a one-line evidence
-- statement (what the memory changed, or a quote), enforced by
-- MemoryRatingService and surfaced in the UI drawers. Audit-only: no
-- scoring reads this column.
--
-- Distinct from reflections.evidence_count, which counts synthesis source
-- observations and has nothing to do with rating evidence.

ALTER TABLE session_memory_exposure ADD COLUMN evidence TEXT;
