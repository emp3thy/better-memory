-- better-memory migration 0005: phase C — background lifecycle.
--
-- retention_runs: append-only audit trail for RetentionScheduler runs.
-- One row per run; the most recent row's run_at is consulted by the
-- 24h "too soon" guard.
--
-- hook_errors: best-effort visibility into hook failures. Hooks must
-- never raise (they catch BaseException); this table records what
-- happened so a failing hook is no longer invisible. Surfaced via
-- the /diagnostics UI page.

CREATE TABLE retention_runs (
    id INTEGER PRIMARY KEY,
    run_at TEXT NOT NULL,
    archived_via_retired_reflection INTEGER NOT NULL,
    archived_via_consumed_without_reflection INTEGER NOT NULL,
    archived_via_no_outcome_episode INTEGER NOT NULL,
    pruned INTEGER NOT NULL,
    triggered_by TEXT NOT NULL  -- 'retrieve' | 'manual'
);

CREATE INDEX idx_retention_runs_run_at
    ON retention_runs(run_at DESC);

CREATE TABLE hook_errors (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    hook_name TEXT NOT NULL,
    exception_type TEXT NOT NULL,
    exception_message TEXT,
    traceback TEXT,
    cwd TEXT
);

CREATE INDEX idx_hook_errors_created_at
    ON hook_errors(created_at DESC);
