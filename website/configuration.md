# Configuration

One environment variable roots the runtime filesystem layout. Everything else has sensible defaults that you can override per-process or via `~/.claude.json`'s `env` block.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BETTER_MEMORY_HOME` | `~/.better-memory` | Root directory for `memory.db`, `knowledge.db`, `spool/`, and `knowledge-base/` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint (embeddings only — see [Architecture](architecture.md#synthesis-pipeline) for why synthesis no longer uses Ollama) |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model (must produce 768-dim vectors) |
| `AUDIT_LOG_RETRIEVED` | `true` | Whether `memory.retrieve` writes per-result audit rows |
| `BETTER_MEMORY_AUTO_PRUNE` | unset (`false`) | When `1`, the auto-retention runner that fires on `memory.retrieve` (throttled to once per 24h) ALSO hard-deletes archived observations older than 365 days. **Irreversible.** Default is archive-only (status flip, reversible). Opt in only if you actively want disk space reclaimed. |

## Project-name override

Memory is bucketed by project name, derived from the cwd's leaf directory name (`Path.cwd().name`). For situations where the leaf name isn't right — multiple worktrees of the same logical project, or a deeply-nested cwd — drop a `.better-memory` file at the project root with a single line containing the desired project name:

```bash
echo "my-project" > .better-memory
```

This applies uniformly to knowledge search, observation writes/reads, episode scoping, and the UI panel filter.

!!! note "File vs. directory"
    `.better-memory` here is a *file* in your repo root, not the data root directory `~/.better-memory/` (set by `BETTER_MEMORY_HOME`). Different things despite the shared name.

## Filesystem layout

Under `BETTER_MEMORY_HOME`:

```
.better-memory/
├── memory.db              # observations, episodes, reflections, audit_log
├── knowledge.db           # FTS5 index over knowledge-base/
├── spool/                 # hook payloads awaiting drain
│   └── .quarantine/       # malformed payloads (not deleted; kept for debug)
└── knowledge-base/
    ├── standards/         # cross-project standards
    ├── languages/         # per-language conventions
    └── projects/          # per-project docs
        └── <project>/...
```

The two SQLite files are never shared between processes — the MCP server owns them for its lifetime, the management UI gets its own connection, and migrations run idempotently at startup.

## Hooks

Three Claude Code hooks ship with better-memory and read or write the filesystem layout above. They are installed automatically by `./scripts/setup.sh` (which calls `python -m better_memory.cli.install_hooks` to merge them idempotently into `~/.claude/settings.json`). The list below is reference material:

- **`better_memory.hooks.session_bootstrap`** (SessionStart) — opens or reuses a background episode for the session and injects the project's curated context (project-scoped and general-scope semantic memories plus all distilled reflections — `do` / `dont` / `neutral` buckets) as `additionalContext` for Claude's first turn. Runs in-process against `memory.db`; failure-isolated: if bootstrap breaks, a fallback directive is injected and the failure is recorded in the `hook_errors` table.
- **`better_memory.hooks.observer`** (PostToolUse) — captures tool-call snapshots into `spool/` for later observation creation.
- **`better_memory.hooks.session_close`** (Stop) — writes a session-close marker into `spool/`.

See [`README.md`](https://github.com/emp3thy/better-memory/blob/main/README.md#manual-setup) for the exact `~/.claude/settings.json` registration JSON.
