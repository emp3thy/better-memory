# AgentCore troubleshooting

Errors you'll likely hit when running better-memory in `agentcore` mode, and what to do about them.

## `ValidationException: Memory name does not match required pattern`

AgentCore memory names must match `[a-zA-Z][a-zA-Z0-9_]{0,47}` — letters, digits, and underscores only, **no dashes**. `better-memory agentcore init` uses safe defaults (`better_memory_episodic` and `better_memory_semantic`); if you've edited `agentcore.json` by hand, check both names match the regex.

## `ResourceNotFoundException` on a fresh write/update

AgentCore has roughly 10 seconds of indexing lag between `BatchCreateMemoryRecords` and the new record being mutable. The backend retries `batch_update_memory_records` calls automatically (`_retry_on_transient_404`, 3 attempts, 10s backoff) — if you see this error escaping into MCP tool responses, the record genuinely doesn't exist (wrong memory ID, deleted, or namespace mismatch).

## `400 — Metadata keys cannot use reserved names or prefixes`

AgentCore reserves the `x-amz-agentcore-memory-*` metadata namespace. The backend strips system-managed keys from update payloads before sending (`_full_metadata_snapshot`), but if you're constructing a record by hand via the smoke or a test, do the same: skip any key whose name starts with `x-amz-agentcore-memory-`.

## `NoCredentialsError` or `Unable to locate credentials`

boto3 didn't find AWS credentials. Standard discovery order:

1. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars
2. `~/.aws/credentials` (named profile via `AWS_PROFILE`)
3. EC2/EKS instance role
4. ECS task role

If you're on a laptop, `aws configure` is the fastest fix. If you're in a container, mount credentials or attach a task role.

## `ResourceConflictException: A memory with this name already exists`

`init` refuses to create a duplicate memory. Either:

- Delete the existing memory via the AWS console and re-run, **or**
- Manually populate `agentcore.json` with the existing memory's IDs (see the file format in `better_memory/storage/agentcore_persistence.py`).

## `MEMORY_FAILED` after `init`

Memory creation entered a terminal `FAILED` state. Check the AWS console for the actual failure reason (strategy-config errors, Bedrock region availability). Common cause: requesting a region where AgentCore Memory isn't GA yet. Stick to `eu-west-2` unless you've verified GA elsewhere.

## Closure events not firing

In agentcore mode, the Stop hook emits a `CreateEvent(role=OTHER)` to tell the episodic strategy "this session is done, extract now." If episodic extraction is taking 15+ minutes instead of 1-3, check:

- `~/.better-memory/agentcore.json` exists at session-close time (Stop hooks run in the same process; if `BETTER_MEMORY_HOME` is unset and the home directory isn't `~/.better-memory/`, the hook can't find the config).
- The IAM principal has `bedrock-agentcore:CreateEvent` permission.
- The Stop hook's error log (`hook_errors` table in `memory.db` — or stdout if running interactively) shows no `session_close_agentcore` entries.

Failure to fire is non-fatal; the episodic strategy still triggers eventually via 15-20 minute idle detection.

## "Region mismatch" — events written but never extracted

`init` writes the region to `agentcore.json`; the MCP server reads it from there and builds clients targeting that region. If you change region via `BETTER_MEMORY_AGENTCORE_REGION` mid-flight, you'll write events to the new region but `init`'s memories live in the old region. Either re-`init` in the new region or revert the env var.
