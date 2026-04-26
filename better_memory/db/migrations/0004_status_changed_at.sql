-- Phase 11: track when an observation's status last changed.
--
-- Needed for spec §9 retention rule "archived 90 days after consumption":
-- created_at is the wrong clock (synthesis can run long after creation).
--
-- Backfills existing rows from created_at — slightly conservative for
-- consumed_* rows (treats them as having been consumed at creation
-- time, which over-archives mildly) but safe: if a row is already
-- marked consumed, retention is the right destination.

ALTER TABLE observations ADD COLUMN status_changed_at TEXT;

UPDATE observations
SET status_changed_at = created_at
WHERE status_changed_at IS NULL;

CREATE INDEX idx_observations_status_changed_at
    ON observations(status, status_changed_at);
