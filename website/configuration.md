# Configuration

One environment variable roots the runtime filesystem layout. Everything else has sensible defaults that you can override per-process or via `~/.claude.json`'s `env` block.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BETTER_MEMORY_HOME` | `~/.better-memory` | Root directory for `memory.db`, `knowledge.db`, `spool/`, and `knowledge-base/` |
| `BETTER_MEMORY_WIRING_AUTOCHECK` | unset (`on`) | Session-start wiring drift autocheck (`better_memory.setup.autocheck.maybe_repair`, wired into the `session_bootstrap` hook): self-repairs `~/.claude.json`, `~/.claude/settings.json` hooks/env, the CLAUDE.md managed block, skill links, and the per-repo `post-commit` hook. Set to `off` (case-insensitive) to disable; any other value leaves it enabled. See [Architecture](architecture.md#self-managing-wiring). |
| `AUDIT_LOG_RETRIEVED` | `true` | Whether `memory.retrieve` writes per-result audit rows |
| `BETTER_MEMORY_AUTO_PRUNE` | unset (`false`) | When `1`, the auto-retention runner that fires on `memory.retrieve` (throttled to once per 24h) ALSO hard-deletes archived observations older than 365 days. **Irreversible.** Default is archive-only (status flip, reversible). Opt in only if you actively want disk space reclaimed. |
| `BETTER_MEMORY_PROJECT` | unset | Force the project name for all calls in this process. Highest-priority project-resolution signal — overrides both the `.better-memory` file and the git-derived name. Designed for subprocess scoping (e.g. ralph's executor sets it per-iteration so subagent observations land in the PBI's target_repo regardless of the worktree's cwd). Empty/whitespace-only values are treated as unset. |
| `BETTER_MEMORY_STORAGE_BACKEND` | unset | `sqlite` or `agentcore`. Explicit **override** of the storage backend. When unset, the backend is read from `$BETTER_MEMORY_HOME/settings.json` (written by `better-memory agentcore init`), falling back to `sqlite`. The env var always wins over settings.json. `agentcore` requires `pip install 'better-memory[agentcore]'` and a populated `agentcore.json` (see [AgentCore setup](agentcore-setup.md)). |
| `BETTER_MEMORY_TEST_AGENTCORE` | unset | `1` enables integration tests against real AWS. Default off; never set in CI. |
| `BETTER_MEMORY_TEST_AGENTCORE_REGION` | inherits `eu-west-2` | Override region used by integration tests. |
| `BETTER_MEMORY_CONTEXT_INJECT_MODE` | `both` | Contextual memory-injection hook trigger: `userprompt` (on prompt only), `pretool` (on tool calls only), `both` (default), or `off`. The `contextual_inject` hook surfaces curated memories (semantic + reflections) relevant to the current prompt / tool-input. |
| `BETTER_MEMORY_INJECT_MODE` | `legacy` | SessionStart bootstrap strategy: `legacy` (default, byte-identical to pre-deferred-injection behavior -- full bootstrap dump gated only by `BETTER_MEMORY_BOOTSTRAP_TOP_N`) or `deferred` (bootstrap renders only general-scope semantic memories plus a one-line index; project-scoped semantic memories and all reflections surface only through the contextual channel or on-demand `memory.retrieve`). Any value other than `deferred` resolves to `legacy`. See [Architecture](architecture.md#injection-strategies). |
| `BETTER_MEMORY_BOOTSTRAP_TOP_N` | `5` | Number of project-scoped semantic memories and reflections the SessionStart bootstrap renders in full (beyond that, a one-line index plus a retrieve affordance). `0` disables slimming and dumps everything in full. Only applies in `BETTER_MEMORY_INJECT_MODE=legacy`. |
| `BETTER_MEMORY_CONTEXT_MIN_HITS` | `2` | **Deprecated, unused.** `contextual_inject` no longer reads this. It was the old minimum-keyword-hits floor, superseded by the evidence gate (BM25 match, or a keyword-hit fallback when the BM25 leg is unavailable) described in [Architecture](architecture.md#injection-strategies). Kept only for back-compat resolution of stray env overrides. |
| `BETTER_MEMORY_CONTEXT_MAX_ITEMS` | `3` | Max number of memories `contextual_inject` injects per firing, after the evidence gate and ranking. |
| `BETTER_MEMORY_CONTEXT_REINJECT_TURNS` | `0` | Turns to wait before `contextual_inject` will re-inject a memory already seen this session. `0` means never re-inject (each memory surfaces at most once per session). A "turn" here means one firing of the `contextual_inject` hook, not one user prompt-response cycle: each user prompt counts as a turn, and PreToolUse counts as at most one more turn per session (it's latched to a single real firing -- see [Architecture](architecture.md#injection-strategies)). |

!!! note "Removed: `BETTER_MEMORY_AGENTCORE_REGION`"
    The region env var is gone. The AWS region is single-sourced from `agentcore.json` (written by `better-memory agentcore init --region <r>`); a differing env value could only produce a split-brain where events were written to a region the memories don't live in. If you had it exported, remove it — it is ignored. To change region, re-run `init` (see [AgentCore troubleshooting](troubleshooting/agentcore.md)).

## Project-name override

Memory is bucketed by project name, resolved in this order (highest priority first):

1. **`BETTER_MEMORY_PROJECT` env var** — if set to a non-empty (after stripping) value, it is used verbatim for every call in the process. Empty/whitespace-only values fall through. Designed for subprocess scoping rather than interactive use.
2. **`.better-memory` override file** — if a `.better-memory` file exists in the cwd, its first non-empty stripped line is used verbatim. Checked only at the cwd, not at ancestors. Reserved for the rare case where the git-derived name isn't right.
3. **Git common dir** — `git rev-parse --git-common-dir` resolves to the main repo's `.git` directory even from inside a worktree, so all worktrees of the same repo share one project bucket automatically. The project name is the parent directory's name.
4. **`general`** — fallback when the cwd isn't inside a git tree (or git is unavailable).

If you need an override (renamed repo, multi-repo monolith, etc.) drop a `.better-memory` file at the project root with a single line containing the desired project name:

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
├── memory.db              # observations, episodes, reflections, audit_log (+ agentcore_migration ledger in agentcore mode)
├── knowledge.db           # FTS5 index over knowledge-base/
├── settings.json          # optional; persists storage_backend selection (written by `agentcore init`)
├── agentcore.json         # agentcore mode only: memory IDs + region (written by `agentcore init`)
├── agentcore_migration.lock  # agentcore mode only: transient single-run lock held by `agentcore migrate`
├── spool/                 # hook payloads awaiting drain
│   └── .quarantine/       # malformed payloads (not deleted; kept for debug)
├── install-backups/       # timestamped .bak copies of settings.json / .claude.json / CLAUDE.md, written by `better-memory setup` and `doctor --fix` before each write
├── state/                 # per-session context_seen_<session_id>.json (contextual_inject dedup); wiring_fingerprint.json (session-start wiring autocheck cache); setup-apply.lock (apply() concurrency lock, 60s stale timeout)
└── knowledge-base/
    ├── standards/         # cross-project standards
    ├── languages/         # per-language conventions
    └── projects/          # per-project docs
        └── <project>/...
```

The two SQLite files are never shared between processes — the MCP server owns them for its lifetime, the management UI gets its own connection, and migrations run idempotently at startup.

## Hooks

Eight managed hook entries ship with better-memory, across seven hook modules (`contextual_inject` registers on two events), and read or write the filesystem layout above. They are installed automatically by `better-memory setup` (run directly, or via `./scripts/setup.sh` / `.\scripts\setup.ps1`), which merges them idempotently into `~/.claude/settings.json`; the legacy `python -m better_memory.cli.install_hooks` module is a deprecated shim that now just prints a warning and delegates to `setup`. The list below is reference material:

- **`better_memory.hooks.session_bootstrap`** (SessionStart) — opens or reuses a background episode for the session and injects the project's curated context as `additionalContext` for Claude's first turn. Shape depends on `BETTER_MEMORY_INJECT_MODE`: in `legacy` (default) it's project-scoped and general-scope semantic memories plus distilled reflections in `do` / `dont` / `neutral` buckets (retrieved up to 20 per bucket and ranked by a Wilson-score lower bound on positive-rated exposures, then confidence, then recency, with the bucket's last slot reserved for an under-rated memory - fewer than 3 rated exposures - when one exists; see [Architecture](architecture.md#self-rating-loop)), with only the top `BETTER_MEMORY_BOOTSTRAP_TOP_N` project-scoped items (default 5; general-scope semantic memories are always shown in full) rendered in full and the rest collapsed into a one-line index plus a `memory.retrieve` / `memory.retrieve_observations` affordance (`BETTER_MEMORY_BOOTSTRAP_TOP_N=0` restores the full dump). In `deferred` it's general-scope semantic memories in full plus a single one-line index -- project-scoped semantic memories and reflections surface only through `contextual_inject` or on-demand `memory.retrieve`. Runs in-process against `memory.db` (in agentcore mode it also opens the local `memory.db` — for the exposure ledger only, never memory content — while content reads route through the storage backend instead); failure-isolated: if bootstrap breaks, a fallback directive is injected and the failure is recorded in the `hook_errors` table. Also runs the wiring autocheck (`better_memory.setup.autocheck.maybe_repair`, see `BETTER_MEMORY_WIRING_AUTOCHECK` above) after bootstrap renders, appending at most one summary line — see [Architecture](architecture.md#self-managing-wiring).
- **`better_memory.hooks.observer`** (PostToolUse) — captures tool-call snapshots into `spool/` for later observation creation.
- **`better_memory.hooks.session_close`** (Stop) — writes a session-close marker into `spool/`; also emits the rating directive described in [Self-rating loop](architecture.md#self-rating-loop). In agentcore mode (resolved via the env var, else `settings.json`) it additionally fires one `CreateEvent(role=OTHER)` closure event against the current AgentCore session so the episodic strategy extracts within minutes; failure is logged to `hook_errors` and never blocks the marker write.
- **`better_memory.hooks.stop_sweep`** (Stop) — a second, independent Stop registration: prints a `systemMessage` reminding Claude to record any non-obvious observations from the session (per the CLAUDE.md mandatory triggers) before stopping.
- **`better_memory.hooks.contextual_inject`** (UserPromptSubmit, every prompt; PreToolUse, now every tool call, latched to one real firing per session) — scores the curated memory set (semantic + reflections) against the current prompt or tool-input through an evidence gate (a BM25 match against `reflection_fts` — replaced on agentcore by the backend's own server-side semantic-search evidence, `relevance_ranks` — or a keyword-hit fallback when those legs are structurally unavailable or an AWS lookup fails; `BETTER_MEMORY_CONTEXT_MIN_HITS` is deprecated and no longer consulted), caps survivors to `BETTER_MEMORY_CONTEXT_MAX_ITEMS`, and injects them as a `<project-memory>` block in `additionalContext`. A per-session seen-file dedups repeats (`BETTER_MEMORY_CONTEXT_REINJECT_TURNS` controls re-injection). Gated by `BETTER_MEMORY_CONTEXT_INJECT_MODE`. See [Architecture](architecture.md#injection-strategies) for detail.
- **`better_memory.hooks.commit_checkpoint`** (PreToolUse, matcher `Bash`, `if: "Bash(git commit*)"`) — fires only immediately before a `git commit` tool call and injects a reminder that CLAUDE.md's mandatory triggers require a `memory.observe` call first if the commit fixes a non-obvious bug, addresses reviewer feedback, or wraps a phase.
- **`better_memory.hooks.pre_compact`** (PreCompact) — injects a reminder to persist the in-flight task, key decisions, open questions, and relevant file paths to better-memory via `memory.observe` before context compaction discards them.

See [docs/hooks-setup.md](https://github.com/emp3thy/better-memory/blob/main/docs/hooks-setup.md) for the exact `~/.claude/settings.json` registration JSON (reference only — `better-memory setup` / `doctor --fix` write it for you).
