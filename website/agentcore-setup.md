# AgentCore setup

`agentcore` is better-memory's optional cloud-managed storage backend. It uses AWS Bedrock AgentCore Memory (currently GA in `eu-west-2`) for storage and built-in LLM extraction. Pick it if you want team-shared memory or managed extraction; the [sqlite](configuration.md) backend remains the default and the recommended choice for single-machine usage.

## Prerequisites

- AWS account with Bedrock AgentCore Memory available in your chosen region (`init --region` defaults to `eu-west-2`).
- IAM principal (user or role) with the policy below attached.
- AWS credentials discoverable by boto3 (env vars, `~/.aws/credentials`, EC2/EKS role, etc.).
- `better-memory[agentcore]` installed: `pip install 'better-memory[agentcore]'` or `uv pip install '.[agentcore]'`. The extra pins `boto3>=1.43.56` and `botocore>=1.43.56` — older versions predate the `extractionMode` parameter on `CreateEvent`, which both the Stop hook's closure event and the end-of-session rating sweep's receipt event (see [Capability table](#capability-table)) depend on. If you install boto3/botocore separately (rather than through the extra), make sure they meet that floor.

## IAM policy

Narrow policy — `bedrock-agentcore` (data plane) + `bedrock-agentcore-control` (control plane). No Bedrock model access needed (built-in strategies have their own infrastructure).

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore-control:CreateMemory",
        "bedrock-agentcore-control:GetMemory",
        "bedrock-agentcore-control:ListMemories",
        "bedrock-agentcore-control:DeleteMemory",
        "bedrock-agentcore-control:UpdateMemoryStrategy",
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:ListEvents",
        "bedrock-agentcore:BatchCreateMemoryRecords",
        "bedrock-agentcore:BatchUpdateMemoryRecords",
        "bedrock-agentcore:BatchDeleteMemoryRecords",
        "bedrock-agentcore:ListMemoryRecords",
        "bedrock-agentcore:GetMemoryRecord"
      ],
      "Resource": "*"
    }
  ]
}
```

For tighter scoping, restrict `Resource` to the two memory ARNs after `init` writes them. `UpdateMemoryStrategy` is only exercised by `better-memory agentcore migrate` — it widens an older semantic memory's schema so it declares `source_row_id` — and is not needed by `init`, `status`, or `smoke`; drop it if you never migrate.

## Initialise

```bash
better-memory agentcore init --region <region>
```

That's the whole activation — no environment variable to export. The `init` command:

1. Creates two AgentCore memories — one for episodic reflections, one for semantic preferences. Creation takes 90-115 seconds per memory; progress prints every 5 seconds so you can confirm the process isn't hung.
2. Writes their IDs **and the region** to `$BETTER_MEMORY_HOME/agentcore.json`. That file's region is the single source of truth for every client better-memory builds — the MCP server's data/control clients and the Stop hook's closure client all sign against it. (`--region` defaults to `eu-west-2`.)
3. Activates the backend by writing `{"storage_backend": "agentcore"}` into `$BETTER_MEMORY_HOME/settings.json` (merged into any existing keys, written atomically). The MCP server, all hooks, and the CLI resolve the backend as: `BETTER_MEMORY_STORAGE_BACKEND` env var if set → else `settings.json` → else `sqlite`. The env var always wins, so if you have it exported to `sqlite` somewhere, unset it or set it to `agentcore`.

To provision without activating (scripting), pass `--no-activate`: agentcore.json is written but the backend selection is unchanged.

To revert to sqlite: remove the `storage_backend` key from `settings.json`, or set `BETTER_MEMORY_STORAGE_BACKEND=sqlite`.

After `init` returns, restart your Claude Code session (or the MCP server) so it picks up the new backend. The MCP server reads `settings.json`, then `agentcore.json`, and constructs an `AgentCoreBackend` instead of `SqliteBackend`.

!!! note "Custom `BETTER_MEMORY_HOME`"
    The installer injects `BETTER_MEMORY_HOME` only into the MCP server's env block in `~/.claude.json`; hooks run without it and fall back to `~/.better-memory`. If you use a custom home, run `init` against it (`--home` or the env var) **and** make sure hooks can see the same home — otherwise the Stop hook looks for `~/.better-memory/settings.json` and stays on sqlite.

## Verify

```bash
better-memory agentcore status
```

Prints an `effective backend: <backend> (source: env|settings|default)` line — confirm it says `agentcore` — then per-memory state. Should print `ACTIVE` for both memories. If you see `CREATING`, wait a minute and re-run. If the effective backend says `sqlite (source: env)`, a `BETTER_MEMORY_STORAGE_BACKEND` env var is overriding your settings.json.

```bash
better-memory agentcore smoke
```

Drives a minimal observe → list_events → batch_create → list_records → batch_delete cycle. Exit 0 means the round-trip works end-to-end. Smoke validates AWS credentials and wire access, not MCP registration. This is the recommended ops check after any region or credential change.

## Migrate existing memory (optional)

`init` gives you empty AgentCore memories. If you already have sqlite memory worth carrying across, `better-memory agentcore migrate` bulk-copies it into the two AgentCore memories:

```bash
better-memory agentcore migrate --dry-run   # preview the plan, zero AWS calls
better-memory agentcore migrate             # execute
```

It migrates two kinds by default (`--include reflections,semantic`):

- **Reflections** → the episodic memory. All of a migrated reflection's state — the rating counters, its `status`, and the `source_row_id` used for de-duplication — lives in the record's **JSON content body**, not its metadata. (Cloud-extracted records store that state in record *metadata*; migrated reflections can't, because the built-in episodic strategy owns the metadata schema.)
- **Semantic memories** → the semantic memory. Here `source_row_id` and the counters live in **declared metadata**, so migrate first ensures the semantic strategy schema declares `source_row_id` — widening it in place if needed — because AWS silently drops undeclared metadata keys on client writes.

Migration is **idempotent**: a per-row ledger (the `agentcore_migration` table in `memory.db`) plus a reconcile-by-`source_row_id` scan means a re-run converges instead of duplicating. A crashed or partial run is safe to re-run; failed records are marked in the ledger and retried next time. Only one run at a time — a `<home>/agentcore_migration.lock` file guards against concurrent runs.

Flags (read from the `migrate` argparse):

| Flag | Effect |
|---|---|
| `--dry-run` | Compute the plan and print create/update/skip/retire tallies without any AWS call. |
| `--include <kinds>` | Comma list of kinds to migrate (default `reflections,semantic`). `observations` is accepted but **lossy and non-retrievable** — observation replay is not performed; they are treated as already distilled into reflections. |
| `--restart` | Re-verify/upsert every eligible row, ignoring the ledger's `migrated` state. |
| `--provision` | Create the target memories (or replace a missing / not-ACTIVE one) before migrating, instead of hard-erroring; persists any new IDs to `agentcore.json`. |
| `--project <name>` | Limit migration to one project (plus `general`-scope rows). |
| `--db <path>` | Source SQLite path (default `<home>/memory.db`). |
| `--batch-size <n>` | Records per `BatchCreateMemoryRecords` call (default 25). |
| `--verify` | Read one migrated record per kind back via `GetMemoryRecord` and diff it against SQLite. |
| `--region <r>` / `--home <p>` | Region / home overrides (default: region from `agentcore.json`, home from `$BETTER_MEMORY_HOME`). |

**Migration does not activate the backend.** `migrate` only writes records into AWS; it never touches `settings.json`. Activate separately with `init` (or by setting `storage_backend` yourself) — see [Initialise](#initialise). Exit codes: `0` all rows converged, `2` completed with some failed records (re-run to retry), `1` a configuration or setup error.

## Capability table

The self-rating / learning loop is **backend-agnostic**: agentcore mode runs the same exposure tracking, mid-session credit, end-of-session rating sweep, Wilson-score ranking, and exploration slot that sqlite mode does — just against AWS-side counters instead of local columns. What still differs is synthesis, episodes, and retention (AgentCore manages those internally and better-memory's local machinery has no equivalent), plus evidence browsing (local-only for now).

| Capability | `sqlite` | `agentcore` |
|---|---|---|
| Exposure ledger, mid-session `memory.credit`, end-of-session sweep, Wilson ranking, exploration slot | Local, entirely in `memory.db` | Same loop. Exposure ledger is the same local `memory.db` table (session-operational state, not memory content); rating counters (`useful_count`, `ignored_count`, `overlooked_count`, `times_misled`) live on the AWS record and are genuinely shared across every teammate's session. |
| Query-conditioned retrieval (`memory.retrieve(query=...)`) | BM25 + vector RRF against the Wilson prior | Server-side semantic search (`RetrieveMemoryRecords`) RRF-fused against the Wilson prior instead (no BM25 leg — semantic search subsumes it); degrades to the Wilson-only order on an AWS error. |
| Contextual-injection evidence gate | BM25 / vector legs | The backend's own `relevance_ranks` (same `RetrieveMemoryRecords` semantic search) — a memory qualifies iff present in that result set; keyword fallback applies only when the AWS lookup itself fails, never merely because it found no matches. |
| Rating-evidence browsing | Per-exposure `evidence` column, browsable in the management UI's Reflections/Semantic drawers | **Local-only for now.** The local exposure rows are stamped with evidence the same way, but there's no cross-machine evidence browsing yet — a future PR adds a read path for the ratings-event log below. |
| Rating-evidence receipts | N/A (the local row is the record) | Each end-of-session sweep emits one best-effort `CreateEvent` (`extractionMode: "SKIP"`, `metadata.type: "ratings"`) as a durable, team-visible receipt of what was rated. Durable from day one; no read/browse UI for it yet (non-goal of this change, tracked as a follow-up). |
| Synthesis (observation → reflection distillation) | Local, Claude-driven (`memory.synthesize_next_*`) | Cloud, built-in strategy on its own ~15-20 minute cadence — unchanged by this parity work. |
| Episodes | Local `episodes` table + episode tools | Internal to AgentCore via `sessionId` — unchanged by this parity work. |
| Retention | `memory.run_retention` | AgentCore's own event expiry (set at `init`) — unchanged by this parity work. |

!!! warning "Silent metadata drop on undeclared keys"
    AgentCore batch create/update calls that include a `memoryStrategyId` **silently strip any metadata key not declared in that strategy's `memoryRecordSchema`** — no error, no warning, the key is just absent from the stored record. This is documented AWS behaviour, not a better-memory bug, but it is easy to trip over if you extend the schema yourself.

    better-memory's own counters are safe: every key it writes (`useful_count`, `missed_count`, `ignored_count`, `times_misled`, `overlooked_count`, `last_credited_at`, `status`, plus `source_row_id` on the semantic strategy) is declared on both strategies at `init` time (`cli/_agentcore_strategies.py`). If you add a new metadata key of your own, declare it in the strategy's schema first — and if the memory is already provisioned, widen the schema in place via `UpdateMemoryStrategy` (the same call `better-memory agentcore migrate` uses to add `source_row_id` to older semantic memories) — or writes of that key will silently vanish.

## UI capability flags

The management UI's `create_app` builds a `StorageBackend` and reads six `@property -> bool` capability flags off it through a `caps` template context processor: `supports_episodes`, `supports_observations`, `supports_provenance`, `supports_retention_runs`, `supports_reflection_review`, `supports_reflection_text_edit`. All six are `False` in agentcore mode (all `True` on sqlite). In this release only the Episodes nav link is gated on `caps.supports_episodes`; the remaining five flags are wired into every template already but have no consuming gate yet — they are reserved for later UI work that hides the Observations/Provenance/Retention/Reflection-review/Reflection-edit surfaces agentcore mode has no backing data for.

## What changes in agentcore mode

- **Memory content lives in AWS; session-operational state stays local.** Observations, reflections, semantic memories, and reinforcement all go to Bedrock AgentCore — `memory.observe`, `memory.retrieve`, `memory.retrieve_observations`, `memory.record_use`, the four `memory.semantic_*` tools, the rating tools (`memory.credit`, `memory.apply_session_ratings`, `memory.list_session_exposures`), and `memory.session_bootstrap` all dispatch to the AgentCore backend. A local `memory.db` and `knowledge.db` are still created — `knowledge.db` for the knowledge tools, `memory.db` for hook-error logging, the `agentcore_migration` ledger, and (see the capability table above) the exposure ledger that drives the self-rating loop. The rule is: agentcore mode never stores memory CONTENT locally; session-operational state (exposure ledger, migration ledger, hook errors) always lives in the local `memory.db`, on both backends.
- **`memory.synthesize_next_*` tools are not registered** — the built-in episodic strategy extracts in the cloud on its own ~15-20 minute cadence (~1-3 minutes after a closure event).
- **Episode and retention tools are not registered.** `memory.start_episode`, `memory.close_episode`, `memory.reconcile_episodes`, `memory.list_episodes`, and `memory.run_retention` are hidden from the advertised tool list — AgentCore manages event grouping via `sessionId` and applies its own event expiry, so better-memory's local episode/retention machinery has no equivalent.
- **Closure events fire automatically.** The Stop hook resolves the backend the same way the server does (env var, else `settings.json`) and, when it resolves to agentcore, emits a `CreateEvent(role=OTHER)` against the current AgentCore session, which tells the episodic strategy "extract now". Failure is logged but never blocks the hook. The mere existence of `agentcore.json` does **not** activate this — only the env var or `settings.json` does.
- **The end-of-session rating sweep emits a receipt event too.** After `memory.apply_session_ratings` successfully stamps local exposure rows and pushes counter bumps to AWS, it emits one best-effort `CreateEvent` (`extractionMode: "SKIP"` so it's excluded from LLM extraction) carrying the rated batch as its payload. This is a durable, team-visible audit trail from day one; failure never blocks the sweep, and there's no read/browse path for these events yet (see the capability table above).

See [Architecture > Storage backends](architecture.md#storage-backends) for the data-flow diagram.

## Troubleshooting

See [AgentCore troubleshooting](troubleshooting/agentcore.md) for common errors (name regex, ~10s lag, system-key handling, credential discovery, region mismatches).
