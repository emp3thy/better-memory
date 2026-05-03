# Configuration

One environment variable roots the runtime filesystem layout. Everything else has sensible defaults that you can override per-process or via `~/.claude.json`'s `env` block.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BETTER_MEMORY_HOME` | `~/.better-memory` | Root directory for `memory.db`, `knowledge.db`, `spool/`, and `knowledge-base/` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model (must produce 768-dim vectors) |
| `CONSOLIDATE_MODEL` | `llama3` | LLM used by the synthesis pipeline |
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
