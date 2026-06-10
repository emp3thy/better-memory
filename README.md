# better-memory

**better-memory gives your AI coding assistant a memory that grows with your project.** It remembers what worked and what didn't, picks up the preferences and conventions you care about, and when you have to point it back to something it should have used, it records that too — so the same lesson surfaces on its own next time. Every lesson is captured the moment a decision is made and distilled into short, relevance-ranked guidance the assistant pulls up on its own. The result: an assistant that *compounds* — getting sharper on your codebase the more you work together.

It's a memory layer for Claude Code that runs entirely on your machine. Your observations, distilled lessons, and knowledge base live in a local SQLite database — nothing is sent to a cloud service. Even the work of turning raw notes into durable lessons runs inside your own Claude Code session — no separate cloud LLM, no second subscription, no third party seeing your code.

## How it works

1. **As it works**, the assistant records what it tried and how it turned out — a fix that worked, an approach that failed, a preference you stated.
2. **Between sessions**, those raw notes are distilled into short, durable lessons — signal kept, noise dropped.
3. **Next session**, the relevant lessons are handed back to the assistant automatically, before it touches your code, so it starts already informed about your project.

## What you get

- **Even the failures pay off** — a botched approach is recorded just as carefully as a working fix, so the assistant turns a dead end into a *don't* the next session won't repeat.
- **The right lesson at the right time** — past lessons come back sorted into *do this*, *avoid this*, and *context*, weighted by how reliable each has proven.
- **Your standards, on hand** — drop your own conventions, language guides, and project docs into a knowledge base the assistant can search.
- **Zero ceremony** — once it's installed, capture is automatic: hooks inside Claude Code snapshot your session for you, with nothing to call or manage mid-task.
- **Nothing happens silently** — every change to memory is an append-only record you can audit.

## Requirements

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** for environment management
- **[Ollama](https://ollama.com/)** — a local model runner (installed separately) with the `nomic-embed-text` embedding model pulled. better-memory uses it *only* to turn text into search vectors; lesson-distilling is done by Claude, and your code never leaves your machine. Ollama is only required when `BETTER_MEMORY_EMBEDDINGS_BACKEND=ollama` (the default). Set it to `sqlite` to use a pure-SQL trigram-FTS5 backend with no model downloads.
- **Claude Code** installed

SQLite ships with Python; `sqlite-vec` is installed as a pip dependency — nothing else to set up.

## Storage backends

better-memory has two storage backends. Pick one:

| Backend | When to pick | Setup |
|---|---|---|
| **`sqlite`** (default) | Single-machine usage; full offline operation; no cloud cost. | None — works out of the box. |
| **`agentcore`** | Multi-machine syncing; managed extraction by AWS; team-shared memory bucket. | Requires AWS account with Bedrock AgentCore Memory enabled in `eu-west-2`. See [AgentCore setup](website/agentcore-setup.md). |

Switch backends via `BETTER_MEMORY_STORAGE_BACKEND=agentcore` (default: `sqlite`). The MCP server reads the env var at startup and dispatches accordingly. Switching is one-way today — there is no bulk migration tool (deferred; clean start in agentcore mode is the supported path).

`agentcore` mode needs the optional dependency group: `pip install 'better-memory[agentcore]'` (or `uv pip install '.[agentcore]'`). Sqlite-only installs skip boto3 entirely.

## Quick start

```bash
./scripts/setup.sh
```

The script:
1. Verifies Python ≥ 3.12 and uv.
2. Runs `uv sync` to build the venv.
3. Checks for Ollama; offers to install via `brew` / `apt` / `winget` if missing.
4. Pulls `nomic-embed-text`.
5. Creates `~/.better-memory/{spool,knowledge-base/...}`.
6. Auto-installs the MCP server registration into `~/.claude.json` and the three hooks (`session_bootstrap`, `observer`, `session_close`) into `~/.claude/settings.json` (idempotent; backups go to `~/.better-memory/install-backups/`).

If you'd rather inspect or hand-edit the config, see [Manual setup](#manual-setup) below.

> **Note:** `./scripts/setup.sh` writes both `~/.claude.json` and `~/.claude/settings.json` for you idempotently. The Manual setup section below is reference material — useful if you need to inspect or hand-edit the config, but not required for a normal install.

## Manual setup

If you'd rather do it by hand:

```bash
uv sync
mkdir -p ~/.better-memory/{spool,knowledge-base/{standards,languages,projects}}
ollama pull nomic-embed-text
```

Then add to `~/.claude.json` (user-scope MCP — create the file if it doesn't exist):

```json
{
  "mcpServers": {
    "better-memory": {
      "type": "stdio",
      "command": "/absolute/path/to/better-memory/.venv/bin/python",
      "args": ["-m", "better_memory.mcp"],
      "env": {
        "BETTER_MEMORY_HOME": "/absolute/path/to/your/home/.better-memory"
      }
    }
  }
}
```

And add hooks to `~/.claude/settings.json`:

A single SessionStart hook ships: `session_bootstrap` opens (or reuses) a background episode for the session and injects the project's curated context — both project-scoped and general-scope semantic memories plus all distilled reflections (`do` / `dont` / `neutral` buckets) — as `additionalContext` so Claude has prior memory available without needing to call any retrieval tool first. The hook does its work in-process against `memory.db`; if it fails for any reason, a fallback directive is injected instructing Claude to call `mcp__better-memory__memory_session_bootstrap` manually.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/.venv/bin/python -m better_memory.hooks.session_bootstrap"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/.venv/bin/python -m better_memory.hooks.observer",
            "async": true
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/.venv/bin/python -m better_memory.hooks.session_close",
            "async": true
          }
        ]
      }
    ]
  }
}
```

On Windows, point hooks at `.venv\Scripts\pythonw.exe` (no console flash) instead of `python.exe`.

Restart Claude Code. MCP servers don't hot-reload.

## Configuration

One env var roots the runtime filesystem layout:

| Variable | Default | Purpose |
|---|---|---|
| `BETTER_MEMORY_HOME` | `~/.better-memory` | Root dir for `memory.db`, `knowledge.db`, `spool/`, `knowledge-base/` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model (must produce 768-dim vectors) |
| `AUDIT_LOG_RETRIEVED` | `true` | Whether `memory.retrieve` writes per-result audit rows |
| `BETTER_MEMORY_EMBEDDINGS_BACKEND` | `ollama` | `ollama` (default) — local Ollama at `OLLAMA_HOST`; `sqlite` — pure-SQL trigram-FTS5 fusion, no model downloads and no in-memory state. |
| `BETTER_MEMORY_AUTO_PRUNE` | (unset = `false`) | When set to `1`, the auto-retention runner (which fires on `memory.retrieve`, throttled to once per 24h) ALSO hard-deletes archived observations older than 365 days. **Irreversible.** Default is archive-only (status flip, reversible). Opt in only if you actively want disk space reclaimed. |
| `BETTER_MEMORY_PROJECT` | unset | Force the project name for all calls in this process. Highest-priority project-resolution signal — overrides both the `.better-memory` file and the git-derived name. Designed for subprocess scoping (e.g. ralph's executor sets it per-iteration so subagent observations land in the PBI's target_repo regardless of the worktree's cwd). Empty/whitespace-only values are treated as unset. |

### Project-name override

Memory is bucketed by project name, resolved in this order (highest priority first):

1. **`BETTER_MEMORY_PROJECT` env var** — if set to a non-empty (after
   stripping) value, it is used verbatim for every call in the process.
   Empty/whitespace-only values fall through. Designed for subprocess
   scoping rather than interactive use.
2. **`.better-memory` override file** — if a `.better-memory` file
   exists in the cwd, its first non-empty stripped line is used
   verbatim. Checked only at the cwd, not at ancestors. Use this only
   for the rare case where the git-derived name isn't right.
3. **Git common dir** — `git rev-parse --git-common-dir` resolves to
   the main repo's `.git` directory even from inside a worktree, so
   all worktrees of the same repo share one project bucket
   automatically. The project name is the parent directory's name.
4. **`general`** — fallback when the cwd isn't inside a git tree (or
   git is unavailable).

If you need an override (renamed repo, multi-repo monolith, etc.):

```bash
echo "my-project" > .better-memory
```

This applies uniformly to knowledge search, observation writes/reads,
episode scoping, and the UI panel filter.

Note: this is a *file* in your repo root, not the data root directory
`~/.better-memory/` (set by `BETTER_MEMORY_HOME`). Different things
despite the shared name.

## MCP tools

The server registers 22 tools, grouped below. Full schemas are in [`website/mcp-tools.md`](website/mcp-tools.md) and live in `better_memory/mcp/tools.py`.

**Episodic memory** — observations the AI writes during a session.

| Tool | Purpose |
|---|---|
| `memory.observe(content, component?, theme?, trigger_type?, outcome?, tech?, scope?)` | Record an observation. Returns `{"id": ...}`. |
| `memory.retrieve(project?, tech?, phase?, polarity?, limit_per_bucket?)` | Distilled reflections in `do` / `dont` / `neutral` buckets. Drains spool first. |
| `memory.retrieve_observations(query?, component?, theme?, outcome?, episode_id?, project?, limit?)` | Raw-observation drill-down with hybrid FTS5 + vector search. |
| `memory.record_use(id, outcome?)` | Stamp reinforcement outcome on a memory after validation. |

**Semantic memory** — user-stated facts and preferences (current truth, not history).

| Tool | Purpose |
|---|---|
| `memory.semantic_observe(content, scope?)` | Record a user-stated fact. `scope='general'` for cross-project rules. |
| `memory.semantic_retrieve(project?)` | Project-scoped facts merged with all `general` ones. |
| `memory.semantic_update(id, content)` | Edit a semantic memory in place. |
| `memory.semantic_delete(id)` | Remove a semantic memory (idempotent). |

**Episodes** — bounded sessions of work; observations and reflections scope to one.

| Tool | Purpose |
|---|---|
| `memory.start_episode(goal, tech?)` | Open a foreground episode. Returns active episode id and `pending_synthesis` queue depth. |
| `memory.close_episode(outcome, close_reason?, summary?)` | Close the active episode. `outcome` ∈ `success` / `partial` / `abandoned` / `no_outcome`. |
| `memory.list_episodes(project?, outcome?, only_open?)` | List episodes for UI / introspection. |
| `memory.reconcile_episodes()` | List episodes still open from prior sessions. |

**Synthesis** — IDE-driven (Claude itself decides). See [`better-memory-synthesize`](.claude/skills/better-memory-synthesize/SKILL.md) skill.

| Tool | Purpose |
|---|---|
| `memory.synthesize_next_get_context(project?)` | One pending episode's full context for the IDE-LLM to act on. |
| `memory.synthesize_next_apply(episode_id, decision, project?)` | Atomically apply Claude's `new` / `augment` / `merge` / `ignore` decision. |

**Rating** — closed-loop reinforcement on top of `memory.record_use`. Session id is resolved server-side from `CLAUDE_SESSION_ID`; none of these tools take a session id.

| Tool | Purpose |
|---|---|
| `memory.credit(kind, id, class)` | Opportunistic per-tool-use credit. `class` ∈ `cited` / `shaped` / `misled` / `overlooked` (not `ignored`). Call immediately when a retrieved memory is actually used. |
| `memory.list_session_exposures()` | Unrated exposure rows for the current session. Read-only; used by the `rate-session-memories` skill. |
| `memory.apply_session_ratings(ratings)` | Atomic end-of-session batch rating. Each entry: `{kind, id, class}` with `class` ∈ `cited` / `shaped` / `ignored` / `misled` / `overlooked`. |

**Knowledge** — human-authored markdown corpus.

| Tool | Purpose |
|---|---|
| `knowledge.search(query, project?)` | BM25 search against the knowledge base. |
| `knowledge.list(project?)` | List indexed knowledge docs. |

**Operations**

| Tool | Purpose |
|---|---|
| `memory.session_bootstrap(source?, session_id?, cwd?)` | Open or reuse a session episode and inject project + general semantic memories and reflections as `additionalContext` markdown. Reflections capped at 20 per polarity bucket, ranked by usefulness then confidence. Mirrors the SessionStart hook; callable manually for recovery, testing, or post-`/clear` re-injection. |
| `memory.run_retention(retention_days?, prune?, prune_age_days?, dry_run?)` | Apply spec §9 retention rules; archive or hard-delete. |
| `memory.start_ui()` | Spawn or reuse the management UI; returns `{url, reused}`. |

## Skills

The `better_memory/skills/` directory contains four markdown skills the AI should load at the appropriate moment:

- `memory-retrieve.md` — before starting any task
- `memory-write.md` — at every decision point
- `memory-feedback.md` — when validation evidence arrives
- `session-close.md` — before wrapping up

Plus `CLAUDE.snippet.md` — paste into your project's `CLAUDE.md` to teach the AI about better-memory.

### Claude Code skills

Two skills live in `.claude/skills/` and are auto-symlinked into `~/.claude/skills/` by `./scripts/setup.sh` so Claude triggers them in any repo:

- **`better-memory-synthesize`** — walks Claude through the per-episode reflection synthesis loop (`memory.synthesize_next_get_context` → decide → `memory.synthesize_next_apply`). Fires when `memory.start_episode` reports `pending_synthesis.pending > 0` or when the user asks to consolidate.
- **`rate-session-memories`** — classifies exposed reflections / semantic memories at session end (`cited` / `shaped` / `ignored` / `misled` / `overlooked`) via `memory.list_session_exposures` + `memory.apply_session_ratings`. Triggered by a Stop-block directive from the `session_close` hook when unrated exposures remain.

For the synthesis skill, also add a section to `~/.claude/CLAUDE.md` telling Claude to invoke it when `mcp__better-memory__memory_start_episode` reports `pending_synthesis.pending > 0`. The rating skill fires from the hook directive and doesn't need an instruction line.

If the auto-symlink failed (e.g. Windows without developer mode or admin), symlink manually:

```bash
# Linux / macOS
ln -s "$PWD/.claude/skills/better-memory-synthesize" ~/.claude/skills/better-memory-synthesize
ln -s "$PWD/.claude/skills/rate-session-memories"    ~/.claude/skills/rate-session-memories

# Windows (PowerShell, run from this repo, as Administrator OR with developer mode)
New-Item -ItemType Junction -Path "$env:USERPROFILE\.claude\skills\better-memory-synthesize" -Target "$PWD\.claude\skills\better-memory-synthesize"
New-Item -ItemType Junction -Path "$env:USERPROFILE\.claude\skills\rate-session-memories"    -Target "$PWD\.claude\skills\rate-session-memories"
```

## Development

```bash
uv sync              # install deps
uv run pytest         # full suite (requires Ollama running for integration marker)
uv run pytest -m "not integration"   # unit tests only
uv run ruff check .   # lint
```

Run the MCP server standalone for manual poking:

```bash
uv run python -m better_memory.mcp
```

It speaks JSON-RPC over stdio — pipe `initialize` / `tools/list` / `tools/call` payloads in.

## Troubleshooting

**"Ollama unreachable" on startup.**
Make sure Ollama is running (`ollama serve` on macOS/Linux; the tray app on Windows) and that `nomic-embed-text` is pulled (`ollama pull nomic-embed-text`). The MCP server continues booting and serves `knowledge.*` tools, but `memory.observe` / `memory.retrieve` will error until Ollama is up.

**MCP server not appearing in Claude Code after editing `~/.claude.json`.**
MCP servers don't hot-reload. Restart Claude Code.

**Hooks not firing.**
Open `/hooks` once in Claude Code to reload the hook config — the settings watcher only watches directories that had a settings file when the session started. If that fails, restart.

**Spool files piling up.**
The spool drains on every `memory.retrieve` call. If you haven't retrieved in a long time, files accumulate — they're tiny JSON, not a concern. Bad files are moved to `spool/.quarantine/` so one corrupt file never blocks the drain.

**Windows console flashes on every tool call.**
Your hook command is using `python.exe`; switch to `.venv\Scripts\pythonw.exe`. The no-console variant still reads stdin pipes fine and won't flash a window.

## Architecture

See `docs/superpowers/specs/2026-04-06-better-memory-design.md` for the full design spec — four-layer epistemic hierarchy, hybrid search via FTS5 + sqlite-vec + RRF, reinforcement-weighted ranking, and the consolidation pipeline that lives in Plan 2.

## Observation lifecycle

Observations don't live forever. They flow through four states (`active` → `consumed_*` → `archived` → deleted) driven by two pipelines: **synthesis** (LLM-driven, runs from the UI button or automatically by `memory.start_episode`) and **retention** (manual; spec §13 explicitly defers auto-scheduling).

```mermaid
stateDiagram-v2
    [*] --> active: memory.observe
    active --> consumed_into_reflection: synthesis cites as source<br/>(_apply_new / _apply_augment)
    active --> consumed_without_reflection: synthesis ignore action<br/>(_apply_ignore)
    consumed_into_reflection --> archived: Retention Rule A<br/>only retired reflections,<br/>retired ≥ retention_days ago
    consumed_without_reflection --> archived: Retention Rule B<br/>status changed ≥ retention_days ago
    active --> archived: Retention Rule C<br/>episode outcome=no_outcome,<br/>ended ≥ retention_days ago
    consumed_into_reflection --> archived: Retention Rule C
    consumed_without_reflection --> archived: Retention Rule C
    archived --> [*]: prune (opt-in)<br/>archived ≥ prune_age_days
```

**Synthesis transitions** (no deletion, just status flips, atomic per run):

| Outcome | New status | Notes |
|---|---|---|
| Cited as a source for a NEW reflection | `consumed_into_reflection` | Linked into `reflection_sources`. |
| Cited as a NEW source for an EXISTING reflection (`augment`) | `consumed_into_reflection` | Linked into `reflection_sources`. |
| LLM marks as not reflection-worthy (`ignore`) | `consumed_without_reflection` | No link, just marked done. |
| Untouched by this run | (unchanged — usually `active`) | Picked up by the next synthesis run. |

`merge` does not change observation status: source reflection's `reflection_sources` rows move to the target, and the source's rating counters (`useful_count`, `times_misled`, `times_overlooked`) are summed onto the target with `last_*_at` timestamps taking the later of the two. Source is marked `superseded`.

**Retention** (`better_memory.services.retention.RetentionService.run`, default `retention_days=90`):

| Rule | Archives observations where … |
|---|---|
| **A** | linked only to *retired* reflections, oldest retirement ≥ retention_days old |
| **B** | `status='consumed_without_reflection'` and the status change was ≥ retention_days ago |
| **C** | belongs to an episode whose `outcome='no_outcome'` and ended ≥ retention_days ago |

**Prune** (opt-in, `RetentionService.run(..., prune=True, prune_age_days=N)`): hard-deletes `archived` rows older than `prune_age_days`. `dry_run=True` previews the count without deleting. Reflections are **never** auto-deleted.

Retention is invoked manually — there's no scheduler. Triggers today:
- `memory.run_retention` MCP tool, or
- Direct call from a script / cron / CI step.

## License

See [LICENSE](LICENSE).

## Management UI

Call the `memory.start_ui` MCP tool. It returns `{"url": ..., "reused": ...}`:
the URL is the loopback address the UI bound to; `reused` is `true` when an
existing live UI was returned and `false` when a fresh one was spawned. Open
the URL in a browser. Stdout and stderr from the UI subprocess are written
to `$BETTER_MEMORY_HOME/ui.log`.

The UI exits after 30 minutes of inactivity or when you click **Close UI**
in the header.

To launch it manually for debugging, the entry point is unchanged:

```bash
BETTER_MEMORY_HOME=~/.better-memory uv run python -m better_memory.ui
```
