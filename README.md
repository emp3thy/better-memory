# better-memory

**better-memory gives your AI coding assistant a memory that grows with your project.** It remembers what worked and what didn't, picks up the preferences and conventions you care about, and when you have to point it back to something it should have used, it records that too — so the same lesson surfaces on its own next time. Every lesson is captured the moment a decision is made and distilled into short, relevance-ranked guidance the assistant pulls up on its own. The result: an assistant that *compounds* — getting sharper on your codebase the more you work together.

It's a memory layer for Claude Code that (with the default sqlite backend) runs entirely on your machine. Your observations, distilled lessons, and knowledge base live in a local SQLite database — nothing is sent to a cloud service. Even the work of turning raw notes into durable lessons runs inside your own Claude Code session — no separate cloud LLM, no second subscription, no third party seeing your code. (An optional [AWS-backed backend](website/agentcore-setup.md) exists for teams that want cloud-managed memory.)

## How it works

1. **As it works**, the assistant records what it tried and how it turned out — a fix that worked, an approach that failed, a preference you stated.
2. **Between sessions**, those raw notes are distilled into short, durable lessons — signal kept, noise dropped.
3. **Next session**, lessons surface exactly when they become relevant: your standing rules inject at session start, and everything else arrives as you work — each prompt and the session's first tool call are scored against the memory store (text match + semantic similarity + track record), and only memories with real evidence of relevance inject. No firehose, no guessing.
4. **Every serve gets scored.** At session end the assistant rates each surfaced memory — and any non-trivial rating requires a one-line receipt (what the memory changed, or a quote). Hit rates drive the ranking: memories that keep helping rise, memories that keep getting ignored sink, and brand-new lessons get a reserved trial slot to earn their score.

## What you get

- **Even the failures pay off** — a botched approach is recorded just as carefully as a working fix, so the assistant turns a dead end into a *don't* the next session won't repeat.
- **The right lesson at the right time** — past lessons come back sorted into *do this*, *avoid this*, and *context*, ranked by a Wilson-score hit rate (how often each memory actually helped, out of how often it was shown) fused with text and semantic relevance to what you're doing right now.
- **Ratings with receipts** — when the assistant claims a memory helped, it must say how, in one line; those evidence lines are stored and browsable per-memory in the management UI.
- **Your standards, on hand** — drop your own conventions, language guides, and project docs into a knowledge base the assistant can search.
- **Zero ceremony** — once it's installed, capture is automatic: hooks inside Claude Code snapshot your session for you, with nothing to call or manage mid-task.
- **Nothing happens silently** — every change to memory is an append-only record you can audit.

## Requirements

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** for environment management — the bootstrap scripts below install it for you if it's missing.
- **Claude Code** installed

SQLite ships with Python; `sqlite-vec` is installed as a pip dependency — nothing else to set up.

better-memory's default embeddings backend (`BETTER_MEMORY_EMBEDDINGS_BACKEND=ollama`) needs [Ollama](https://ollama.com/) running locally with the `nomic-embed-text` model pulled (`ollama pull nomic-embed-text`) — the bootstrap scripts no longer install or pull this for you. Set `BETTER_MEMORY_EMBEDDINGS_BACKEND=sqlite` instead to skip Ollama entirely (pure-SQL trigram-FTS5 backend, no model downloads).

## Storage backends

better-memory has two storage backends. Pick one:

| Backend | When to pick | Setup |
|---|---|---|
| **`sqlite`** (default) | Single-machine usage; full offline operation; no cloud cost. | None — works out of the box. |
| **`agentcore`** | Multi-machine syncing; managed extraction by AWS; team-shared memory bucket. | Requires an AWS account with Bedrock AgentCore Memory available in your chosen region (`init --region` defaults to `eu-west-2`). See [AgentCore setup](website/agentcore-setup.md). |

Switching to agentcore is done by `better-memory agentcore init`, which provisions the AWS memories and activates the backend by writing `{"storage_backend": "agentcore"}` to `$BETTER_MEMORY_HOME/settings.json`. The MCP server, hooks, and CLI all resolve the backend the same way: `BETTER_MEMORY_STORAGE_BACKEND` env var if set (always wins), else `settings.json`, else `sqlite`. To revert, remove the `storage_backend` key from `settings.json` or set the env var to `sqlite`. To carry existing sqlite memory across, `better-memory agentcore migrate` bulk-copies your distilled reflections and semantic memories into the AgentCore memories (idempotent, re-runnable; `--dry-run` previews the plan). Migration and activation are independent — `migrate` only writes records to AWS and never flips the backend, so use `init` (or set `storage_backend` yourself) to actually switch. See [AgentCore setup](website/agentcore-setup.md).

`agentcore` mode needs the optional dependency group: `pip install 'better-memory[agentcore]'` (or `uv pip install '.[agentcore]'`). Sqlite-only installs skip boto3 entirely.

## Quick start

**Interactive** — clone the repo and run the bootstrap script for your OS from a terminal:

```bash
# macOS / Linux
git clone https://github.com/emp3thy/better-memory
cd better-memory
./scripts/setup.sh
```

```powershell
# Windows (PowerShell)
git clone https://github.com/emp3thy/better-memory
cd better-memory
.\scripts\setup.ps1
```

Each script: installs `uv` if it isn't already on PATH (macOS/Linux: the official installer via `curl | sh`; Windows: `winget install --id=astral-sh.uv`, falling back to the `irm https://astral.sh/uv/install.ps1 | iex` installer if winget isn't available), runs `uv sync`, then runs `uv run better-memory setup` — which creates `~/.better-memory/{spool,state,install-backups,knowledge-base/{standards,languages,projects}}`, and installs/repairs the MCP server registration (`~/.claude.json`), all eight managed hooks plus the `BETTER_MEMORY_INJECT_MODE` env var (`~/.claude/settings.json`), the managed CLAUDE.md block, and a skill symlink for every skill in the repo's `.claude/skills/` (falling back to Windows directory junctions if symlink privilege is unavailable) — idempotently; anything overwritten is backed up first to `~/.better-memory/install-backups/`.

**Scripted / CI** — the same steps, called directly (skips the bootstrap script's `uv`-install step, e.g. when `uv` is already provisioned by the image):

```bash
uv sync
uv run better-memory setup
```

Both paths are fully idempotent and non-interactive — neither prompts for input, so either is safe to re-run any time, including from automation. Restart Claude Code afterward; MCP servers and hook registrations don't hot-reload.

If you'd rather inspect what gets written or hand-edit the config, see [Manual setup](#manual-setup) below; for checking or repairing drift later, see [Doctor](#doctor).

## Manual setup

`better-memory setup` (invoked by both bootstrap scripts above) writes everything below automatically and idempotently — this section is reference material for inspecting or hand-editing the config, not a required step.

```bash
uv sync
uv run better-memory setup
```

That single command:
- Creates `~/.better-memory/{spool,state,install-backups,knowledge-base/{standards,languages,projects}}` and a default `settings.json` if none exists.
- Registers the MCP server in `~/.claude.json` (creating the file if needed).
- Installs eight managed hooks plus the `BETTER_MEMORY_INJECT_MODE` env var into `~/.claude/settings.json`: `session_bootstrap` (SessionStart) opens/reuses the session's background episode and injects curated context — project-scoped and general-scope semantic memories plus distilled reflections (`do` / `dont` / `neutral` buckets) — as `additionalContext`, with only the top `BETTER_MEMORY_BOOTSTRAP_TOP_N` project-scoped items (default 5) rendered in full; `observer` (PostToolUse, `Write|Edit|Bash`) captures tool-call snapshots into the spool; `session_close` + `stop_sweep` (Stop) drive the end-of-session rating sweep and a reminder to record non-obvious observations; `contextual_inject` (UserPromptSubmit every prompt, and PreToolUse unscoped but latched to one firing per session) surfaces memories relevant to the current prompt/tool-input — see [Configuration](website/configuration.md) and [Architecture](website/architecture.md#injection-strategies) for how it scores, floors, and dedups candidates; `commit_checkpoint` (PreToolUse, gated to `Bash(git commit*)`) reminds Claude to record an observation before committing; `pre_compact` (PreCompact) reminds Claude to persist state before context compaction.
- Splices the managed CLAUDE.md block (the retrieve/reinforce/synthesize/record protocol) into `~/.claude/CLAUDE.md` between `<!-- BEGIN better-memory (managed) -->` / `<!-- END -->` markers, leaving the rest of your CLAUDE.md untouched.
- Symlinks (or, without Windows symlink privilege, directory-junctions) every Claude Code skill found in the repo's `.claude/skills/`, and prunes links whose repo skill was removed — see [Claude Code skills](#claude-code-skills) below.

For the exact JSON shapes it writes — useful if you're hand-editing `~/.claude/settings.json` or `~/.claude.json` — see [docs/hooks-setup.md](docs/hooks-setup.md), which documents each hook's event, matcher, and async/sync-ness as reference material only; `better-memory setup` / `doctor --fix` are the source of truth, not the doc.

Restart Claude Code. MCP servers don't hot-reload.

## Doctor

`better-memory doctor` checks the same managed wiring `setup` installs, without writing anything unless you pass `--fix`:

```bash
uv run better-memory doctor            # human-readable drift report; exit code 1 if any drift, 0 if clean
uv run better-memory doctor --fix      # repairs drift in place (same engine as `setup`); always exits 0
uv run better-memory doctor --json     # machine-readable {"drift": [...]}; ignored if --fix is also passed
```

- **Session-start autocheck.** Every Claude Code session start, the `session_bootstrap` hook runs a near-zero-cost version of the same check in-process — cached against a fingerprint of the desired state plus the target files' mtimes, so a clean machine costs nothing on the happy path — and self-repairs any drift it finds, appending a one-line summary to the session's startup context. It also installs the per-repo `post-commit` hook (see [docs/hooks-setup.md](docs/hooks-setup.md#post-commit-hook-opt-in-episode-close)) the first time it sees a git repo that doesn't have one yet, honoring a custom `core.hooksPath`, chaining after an existing plain-`sh` script, and warning — never overwriting — anything it doesn't recognize. Set `BETTER_MEMORY_WIRING_AUTOCHECK=off` to disable the autocheck entirely.
- **Moved or renamed the repo?** The machine-level wiring embeds this checkout's absolute path (interpreters, skill-symlink targets). After moving `better-memory`, run `uv run better-memory doctor --fix` from the new location to repoint everything.
- **`install_hooks` is deprecated.** `python -m better_memory.cli.install_hooks` still runs, but only prints a deprecation warning and delegates to `better-memory setup`; its old flags (`--venv-py`, `--venv-pyw`, `--home`) are accepted and ignored, since machine params are auto-detected now. Use `better-memory setup` / `doctor` directly.

## Configuration

One env var roots the runtime filesystem layout:

| Variable | Default | Purpose |
|---|---|---|
| `BETTER_MEMORY_HOME` | `~/.better-memory` | Root dir for `memory.db`, `knowledge.db`, `spool/`, `knowledge-base/` |
| `BETTER_MEMORY_WIRING_AUTOCHECK` | (unset = on) | Session-start wiring drift autocheck: self-repairs `~/.claude.json`, `~/.claude/settings.json` hooks/env, the CLAUDE.md managed block, skill links, and the per-repo post-commit hook. Set to `off` (case-insensitive) to disable; any other value leaves it enabled. See [Doctor](#doctor). |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model (must produce 768-dim vectors) |
| `AUDIT_LOG_RETRIEVED` | `true` | Whether `memory.retrieve` writes per-result audit rows |
| `BETTER_MEMORY_EMBEDDINGS_BACKEND` | `ollama` | `ollama` (default) — local Ollama at `OLLAMA_HOST`; `sqlite` — pure-SQL trigram-FTS5 fusion, no model downloads and no in-memory state. |
| `BETTER_MEMORY_STORAGE_BACKEND` | unset | `sqlite` or `agentcore`. Explicit override of the storage backend; when unset, `$BETTER_MEMORY_HOME/settings.json` (written by `better-memory agentcore init`) decides, falling back to `sqlite`. The env var always wins over settings.json. |
| `BETTER_MEMORY_AUTO_PRUNE` | (unset = `false`) | When set to `1`, the auto-retention runner (which fires on `memory.retrieve`, throttled to once per 24h) ALSO hard-deletes archived observations older than 365 days. **Irreversible.** Default is archive-only (status flip, reversible). Opt in only if you actively want disk space reclaimed. |
| `BETTER_MEMORY_PROJECT` | unset | Force the project name for all calls in this process. Highest-priority project-resolution signal — overrides both the `.better-memory` file and the git-derived name. Designed for subprocess scoping (e.g. ralph's executor sets it per-iteration so subagent observations land in the PBI's target_repo regardless of the worktree's cwd). Empty/whitespace-only values are treated as unset. |
| `BETTER_MEMORY_INJECT_MODE` | `legacy` (config default; `deferred` is the recommended and currently-deployed setting) | `deferred`: SessionStart injects only general-scope standing rules + a one-line index; everything else surfaces via the contextual channel as it becomes relevant. `legacy`: the original full bootstrap dump. Unknown values coerce to `legacy`. |
| `BETTER_MEMORY_CONTEXT_INJECT_MODE` | `both` | Contextual memory-injection hook trigger: `userprompt`, `pretool`, `both` (default), or `off`. PreToolUse fires once per session (any tool), latched. |
| `BETTER_MEMORY_CONTEXT_VEC_FLOOR` | `0.55` | Cosine floor for the contextual channel's vector-evidence leg. A memory injects only with a text match or a cosine ≥ floor; calibrated precision-first (see the deferred-injection spec). |
| `BETTER_MEMORY_BOOTSTRAP_TOP_N` | `5` | Legacy-mode only: project-scoped items the SessionStart bootstrap renders in full; the rest collapse into a one-line index. `0` = full dump. |
| `BETTER_MEMORY_CONTEXT_MIN_HITS` | `2` | **Deprecated, unused** — superseded by the evidence gate (BM25 / vector floor). Kept for back-compat only. |
| `BETTER_MEMORY_CONTEXT_MAX_ITEMS` | `3` | Max memories the contextual-injection hook injects per firing. |
| `BETTER_MEMORY_CONTEXT_REINJECT_TURNS` | `0` | Turns before a contextually-injected memory can be re-injected. `0` = never re-inject. A turn is one firing of the `contextual_inject` hook (each user prompt, plus each matched tool call in mode `both`), not one user prompt-response cycle. |

See [Configuration](website/configuration.md) for the full table and injection-tuning detail.

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

The server registers 22 tools, grouped below. Full schemas are in [`website/mcp-tools.md`](website/mcp-tools.md) and live in `better_memory/mcp/tools.py`. (In agentcore mode the two synthesis tools, the four episode tools, and `memory.run_retention` are hidden from the advertised list — 15 tools; the memory-data tools dispatch to AWS instead of SQLite.)

**Episodic memory** — observations the AI writes during a session.

| Tool | Purpose |
|---|---|
| `memory.observe(content, component?, theme?, trigger_type?, outcome?, tech?, scope?)` | Record an observation. Returns `{"id": ...}`. |
| `memory.retrieve(query?, project?, tech?, phase?, polarity?, limit_per_bucket?)` | Distilled reflections in `do` / `dont` / `neutral` buckets, ranked by a Wilson-score hit-rate prior; pass `query` (a plain-language task description) to fuse in BM25 + vector relevance. One shortlist slot per bucket is reserved for an under-rated memory so new lessons earn a score. Drains spool first. |
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

**Rating** — closed-loop reinforcement on top of `memory.record_use`. Session id is resolved server-side from `CLAUDE_SESSION_ID` by default; `list_session_exposures` and `apply_session_ratings` also accept an explicit `session_id` (from the RATE_MEMORIES directive's `Session:` line), which overrides env/marker resolution. `memory.credit` takes no session id.

| Tool | Purpose |
|---|---|
| `memory.credit(kind, id, class, evidence)` | Opportunistic per-tool-use credit. `class` ∈ `cited` / `shaped` / `misled` / `overlooked` (not `ignored`). `evidence` is required: one line — what the memory changed, or a quote. If you can't write one, the memory was ignored; don't call credit. |
| `memory.list_session_exposures(session_id?)` | Unrated exposure rows for the current session. Read-only; used by the `rate-session-memories` skill. |
| `memory.apply_session_ratings(ratings, session_id?)` | Atomic end-of-session batch rating. Each entry: `{kind, id, class, evidence?}` — evidence is required for every non-`ignored` class (write the evidence line first; nothing to point at means the class is `ignored`). Violating batches are rejected whole. |

**Knowledge** — human-authored markdown corpus.

| Tool | Purpose |
|---|---|
| `knowledge.search(query, project?)` | BM25 search against the knowledge base. |
| `knowledge.list(project?)` | List indexed knowledge docs. |

**Operations**

| Tool | Purpose |
|---|---|
| `memory.session_bootstrap(source?, session_id?, cwd?)` | Open or reuse a session episode and inject startup context as `additionalContext` markdown. In `deferred` mode (recommended): general-scope standing rules in full plus a one-line index — everything else arrives contextually. In `legacy` mode: the original render (up to 20 reflections/bucket, Wilson-ranked, top `BETTER_MEMORY_BOOTSTRAP_TOP_N` in full). Mirrors the SessionStart hook, except the hook additionally runs the wiring autocheck (`better_memory.setup.autocheck.maybe_repair`) after bootstrap renders, appending at most one summary line — see [Doctor](#doctor); calling this tool directly does not run the autocheck. Callable manually for recovery, testing, or post-`/clear` re-injection. |
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

The skills in `.claude/skills/` (every directory with a `SKILL.md`) are enumerated and auto-symlinked into `~/.claude/skills/` by `better-memory setup` (run directly, or via either bootstrap script) so Claude triggers them in any repo — falling back to a Windows directory junction automatically when symlink privilege isn't available. A skill added to the repo is picked up on the next session's autocheck with no code change; a skill removed from the repo has its user-level link pruned. Currently:

- **`better-memory-synthesize`** — walks Claude through the per-episode reflection synthesis loop (`memory.synthesize_next_get_context` → decide → `memory.synthesize_next_apply`). Fires when `memory.start_episode` reports `pending_synthesis.pending > 0` or when the user asks to consolidate.
- **`rate-session-memories`** — classifies exposed reflections / semantic memories at session end (`cited` / `shaped` / `ignored` / `misled` / `overlooked`) via `memory.list_session_exposures` + `memory.apply_session_ratings`. Triggered by a Stop-block directive from the `session_close` hook when unrated exposures remain.
- **`start-better-memory-ui`** — launches the management UI directly (the `memory.start_ui` MCP tool is a stub; this skill runs `python -m better_memory.ui` itself). Triggered by phrasing like "start ui" / "launch the better-memory UI".

`better-memory setup` also writes the managed CLAUDE.md block that tells Claude when to invoke `better-memory-synthesize` (the `pending_synthesis.pending > 0` trigger) — no manual CLAUDE.md editing needed. The rating skill fires from the hook directive and needs no instruction line either.

If the automatic symlink/junction install failed for some reason (e.g. Windows with neither Developer Mode nor an elevated shell), link manually:

```bash
# Linux / macOS
ln -s "$PWD/.claude/skills/better-memory-synthesize" ~/.claude/skills/better-memory-synthesize
ln -s "$PWD/.claude/skills/rate-session-memories"    ~/.claude/skills/rate-session-memories
ln -s "$PWD/.claude/skills/start-better-memory-ui"   ~/.claude/skills/start-better-memory-ui

# Windows (PowerShell, run from this repo, as Administrator OR with developer mode)
New-Item -ItemType Junction -Path "$env:USERPROFILE\.claude\skills\better-memory-synthesize" -Target "$PWD\.claude\skills\better-memory-synthesize"
New-Item -ItemType Junction -Path "$env:USERPROFILE\.claude\skills\rate-session-memories"    -Target "$PWD\.claude\skills\rate-session-memories"
New-Item -ItemType Junction -Path "$env:USERPROFILE\.claude\skills\start-better-memory-ui"   -Target "$PWD\.claude\skills\start-better-memory-ui"
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
This only applies to the `observer` hook (PostToolUse) — it's the one hook `better-memory setup` assigns `.venv\Scripts\pythonw.exe` to, since it's the only hook that doesn't need to read Claude Code's stdout back. If it's flashing anyway, your `observer` entry is pointing at `python.exe` instead; run `uv run better-memory doctor --fix` to restore the correct interpreter assignments (or fix it by hand and switch just that entry to `pythonw.exe`). Do **not** switch any other hook (`session_bootstrap`, `session_close`, `stop_sweep`, `contextual_inject`, `commit_checkpoint`, `pre_compact`) to `pythonw.exe` — they all need `python.exe` for Claude Code to read their stdout (`additionalContext` / block directives / `systemMessage`); `pythonw.exe` silently nulls stdout and those hooks would go dark.

**Wiring drifted after a repo move, upgrade, or a hand-edit to `~/.claude/settings.json` / `~/.claude.json` / `~/.claude/CLAUDE.md`.**
Run `uv run better-memory doctor` to see what's stale, or `uv run better-memory doctor --fix` to repair it in place. See [Doctor](#doctor).

## Architecture

See `docs/superpowers/specs/2026-04-06-better-memory-design.md` for the original design spec — four-layer epistemic hierarchy, hybrid search via FTS5 + sqlite-vec + RRF, reinforcement-weighted ranking, and the consolidation pipeline.

### The retrieval-quality layer (July 2026)

Three shipped upgrades sit on top of that foundation (specs: `2026-07-23-retrieval-quality-design.md`, `2026-07-23-deferred-injection-design.md`):

- **Wilson-score ranking.** Reflections and semantic memories rank by the lower bound of their observed hit rate — `(useful + overlooked) / (useful + overlooked + ignored)` — so a memory that helps 3 times out of 4 serves outranks one that helped 67 times out of 192. No raw-count rich-get-richer. One shortlist slot per bucket is reserved for memories with fewer than 3 ratings (the **exploration slot**), so new lessons get served, rated, and scored instead of starving; those serves are tagged (`via_exploration`) and excluded from headline usefulness metrics.
- **Deferred, evidence-gated injection.** SessionStart injects only your standing rules; each prompt (and the session's first tool call) is then scored against the store with three legs — BM25 text match, vector cosine (reflections and semantic memories are embedded at write time, self-healed on retrieve, backfillable via `python -m better_memory.cli.backfill_embeddings`), and the Wilson prior for ranking only. A memory injects solely on positive relevance evidence; popularity can never force an irrelevant injection. Ollama outages cost at most one bounded stall per minute (file-persisted circuit breaker shared across hook processes).
- **Evidence-anchored ratings.** Non-`ignored` ratings require a one-line receipt, enforced server-side and stored on the exposure row; the management UI shows each memory's rating-evidence history. Ratings are the fuel for everything above, so their variance is the system's noise floor — the receipts anchor them to observable events.

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
