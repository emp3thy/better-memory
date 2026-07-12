# AgentCore setup

`agentcore` is better-memory's optional cloud-managed storage backend. It uses AWS Bedrock AgentCore Memory (currently GA in `eu-west-2`) for storage and built-in LLM extraction. Pick it if you want team-shared memory or managed extraction; the [sqlite](configuration.md) backend remains the default and the recommended choice for single-machine usage.

## Prerequisites

- AWS account with Bedrock AgentCore Memory available in your chosen region (`init --region` defaults to `eu-west-2`).
- IAM principal (user or role) with the policy below attached.
- AWS credentials discoverable by boto3 (env vars, `~/.aws/credentials`, EC2/EKS role, etc.).
- `better-memory[agentcore]` installed: `pip install 'better-memory[agentcore]'` or `uv pip install '.[agentcore]'`.

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

For tighter scoping, restrict `Resource` to the two memory ARNs after `init` writes them.

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

## What changes in agentcore mode

- **Memory data lives in AWS.** Observations, reflections, semantic memories, and reinforcement all go to Bedrock AgentCore — `memory.observe`, `memory.retrieve`, `memory.retrieve_observations`, `memory.record_use`, the four `memory.semantic_*` tools, the rating tools (`memory.credit`, `memory.apply_session_ratings`, `memory.list_session_exposures`), and `memory.session_bootstrap` all dispatch to the AgentCore backend. A local `memory.db` is still created for hook-error logging and `knowledge.db` for knowledge tools; no memory content is stored in them.
- **`memory.synthesize_next_*` tools are not registered** — the built-in episodic strategy extracts in the cloud on its own ~15-20 minute cadence (~1-3 minutes after a closure event).
- **Episode and retention tools are not registered.** `memory.start_episode`, `memory.close_episode`, `memory.reconcile_episodes`, `memory.list_episodes`, and `memory.run_retention` are hidden from the advertised tool list — AgentCore manages event grouping via `sessionId` and applies its own event expiry, so better-memory's local episode/retention machinery has no equivalent.
- **Closure events fire automatically.** The Stop hook resolves the backend the same way the server does (env var, else `settings.json`) and, when it resolves to agentcore, emits a `CreateEvent(role=OTHER)` against the current AgentCore session, which tells the episodic strategy "extract now". Failure is logged but never blocks the hook. The mere existence of `agentcore.json` does **not** activate this — only the env var or `settings.json` does.

See [Architecture > Storage backends](architecture.md#storage-backends) for the data-flow diagram.

## Troubleshooting

See [AgentCore troubleshooting](troubleshooting/agentcore.md) for common errors (name regex, ~10s lag, system-key handling, credential discovery, region mismatches).
