# AgentCore setup

`agentcore` is better-memory's optional cloud-managed storage backend. It uses AWS Bedrock AgentCore Memory (currently GA in `eu-west-2`) for storage and built-in LLM extraction. Pick it if you want team-shared memory or managed extraction; the [sqlite](configuration.md) backend remains the default and the recommended choice for single-machine usage.

## Prerequisites

- AWS account with Bedrock AgentCore Memory enabled in `eu-west-2`.
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
export BETTER_MEMORY_STORAGE_BACKEND=agentcore
better-memory agentcore init
```

The `init` command creates two AgentCore memories — one for episodic reflections, one for semantic preferences — and writes their IDs to `$BETTER_MEMORY_HOME/agentcore.json`. Creation takes 90-115 seconds per memory; progress prints every 5 seconds so you can confirm the process isn't hung.

After `init` returns, restart your Claude Code session (or the MCP server). The MCP server now reads `agentcore.json` and constructs an `AgentCoreBackend` instead of `SqliteBackend`.

## Verify

```bash
better-memory agentcore status
```

Should print `ACTIVE` for both memories. If you see `CREATING`, wait a minute and re-run.

```bash
better-memory agentcore smoke
```

Drives a minimal observe → list_events → batch_create → list_records → batch_delete cycle. Exit 0 means the round-trip works end-to-end. This is the recommended ops check after any region or credential change.

## What changes in agentcore mode

- **No SQLite traffic.** The MCP server doesn't open `memory.db` and doesn't run synthesis (AgentCore's built-in episodic strategy handles extraction).
- **`memory.synthesize_next_*` tools are not registered** — the strategy extracts in the cloud on its own ~15-20 minute cadence (~1-3 minutes after a closure event).
- **`pending_synthesis` is omitted from `memory.start_episode`'s response** — there's no local pending queue.
- **Closure events fire automatically.** The Stop hook emits a `CreateEvent(role=OTHER)` against the current AgentCore session, which tells the episodic strategy "extract now". Failure is logged but never blocks the hook.
- **Episode lifecycle methods are no-ops.** AgentCore manages event grouping via `sessionId`; better-memory's episodes table has no equivalent.

See [Architecture > Storage backends](architecture.md#storage-backends) for the data-flow diagram.

## Troubleshooting

See [AgentCore troubleshooting](troubleshooting/agentcore.md) for common errors (name regex, ~10s lag, system-key handling, credential discovery, region mismatches).
